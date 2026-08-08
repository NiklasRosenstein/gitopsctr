"""Driver-neutral deployment verification and explicit reconciliation reapplication."""

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli as deploy_release
from gitopsctr.driver import VerificationContext, VerificationResult, VerificationStatus

DESIRED_REVISION = "d" * 40


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _unit(name: str, driver: str, revision: str) -> dict[str, object]:
    return {
        "schema": 1,
        "name": name,
        "driver": driver,
        "source": {
            "path": f"deployment/{name}",
            "revision": revision,
            "driverVersion": deploy_release.PLUGIN_VERSIONS[driver],
        },
        "inputs": {"environment": "prod"},
    }


def _install_desired_state(monkeypatch, units: list[dict[str, object]]) -> list[str]:
    materialized: list[str] = []

    def deployment_refs(source_root, environment, desired=None, observed=None):
        assert source_root == deploy_release.REPOSITORY_ROOT
        assert (environment, desired, observed) == ("prod", None, None)
        return "deploy/prod", "observed/prod"

    def resolve_ref(ref, revision=None):
        assert (ref, revision) == ("deploy/prod", None)
        return DESIRED_REVISION

    monkeypatch.setattr(deploy_release, "deployment_refs", deployment_refs)
    monkeypatch.setattr(deploy_release, "resolve_ref", resolve_ref)

    def materialize(revision: str, output: Path) -> None:
        materialized.append(revision)
        output.mkdir(parents=True, exist_ok=True)
        if revision == DESIRED_REVISION:
            for unit in units:
                _write_json(output / "units" / f"{unit['name']}.json", unit)

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    for name in ("observed_tree", "publish_receipt_cas", "write_json"):
        monkeypatch.setattr(
            deploy_release,
            name,
            lambda *_args, operation=name, **_kwargs: pytest.fail(f"read-only verification called {operation}"),
        )
    return materialized


def test_verify_runs_every_selected_driver_and_reports_drift_after_all_units(monkeypatch, capsys):
    units = [
        _unit("images", "oci-images", "a" * 40),
        _unit("infrastructure", "terraform", "b" * 40),
    ]
    materialized = _install_desired_state(monkeypatch, units)
    calls: list[tuple[str, VerificationContext]] = []

    def verifier(status):
        def verify(context):
            calls.append((context.unit["name"], context))
            return VerificationResult(status)

        return verify

    monkeypatch.setitem(
        deploy_release.VERIFICATION_PLUGINS,
        "oci-images",
        SimpleNamespace(verify=verifier(VerificationStatus.DRIFT)),
    )
    monkeypatch.setitem(
        deploy_release.VERIFICATION_PLUGINS,
        "terraform",
        SimpleNamespace(verify=verifier(VerificationStatus.CLEAN)),
    )

    with pytest.raises(deploy_release.OperationError, match="detected drift in: images"):
        deploy_release.command_verify(Namespace(environment="prod", unit=None))

    assert [name for name, _ in calls] == ["images", "infrastructure"]
    assert materialized == [DESIRED_REVISION, "a" * 40, "b" * 40]
    assert calls[0][1].source_revision == "a" * 40
    assert calls[0][1].source_path == "deployment/images"
    assert calls[0][1].inputs == {"environment": "prod"}
    output = capsys.readouterr().err
    assert "DRIFT    images" in output
    assert "CLEAN    infrastructure" in output
    assert "RESULT   DRIFT: images" in output


def test_verify_deduplicates_selected_units_and_reports_clean(monkeypatch, capsys):
    units = [
        _unit("images", "oci-images", "a" * 40),
        _unit("infrastructure", "terraform", "b" * 40),
    ]
    materialized = _install_desired_state(monkeypatch, units)
    calls: list[str] = []
    monkeypatch.setitem(
        deploy_release.VERIFICATION_PLUGINS,
        "terraform",
        SimpleNamespace(
            verify=lambda context: calls.append(context.unit["name"]) or VerificationResult(VerificationStatus.CLEAN)
        ),
    )

    deploy_release.command_verify(Namespace(environment="prod", unit=["infrastructure", "infrastructure"]))

    assert calls == ["infrastructure"]
    assert materialized == [DESIRED_REVISION, "b" * 40]
    assert "RESULT   CLEAN" in capsys.readouterr().err


def test_verify_preflights_unsupported_drivers_before_running_any_unit(monkeypatch):
    units = [
        _unit("infrastructure", "terraform", "a" * 40),
        _unit("images", "oci-images", "b" * 40),
    ]
    materialized = _install_desired_state(monkeypatch, units)
    monkeypatch.setitem(
        deploy_release.VERIFICATION_PLUGINS,
        "terraform",
        SimpleNamespace(verify=lambda _context: pytest.fail("preflight must finish before verification starts")),
    )

    with pytest.raises(
        deploy_release.OperationError,
        match="images uses oci-images, which does not support verification",
    ):
        deploy_release.command_verify(Namespace(environment="prod", unit=None))

    assert materialized == [DESIRED_REVISION]


def test_verify_rejects_unmaterialized_desired_units_before_running_driver(monkeypatch):
    unit = _unit("infrastructure", "terraform", "a" * 40)
    unit["inputs"] = {
        "image": {
            "fromObservation": "units/images.json",
            "pointer": "/artifacts/image",
        }
    }
    materialized = _install_desired_state(monkeypatch, [unit])
    monkeypatch.setitem(
        deploy_release.VERIFICATION_PLUGINS,
        "terraform",
        SimpleNamespace(verify=lambda _context: pytest.fail("unmaterialized unit ran verification")),
    )

    with pytest.raises(deploy_release.OperationError, match="not fully materialized"):
        deploy_release.command_verify(Namespace(environment="prod", unit=None))

    assert materialized == [DESIRED_REVISION]


@pytest.mark.parametrize(("reapply", "driver_runs"), ((False, 0), (True, 1)))
def test_reapply_only_bypasses_the_clean_receipt_shortcut(monkeypatch, reapply, driver_runs):
    unit = _unit("infrastructure", "terraform", "a" * 40)
    calls: list[str] = []
    publications: list[dict[str, object]] = []
    outputs: list[tuple[bool, str]] = []

    monkeypatch.setattr(deploy_release, "load_environment", lambda *_args: {"schema": 1})
    monkeypatch.setattr(
        deploy_release,
        "deployment_refs",
        lambda *_args: ("deploy/dev", "observed/dev"),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: DESIRED_REVISION)

    def observed_tree(_ref: str, output: Path):
        _write_json(
            output / "units/infrastructure.json",
            {
                "schema": 1,
                "unit": "infrastructure",
                "driver": "terraform",
                "desired": {"revision": DESIRED_REVISION, "unitBlob": "same-unit"},
                "resolvedInputs": {},
                "controller": {"revision": "c" * 40},
                "applied": {"sourceRevision": "a" * 40},
                "outputs": {},
            },
        )
        return "o" * 40

    def materialize(revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if revision == DESIRED_REVISION:
            _write_json(output / "units/infrastructure.json", unit)

    def driver(context):
        calls.append(context.unit["name"])
        return {"applied": {"sourceRevision": context.source_revision}, "outputs": {}}

    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "same-unit")
    monkeypatch.setattr(deploy_release, "controller_evidence", lambda: {"revision": "c" * 40})
    monkeypatch.setattr(
        deploy_release,
        "publish_receipt_cas",
        lambda _ref, _unit, receipt, _revision: publications.append(receipt) or "p" * 40,
    )
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda changed, desired="": outputs.append((changed, desired)),
    )
    monkeypatch.setitem(
        deploy_release.RECONCILIATION_PLUGINS,
        "terraform",
        SimpleNamespace(reconcile=driver, result_contract=deploy_release.UNIT_PLUGINS["terraform"].result_contract),
    )

    changed = deploy_release.command_reconcile(
        Namespace(
            unit="infrastructure",
            environment="dev",
            desired_ref=None,
            desired_revision=None,
            observed_ref=None,
            plan=False,
            report=None,
            source_revision=None,
            advance=False,
            require_source_ref=None,
            reapply=reapply,
        )
    )

    assert calls == ["infrastructure"] * driver_runs
    assert len(publications) == driver_runs
    assert changed is bool(driver_runs)
    assert outputs == [(bool(driver_runs), "")]


def test_parser_exposes_repeatable_verify_units_and_reapply():
    parser = deploy_release.build_parser()

    verify = parser.parse_args(["verify", "--environment", "prod", "--unit", "api", "--unit", "database"])
    reconcile = parser.parse_args(["reconcile", "--environment", "prod", "--unit", "api", "--reapply"])

    assert verify.handler is deploy_release.command_verify
    assert verify.unit == ["api", "database"]
    assert reconcile.reapply is True
