"""Reconciliation preserves planning and convergence behavior around clean units."""

import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli as deploy_release
from gitopsctr.contrib.drivers.terraform import AppliedTerraformModel, TerraformResultModel
from gitopsctr.driver import ReconciliationOutput
from gitopsctr.resources import ResourceMetadata
from tests.conftest import receipt_document, write_test_document


@pytest.fixture(autouse=True)
def _local_effect_lease(monkeypatch):
    def acquire(_ref, revision, unit_name, uid, **_kwargs):
        lease = deploy_release.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="lease-test",
            owner="test-runner",
            desired_revision=revision,
            expires_at=2_000_000_000,
        )
        return deploy_release.EffectLeaseAcquisition(lease=lease, revision=revision)

    monkeypatch.setattr(deploy_release, "acquire_effect_lease", acquire)
    monkeypatch.setattr(deploy_release, "release_effect_lease", lambda *_args, **_kwargs: None)

    class NoopHeartbeat:
        def __init__(self, acquisition):
            self.acquisition = acquisition

        def stop(self):
            return self.acquisition

    monkeypatch.setattr(
        deploy_release,
        "start_effect_lease_heartbeat",
        lambda _ref, acquisition, **_kwargs: NoopHeartbeat(acquisition),
    )
    monkeypatch.setattr(
        deploy_release,
        "rebase_effect_completion",
        lambda _ref, acquisition, _unit_name, _uid, _root: acquisition,
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _promotion_context(root: Path) -> deploy_release.PromotionContext:
    return deploy_release.PromotionContext(
        source_environment="staging",
        desired_ref="deploy/staging",
        desired_revision="b" * 40,
        observed_ref="observed/staging",
        observed_revision="c" * 40,
        specification_revision="a" * 40,
        desired_root=root,
    )


def test_reconcile_parser_exposes_plan_without_a_dry_alias():
    parser = deploy_release.build_parser()

    args = parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--plan"])
    assert args.plan is True
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--dry"])


def test_dry_plan_uses_artifact_fallback_when_observation_is_unavailable(tmp_path):
    template = {
        "image": {
            "fromArtifact": {
                "unit": "application-images",
                "name": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "pointer": "/images/control/uri",
                "dryFallback": "preview.invalid/control@sha256:" + "0" * 64,
            },
        }
    }
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"

    resolution = deploy_release.resolve_template(template, candidate, observed, None, dry=True)

    assert resolution.value == {"image": "preview.invalid/control@sha256:" + "0" * 64}
    assert resolution.artifacts == {}


def test_observation_reference_materializes_artifact_into_consumer(tmp_path):
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    producer = candidate / "units/application-images.json"
    unit = {
        "name": "application-images",
        "driver": "oci-images",
        "source": {
            "path": ".",
            "revision": "a" * 40,
            "driverVersion": 1,
            "inputHash": "sha256:" + "2" * 64,
        },
    }
    _write_json(producer, unit)
    artifact_path = observed / "artifacts/application-images/containers.yaml"
    _write_json(
        artifact_path,
        {
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "metadata": {"name": "containers"},
            "producer": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "OciImages",
                "name": "application-images",
                "driverVersion": 1,
                "sourceRevision": "a" * 40,
                "inputHashVersion": 1,
                "inputHash": "sha256:" + "2" * 64,
            },
            "images": {"control": {"uri": "registry.example/control@sha256:" + "1" * 64}},
        },
    )
    _write_json(
        observed / "units/application-images.json",
        receipt_document(
            "oci-images",
            "application-images",
            {"unitBlob": deploy_release.file_blob(producer)},
            artifacts={
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": "artifacts/application-images/containers.yaml",
                    "digest": deploy_release.sha256_file(artifact_path),
                    "mediaType": "application/vnd.gitopsctr.container-images.v1+yaml",
                }
            },
        ),
    )

    resolution = deploy_release.resolve_template(
        {
            "image": {
                "fromArtifact": {
                    "unit": "application-images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "pointer": "/images/control/uri",
                },
            }
        },
        candidate,
        observed,
        "a" * 40,
    )

    assert resolution.value == {"image": "registry.example/control@sha256:" + "1" * 64}
    assert resolution.promotions == {}
    assert resolution.receipts == {}
    assert resolution.artifacts == {"application-images/containers": deploy_release.sha256_file(artifact_path)}

    with pytest.raises(deploy_release.ReferenceUnavailable, match="not artifact.gitopsctr.io/v1/FrontendBundle"):
        deploy_release.resolve_template(
            {
                "fromArtifact": {
                    "unit": "application-images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "FrontendBundle",
                }
            },
            candidate,
            observed,
            "a" * 40,
        )


def test_receipt_reference_normalizes_resource_receipt_before_applying_pointer(tmp_path):
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    producer = candidate / "units/infrastructure.yaml"
    producer.parent.mkdir(parents=True)
    producer.write_text("name: infrastructure\ndriver: terraform\nsource:\n  path: infra/deploy\n")
    receipt_path = observed / "units/infrastructure.yaml"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            receipt_document(
                "terraform",
                "infrastructure",
                {"revision": "d" * 40, "unitBlob": deploy_release.file_blob(producer)},
                {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://example.invalid"}},
                resolved_inputs={},
                controller={
                    "version": "0.1.0",
                    "revision": "a" * 40,
                    "observed_at": "2026-08-08T00:00:00Z",
                },
            )
        )
    )

    resolution = deploy_release.resolve_template(
        {
            "url": {
                "fromReceipt": {"unit": "infrastructure", "pointer": "/outputs/url"},
            }
        },
        candidate,
        observed,
        "b" * 40,
    )

    assert resolution.value == {"url": "https://example.invalid"}
    assert resolution.promotions == {}
    assert resolution.receipts == {"infrastructure": deploy_release.file_blob(receipt_path)}
    assert resolution.artifacts == {}


def test_promotion_reference_reads_typed_resource_spec_before_applying_pointer(tmp_path):
    promotion = tmp_path / "promotion"
    source_unit = promotion / "units/aws-application.yaml"
    source_unit.parent.mkdir(parents=True)
    source_unit.write_text(
        json.dumps(
            deploy_release.serialize_unit_document(
                deploy_release.parse_desired_unit_document(
                    {
                        "name": "aws-application",
                        "driver": "terraform",
                        "source": {"path": "infra"},
                        "terraform": {
                            "variables": {"control_image_uri": "registry.example/control@sha256:" + "1" * 64}
                        },
                    },
                    "aws-application",
                ).with_metadata(ResourceMetadata.new_source_tracked("aws-application"))
            )
        )
    )

    resolution = deploy_release.resolve_template(
        {
            "image": {
                "fromPromotion": {
                    "unit": "aws-application",
                    "pointer": "/terraform/variables/control_image_uri",
                },
            }
        },
        tmp_path / "candidate",
        tmp_path / "observed",
        None,
        promotion=_promotion_context(promotion),
    )

    assert resolution.value == {"image": "registry.example/control@sha256:" + "1" * 64}
    assert resolution.promotions == {
        "aws-application#/terraform/variables/control_image_uri": deploy_release.file_blob(source_unit)
    }
    assert resolution.receipts == {}
    assert resolution.artifacts == {}


def test_unknown_unit_fails_before_advancing_desired_state(monkeypatch):
    def fake_git(*args, **_kwargs):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "")
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


@pytest.mark.parametrize("plan", [False, True])
def test_reconcile_source_revision_requests_dirty_source_warning(monkeypatch, plan):
    warnings = []

    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "warn_if_source_revision_excludes_changes", warnings.append)
    if plan:
        monkeypatch.setattr(
            deploy_release,
            "materialize_revision",
            lambda *_args: (_ for _ in ()).throw(deploy_release.OperationError("stop after warning")),
        )
    else:
        monkeypatch.setattr(
            deploy_release,
            "advance_desired",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(deploy_release.OperationError("stop after warning")),
        )

    with pytest.raises(deploy_release.OperationError, match="stop after warning"):
        deploy_release.command_reconcile(
            Namespace(
                unit="aws-application",
                environment="dev",
                desired_ref="deploy/dev",
                desired_revision=None,
                observed_ref="observed/dev",
                plan=plan,
                report=None,
                source_revision="HEAD",
                advance=not plan,
                require_source_ref=None,
            )
        )

    assert warnings == ["a" * 40]


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


def test_reconcile_reports_persisted_transition_reason_before_unit_materialization(monkeypatch, capsys):
    outputs = []

    def fake_observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        return "b" * 40

    def fake_materialize(_revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        _write_json(
            output / ".gitopsctr/transition-blocks.json",
            {"schema": 1, "blocks": {"frontend": "persisted opaque cleanup reason"}},
        )

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
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
    assert "WAIT     persisted opaque cleanup reason" in capsys.readouterr().err


def test_planned_reconcile_executes_clean_unit_and_passes_report(tmp_path, monkeypatch):
    report = tmp_path / "report"
    calls = []

    def fake_observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "observed/dev":
            _write_json(
                output / "units/aws-application.json",
                receipt_document("terraform", "aws-application", {"unitBlob": "same-unit"}),
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


@pytest.mark.parametrize("plan_action", [None, "refresh"])
def test_planned_reconcile_applies_plan_source_policy(tmp_path, monkeypatch, capsys, plan_action):
    source_revision = "b" * 40
    previous_revision = "a" * 40
    planned = []

    specification = {
        "schema": 1,
        "name": "aws-application",
        "driver": "terraform",
        "source": {"path": "."},
    }
    previous = {
        "schema": 1,
        "name": "aws-application",
        "driver": "terraform",
        "source": {
            "path": ".",
            "revision": previous_revision,
            "inputHash": "sha256:same",
            "driverVersion": 2,
        },
    }

    def fake_git(*args, **_kwargs):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, source_revision + "\n", "")
        if args[0] == "cat-file":
            return subprocess.CompletedProcess(args, 1, "", "missing")
        raise AssertionError(args)

    def fake_materialize(revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if revision == source_revision:
            project_spec = (
                {}
                if plan_action is None
                else {
                    "sourceRevisionPolicy": {
                        "unavailableWhen": "outside-candidate-history",
                        "whenUnavailableDuringAdvance": "refresh",
                        "whenUnavailableDuringPlan": plan_action,
                    }
                }
            )
            _write_json(
                output / "gitopsctr.yaml",
                {
                    "apiVersion": "gitopsctr.io/v1",
                    "kind": "Project",
                    "metadata": {"name": "example"},
                    "spec": project_spec,
                },
            )
            _write_json(output / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
            _write_json(output / "deployment/environments/dev/units/aws-application.json", specification)

    def fake_observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/dev":
            _write_json(output / "units/aws-application.json", previous)
            return "c" * 40
        return None

    monkeypatch.setattr(deploy_release, "load_environment", lambda *_args: {"promotion": None})
    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "deployment_refs",
        lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"),
    )
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "resolve_advance_source_revision", lambda *_args: source_revision)
    monkeypatch.setattr(deploy_release, "warn_if_source_revision_excludes_changes", lambda *_args: None)
    monkeypatch.setattr(deploy_release, "file_blob", lambda path: str(path))
    monkeypatch.setitem(
        deploy_release.PLANNING_DRIVERS,
        "terraform",
        SimpleNamespace(plan=lambda context: planned.append(context)),
    )

    command_args = Namespace(
        unit="aws-application",
        environment="dev",
        desired_ref="deploy/dev",
        desired_revision=None,
        observed_ref="observed/dev",
        plan=True,
        report=None,
        source_revision="HEAD",
        advance=True,
        require_source_ref=None,
    )

    if plan_action is None:
        with pytest.raises(deploy_release.SourceRevisionUnavailableError, match="unavailable under project policy"):
            deploy_release.command_reconcile(command_args)
        assert planned == []
        return

    deploy_release.command_reconcile(command_args)

    assert len(planned) == 1
    output = capsys.readouterr().err
    assert (
        "REFRESH  aws-application: retained source aaaaaaaaaaaa is unavailable; use bbbbbbbbbbbb "
        "in the dry candidate only"
    ) in output
    assert "PLAN     terraform planning succeeded" in output


def test_clean_reconcile_with_advance_finishes_pending_desired_convergence(tmp_path, monkeypatch):
    advances = []
    outputs = []

    def fake_observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "observed/dev":
            _write_json(
                output / "units/application-images.json",
                receipt_document("terraform", "application-images", {"unitBlob": "same-unit"}),
            )
            return "b" * 40
        return None

    def fake_materialize(_revision: str, output: Path):
        _write_json(
            output / "units/application-images.json",
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": ResourceMetadata.source_tracked_from_provenance(
                    "application-images", "reconcile-clean-test"
                ).document(profile="desired"),
                "spec": {"source": {"path": ".", "revision": "a" * 40}},
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
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "same-unit")
    monkeypatch.setattr(deploy_release, "advance_desired", fake_advance)
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda reconciled, desired="": outputs.append((reconciled, desired)),
    )
    monkeypatch.setitem(
        deploy_release.RECONCILIATION_DRIVERS,
        "terraform",
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
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "")
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
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "metadata": ResourceMetadata.source_tracked_from_provenance(
                        "aws-application", "reconcile-advance-test"
                    ).document(profile="desired"),
                    "spec": {"source": {"path": "infra/deploy", "revision": "e" * 40}},
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
        return ReconciliationOutput(
            result=TerraformResultModel(
                applied=AppliedTerraformModel(sourceRevision=context.source_revision),
                outputs={},
            )
        )

    def fake_publish(*_args, **_kwargs):
        events.append(("receipt",))
        return "f" * 40

    def unexpected_resolve(*_args):
        raise AssertionError("pre-advanced reconciliation re-resolved desired head")

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(deploy_release, "advance_desired", fake_advance)
    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "d" * 40)
    monkeypatch.setattr(deploy_release, "resolve_ref", unexpected_resolve)
    monkeypatch.setattr(deploy_release, "file_blob", lambda _path: "unit-blob")
    monkeypatch.setattr(deploy_release, "publish_observation_cas", fake_publish)
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
