"""Driver-neutral deployment verification and explicit reconciliation reapplication."""

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller as deploy_release
from gitopsctr.contrib.drivers.terraform import AppliedTerraformModel, TerraformResultModel
from gitopsctr.document import JsonObjectValue
from gitopsctr.driver import ReconciliationOutput, VerificationContext, VerificationResult, VerificationStatus
from gitopsctr.resources import ResourceMetadata, UnitResource
from tests.conftest import receipt_document
from tests.stack_deletion_support import stack_tree


@pytest.fixture(autouse=True)
def _local_effect_lease(monkeypatch):
    def acquire(_ref, revision, unit_name, uid, **_kwargs):
        lease = deploy_release.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="lease-test",
            owner="test-runner",
            desired_revision=revision,
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
        lambda _ref, acquisition, _unit_name, _uid, _root, **_kwargs: acquisition,
    )


DESIRED_REVISION = "d" * 40


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _unit(name: str, driver: str, revision: str) -> UnitResource:
    installed = deploy_release.UNIT_DRIVERS[driver]
    specification: dict[str, object] = {
        "source": {
            "path": f"deployment/{name}",
            "revision": revision,
            "inputHash": f"sha256:{name}",
            "driverVersion": installed.version,
        },
        "inputs": {"environment": "prod"},
    }
    if driver == "terraform":
        specification["terraform"] = {"backend": {}, "variables": {}, "observeOutputs": []}
    unit = deploy_release.parse_desired_unit_document(
        {
            "apiVersion": installed.api_version,
            "kind": installed.kind,
            "metadata": ResourceMetadata.root_from_provenance(name, f"verify:{name}", partition="application").document(
                profile="desired"
            ),
            "spec": specification,
        },
        name,
    )
    return unit


def _install_desired_state(monkeypatch, units: list[UnitResource]) -> list[str]:
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
                _write_json(
                    output / "units" / f"{unit.name}.json",
                    deploy_release.serialize_unit_document(unit),
                )

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    for name in ("observed_tree", "publish_observation_cas", "write_json"):
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
            calls.append((context.unit_name, context))
            return VerificationResult(status)

        return verify

    monkeypatch.setitem(
        deploy_release.VERIFICATION_DRIVERS,
        "oci-images",
        SimpleNamespace(verify=verifier(VerificationStatus.DRIFT)),
    )
    monkeypatch.setitem(
        deploy_release.VERIFICATION_DRIVERS,
        "terraform",
        SimpleNamespace(verify=verifier(VerificationStatus.CLEAN)),
    )

    with pytest.raises(deploy_release.OperationError, match="detected drift in: images"):
        deploy_release.command_verify(Namespace(environment="prod", unit=None))

    assert [name for name, _ in calls] == ["images", "infrastructure"]
    assert materialized == [DESIRED_REVISION, "a" * 40, "b" * 40]
    assert calls[0][1].source_revision == "a" * 40
    assert calls[0][1].source_path == "deployment/images"
    assert calls[0][1].unit.inputs == {"environment": "prod"}
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
        deploy_release.VERIFICATION_DRIVERS,
        "terraform",
        SimpleNamespace(
            verify=lambda context: calls.append(context.unit_name) or VerificationResult(VerificationStatus.CLEAN)
        ),
    )

    deploy_release.command_verify(Namespace(environment="prod", unit=["infrastructure", "infrastructure"]))

    assert calls == ["infrastructure"]
    assert materialized == [DESIRED_REVISION, "b" * 40]
    assert "RESULT   CLEAN" in capsys.readouterr().err


def test_verify_resolves_stack_owned_unit_storage_path(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    _stack_uid, qualified_name = stack_tree(snapshot)
    calls: list[VerificationContext] = []
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: DESIRED_REVISION)

    def materialize(revision: str, output: Path) -> None:
        if revision == DESIRED_REVISION:
            shutil.copytree(snapshot, output)
        else:
            output.mkdir(parents=True)

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "_hydrate_stack_workload_pin_for_unit", lambda *_args: None)
    monkeypatch.setitem(
        deploy_release.VERIFICATION_DRIVERS,
        "terraform",
        SimpleNamespace(verify=lambda context: calls.append(context) or VerificationResult(VerificationStatus.CLEAN)),
    )

    deploy_release.command_verify(Namespace(environment="dev", unit=[qualified_name]))

    assert len(calls) == 1
    assert calls[0].unit_name == "preview-app"
    assert calls[0].qualified_name == qualified_name


def test_verify_preflights_unsupported_drivers_before_running_any_unit(monkeypatch):
    units = [
        _unit("infrastructure", "terraform", "a" * 40),
        _unit("images", "oci-images", "b" * 40),
    ]
    materialized = _install_desired_state(monkeypatch, units)
    monkeypatch.setitem(
        deploy_release.VERIFICATION_DRIVERS,
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
    unit = unit.with_spec(
        replace(
            unit.spec,
            inputs=JsonObjectValue(
                {
                    "image": {
                        "fromReceipt": {"unit": "images", "pointer": "/image"},
                    }
                }
            ),
        )
    )
    materialized = _install_desired_state(monkeypatch, [unit])
    monkeypatch.setitem(
        deploy_release.VERIFICATION_DRIVERS,
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
    publications = []
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
            receipt_document(
                "terraform",
                "infrastructure",
                {"revision": DESIRED_REVISION, "unitContentId": "sha256:" + "a" * 64},
                {"applied": {"sourceRevision": "a" * 40}, "outputs": {}},
                resolved_inputs={},
                controller={"revision": "c" * 40},
            ),
        )
        return "o" * 40

    def materialize(revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if revision == DESIRED_REVISION:
            _write_json(
                output / "units/infrastructure.json",
                deploy_release.serialize_unit_document(unit),
            )

    def driver(context):
        calls.append(context.unit_name)
        return ReconciliationOutput(
            result=TerraformResultModel(
                applied=AppliedTerraformModel(sourceRevision=context.source_revision),
                outputs={},
            )
        )

    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: DESIRED_REVISION)
    monkeypatch.setattr(deploy_release, "unit_content_id", lambda *_args: "sha256:" + "a" * 64)
    monkeypatch.setattr(deploy_release, "controller_evidence", lambda: {"revision": "c" * 40})
    monkeypatch.setattr(
        deploy_release,
        "publish_observation_cas",
        lambda _ref, _unit, receipt, _desired, _artifacts, _revision, **_kwargs: (
            publications.append(receipt) or "p" * 40
        ),
    )
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda changed, desired="": outputs.append((changed, desired)),
    )
    monkeypatch.setitem(
        deploy_release.RECONCILIATION_DRIVERS,
        "terraform",
        SimpleNamespace(reconcile=driver, result_contract=deploy_release.UNIT_DRIVERS["terraform"].result_contract),
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
            reapply=reapply,
        )
    )

    assert calls == ["infrastructure"] * driver_runs
    assert len(publications) == driver_runs
    if publications:
        serialized = deploy_release.RESOURCE_CATALOG.serialize_receipt(publications[0])
        assert serialized["$schema"] == deploy_release.resource_schema_url(
            "unit.gitopsctr.io/v1", "Terraform", "receipt"
        )
    assert changed is bool(driver_runs)
    assert outputs == [(bool(driver_runs), "")]
