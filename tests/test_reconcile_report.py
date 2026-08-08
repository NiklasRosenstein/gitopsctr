"""Reconciliation preserves planning and convergence behavior around clean units."""

import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli as deploy_release


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_reconcile_parser_exposes_plan_without_a_dry_alias():
    parser = deploy_release.build_parser()

    args = parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--plan"])
    assert args.plan is True
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--dry"])


def test_observation_reference_uses_fallback_only_during_dry_resolution(tmp_path):
    template = {
        "image": {
            "fromObservation": "units/application-images.json",
            "pointer": "/artifacts/containers.json/artifacts/control/uri",
            "dryFallback": "preview.invalid/control@sha256:" + "0" * 64,
        }
    }
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"

    resolved, desired_inputs, observed_inputs = deploy_release.resolve_template(
        template,
        candidate,
        observed,
        None,
        dry=True,
    )

    assert resolved == {"image": "preview.invalid/control@sha256:" + "0" * 64}
    assert desired_inputs == {}
    assert observed_inputs == {}

    try:
        deploy_release.resolve_template(template, candidate, observed, None)
    except deploy_release.ReferenceUnavailable:
        pass
    else:
        raise AssertionError("non-dry resolution accepted a dry fallback")


def test_observation_reference_materializes_artifact_into_consumer(tmp_path):
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    producer = candidate / "units/application-images.json"
    _write_json(producer, {"name": "application-images"})
    _write_json(
        observed / "units/application-images.json",
        {
            "desired": {"unitBlob": deploy_release.file_blob(producer)},
            "artifacts": {
                "containers.json": {"artifacts": {"control": {"uri": "registry.example/control@sha256:" + "1" * 64}}}
            },
        },
    )

    resolved, desired_inputs, observed_inputs = deploy_release.resolve_template(
        {
            "image": {
                "fromObservation": "units/application-images.json",
                "pointer": "/artifacts/containers.json/artifacts/control/uri",
            }
        },
        candidate,
        observed,
        "a" * 40,
    )

    assert resolved == {"image": "registry.example/control@sha256:" + "1" * 64}
    assert desired_inputs == {}
    assert observed_inputs == {
        "units/application-images.json": deploy_release.file_blob(observed / "units/application-images.json")
    }


def test_unknown_unit_fails_before_advancing_desired_state(monkeypatch):
    def fake_git(*args, **_kwargs):
        assert args == ("rev-parse", "HEAD^{commit}")
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(
        deploy_release,
        "advance_desired",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unknown unit advanced desired state")),
    )

    with pytest.raises(
        deploy_release.OperationError,
        match=r"unknown unit 'application' for environment 'dev'; available units: ",
    ):
        deploy_release.command_reconcile(
            Namespace(
                unit="application",
                environment="dev",
                desired_ref="deploy/dev",
                desired_revision=None,
                observed_ref="observed/dev",
                plan=False,
                report=None,
                source_revision="HEAD",
                advance=True,
                require_source_ref=None,
            )
        )


def test_known_unmaterialized_unit_remains_a_successful_wait(monkeypatch):
    outputs = []

    def fake_observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        return "b" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, output: output.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda reconciled, desired="": outputs.append((reconciled, desired)),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="frontend",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision=None,
            observed_ref="observed/dev",
            plan=False,
            report=None,
            source_revision=None,
            advance=False,
            require_source_ref=None,
        )
    )

    assert outputs == [(False, "")]


def test_planned_reconcile_executes_clean_unit_and_passes_report(tmp_path, monkeypatch):
    report = tmp_path / "report"
    calls = []

    def fake_observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "observed/dev":
            _write_json(
                output / "units/aws-application.json",
                {"desired": {"unitBlob": "same-unit"}},
            )
            return "b" * 40
        return None

    materializations = 0

    def fake_materialize(_revision: str, output: Path):
        nonlocal materializations
        materializations += 1
        output.mkdir(parents=True, exist_ok=True)
        if materializations == 1:
            _write_json(
                output / "units/aws-application.json",
                {
                    "schema": 1,
                    "name": "aws-application",
                    "driver": "terraform",
                    "source": {"path": "infra/deploy", "revision": "a" * 40},
                },
            )

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "same-unit")
    monkeypatch.setitem(
        deploy_release.PLANNING_DRIVERS,
        "terraform",
        SimpleNamespace(plan=lambda context: calls.append(context)),
    )

    args = Namespace(
        unit="aws-application",
        environment="dev",
        desired_ref="deploy/dev",
        desired_revision=None,
        observed_ref="observed/dev",
        plan=True,
        report=str(report),
        source_revision=None,
        advance=False,
        require_source_ref=None,
    )
    deploy_release.command_reconcile(args)

    assert len(calls) == 1
    assert calls[0].report == report

    monkeypatch.delitem(deploy_release.PLANNING_DRIVERS, "terraform")
    materializations = 0
    with pytest.raises(deploy_release.OperationError, match="does not support planning"):
        deploy_release.command_reconcile(args)


def test_clean_reconcile_with_advance_finishes_pending_desired_convergence(tmp_path, monkeypatch):
    advances = []
    outputs = []

    def fake_observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "observed/dev":
            _write_json(
                output / "units/application-images.json",
                {"desired": {"unitBlob": "same-unit"}},
            )
            return "b" * 40
        return None

    def fake_materialize(_revision: str, output: Path):
        _write_json(
            output / "units/application-images.json",
            {
                "schema": 1,
                "name": "application-images",
                "driver": "oci-images",
                "source": {"path": ".", "revision": "a" * 40},
            },
        )

    def fake_advance(*args):
        advances.append(args)
        return "d" * 40, True

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "same-unit")
    monkeypatch.setattr(deploy_release, "advance_desired", fake_advance)
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda reconciled, desired="": outputs.append((reconciled, desired)),
    )
    monkeypatch.setitem(
        deploy_release.RECONCILIATION_DRIVERS,
        "oci-images",
        SimpleNamespace(reconcile=lambda _context: (_ for _ in ()).throw(AssertionError("clean unit ran its driver"))),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="application-images",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision="c" * 40,
            observed_ref="observed/dev",
            plan=False,
            report=None,
            source_revision="HEAD",
            advance=True,
            require_source_ref=None,
        )
    )

    assert len(advances) == 1
    assert advances[0][0] == "dev"
    assert advances[0][1] == deploy_release.git("rev-parse", "HEAD^{commit}").stdout.strip()
    assert advances[0][2:] == ("deploy/dev", "observed/dev", None)
    assert outputs == [(False, "d" * 40)]


def test_unpinned_reconcile_advances_and_pins_before_running_driver(tmp_path, monkeypatch):
    events = []
    advance_results = iter([("d" * 40, True), ("d" * 40, False)])

    def fake_git(*args, **_kwargs):
        assert args == ("rev-parse", "HEAD^{commit}")
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    def fake_materialize(revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if revision == "a" * 40:
            _write_json(
                output / "deployment/environments/dev/environment.json",
                {"schema": 1, "name": "dev"},
            )
        elif revision == "d" * 40:
            _write_json(
                output / "units/aws-application.json",
                {
                    "schema": 1,
                    "name": "aws-application",
                    "driver": "terraform",
                    "source": {"path": "infra/deploy", "revision": "e" * 40},
                },
            )

    def fake_advance(*args):
        events.append(("advance", args))
        return next(advance_results)

    def fake_observed_tree(_ref: str, output: Path):
        events.append(("observe",))
        output.mkdir(parents=True, exist_ok=True)
        return "b" * 40

    def fake_driver(context):
        events.append(("driver", context.source_revision))
        return {"applied": {"sourceRevision": context.source_revision}, "outputs": {}}

    def fake_publish(*_args):
        events.append(("receipt",))
        return "f" * 40

    def unexpected_resolve(*_args):
        raise AssertionError("pre-advanced reconciliation re-resolved desired head")

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(deploy_release, "advance_desired", fake_advance)
    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(deploy_release, "resolve_ref", unexpected_resolve)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "unit-blob")
    monkeypatch.setattr(deploy_release, "publish_receipt_cas", fake_publish)
    monkeypatch.setitem(
        deploy_release.RECONCILIATION_DRIVERS,
        "terraform",
        SimpleNamespace(reconcile=fake_driver),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="aws-application",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision=None,
            observed_ref="observed/dev",
            plan=False,
            report=None,
            source_revision="HEAD",
            advance=True,
            require_source_ref=None,
        )
    )

    assert [event[0] for event in events] == [
        "advance",
        "observe",
        "driver",
        "receipt",
        "advance",
    ]
    assert events[0][1] == ("dev", "a" * 40, "deploy/dev", "observed/dev", None)
    assert events[2] == ("driver", "e" * 40)


def test_superseded_source_stops_before_reconciliation(monkeypatch):
    def fake_git(*args, **_kwargs):
        assert args == ("rev-parse", "HEAD^{commit}")
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "b" * 40)
    monkeypatch.setattr(
        deploy_release,
        "advance_desired",
        lambda *_args: (_ for _ in ()).throw(AssertionError("superseded source advanced")),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="aws-application",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision=None,
            observed_ref="observed/dev",
            plan=False,
            report=None,
            source_revision="HEAD",
            advance=True,
            require_source_ref="main",
        )
    )
