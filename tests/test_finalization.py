"""Durable source-tracked deletion intents and fenced finalization."""

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller as deploy_release
from gitopsctr.contracts import DesiredOwnerReference, DesiredSource
from gitopsctr.driver import TeardownResult
from gitopsctr.errors import OperationError
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
        lambda _ref, acquisition, unit_name, uid, root: (
            replace(
                acquisition,
                lease=replace(
                    acquisition.lease,
                    snapshot=deploy_release.effect_lease_snapshot(root, unit_name, uid),
                ),
            )
            if acquisition.lease.snapshot is None
            else acquisition
        ),
    )
    monkeypatch.setattr(deploy_release, "validate_effect_lease_head", lambda _ref, *_args: "c" * 40)


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _terraform_unit(
    name: str,
    uid: str,
    source_revision: str = "a" * 40,
    owner: DesiredOwnerReference | None = None,
    resolved_inputs: dict[str, object] | None = None,
) -> dict[str, object]:
    metadata = deploy_release.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=(
            deploy_release.DesiredLifecycle(owner=owner)
            if owner is not None
            else deploy_release.DesiredLifecycle(management=deploy_release.LifecycleManagement(mode="sourceTracked"))
        ),
    )
    specification: dict[str, object] = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": source_revision,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }
    if resolved_inputs is not None:
        specification["spec"]["resolvedInputs"] = resolved_inputs  # type: ignore[index]
    return specification


def _vite_unit(name: str, uid: str) -> dict[str, object]:
    metadata = deploy_release.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=deploy_release.DesiredLifecycle(management=deploy_release.LifecycleManagement(mode="sourceTracked")),
    )
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "ViteOciBundle",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "frontend",
                "revision": "a" * 40,
                "inputHash": "sha256:" + "2" * 64,
                "driverVersion": deploy_release.DRIVER_VERSIONS["vite-oci-bundle"],
            },
            "build": {"nodeVersion": "24"},
            "publish": {"repository": "registry.example/frontend"},
        },
    }


def _source_root(root: Path) -> None:
    _write_json(root / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})


def _finalize_args(unit: str = "application", **overrides: object) -> Namespace:
    values = {
        "unit": unit,
        "environment": "dev",
        "desired_ref": "deploy/dev",
        "observed_ref": "observed/dev",
        "candidate_ref": None,
        "uid": "d1-application",
        "deletion_generation": 1,
        "report": None,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _recover_opaque_args(unit: str = "application", **overrides: object) -> Namespace:
    values = {
        "unit": unit,
        "environment": "dev",
        "desired_ref": "deploy/dev",
        "candidate_ref": None,
        "uid": "d1-application",
        "source_revision": "b" * 40,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _prepare_opaque_recovery(
    tmp_path: Path,
    monkeypatch,
    payload: dict[str, object],
    *,
    source_unit: dict[str, object] | None = None,
    intent: deploy_release.UnitDeletionIntent | None = None,
):
    current = tmp_path / "current"
    source = tmp_path / "source"
    current.mkdir()
    _source_root(source)
    deploy_release.write_opaque_cleanup_root(
        current,
        "application",
        deploy_release.OpaqueCleanupRoot(
            path=current / ".gitopsctr/cleanup/units/application.json",
            payload=payload,
            metadata=deploy_release.ResourceMetadata(
                name="application",
                uid="d1-application",
                lifecycle=deploy_release.DesiredLifecycle(
                    management=deploy_release.LifecycleManagement(mode="sourceTracked")
                ),
            ),
            source=deploy_release.raw_document_source(payload),
        ),
    )
    if intent is not None:
        deploy_release.write_deletion_intent(current, intent)
    if source_unit is not None:
        _write_json(source / "deployment/environments/dev/units/application.json", source_unit)

    published: list[Path] = []

    def observed_tree(_ref: str, output: Path):
        shutil.copytree(current, output)
        return "c" * 40

    def materialize_revision(_revision: str, output: Path):
        shutil.copytree(source, output)

    def publish_desired_change(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize_revision)
    monkeypatch.setattr(deploy_release, "publish_desired_change", publish_desired_change)
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    return current, source, published


def test_recover_opaque_source_absent_restores_unit_and_creates_intent(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    _current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, payload)

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args(source_revision="a" * 40)) is True

    candidate = published[0]
    restored = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert restored.metadata.uid == "d1-application"
    assert intent.uid == "d1-application"
    assert intent.deletion_generation == 2
    assert not deploy_release.load_desired_cleanup_roots(candidate)


def test_recover_opaque_source_present_restores_active_source_tracked_unit(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    source_unit = json.loads(json.dumps(payload))
    source_unit["metadata"] = {"name": "application"}
    source_unit["spec"]["source"] = {"path": "infra/deploy"}
    _current, _source, published = _prepare_opaque_recovery(
        tmp_path,
        monkeypatch,
        payload,
        source_unit=source_unit,
    )

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args(source_revision="a" * 40)) is True

    candidate = published[0]
    restored = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    assert restored.metadata.uid == "d1-application"
    assert restored.metadata.lifecycle is not None
    assert restored.metadata.lifecycle.management is not None
    assert restored.metadata.lifecycle.management.mode == "sourceTracked"
    assert deploy_release.load_desired_deletion_intents(candidate) == {}
    assert deploy_release.load_desired_unit_incarnation_tombstones(candidate)["application"].state == "active"


def test_recover_opaque_source_transition_restores_old_unit_with_intent(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    vite = deploy_release.parse_desired_unit_document(_vite_unit("application", "new-vite"), "application")
    source_unit = deploy_release.serialize_unit_document(vite, profile="desired")
    source_unit["metadata"] = {"name": "application"}
    source_unit["spec"]["source"] = {"path": "frontend"}
    source_unit["spec"].pop("inputs", None)
    source_unit["spec"].pop("resolvedInputs", None)
    _current, _source, published = _prepare_opaque_recovery(
        tmp_path,
        monkeypatch,
        payload,
        source_unit=source_unit,
    )

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args()) is True

    candidate = published[0]
    restored = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert restored.driver_name == "terraform"
    assert intent.uid == "d1-application"
    assert intent.deletion_generation == 2


def test_recover_opaque_existing_intent_preserves_uid_and_generation(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    retained = tmp_path / "retained/units/application.yaml"
    _write_json(retained, payload)
    original = deploy_release.load_desired_unit(retained, "application")
    deploy_release.write_desired_candidate_unit(retained, original, tmp_path / "retained")
    original = deploy_release.load_desired_unit(retained, "application")
    canonical_payload = deploy_release.serialize_unit_document(original, profile="desired")
    intent = deploy_release.UnitDeletionIntent.from_unit(original, retained, tmp_path / "retained", 7)
    current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, canonical_payload, intent=intent)

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args()) is True

    candidate = published[0]
    recovered_intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert recovered_intent.uid == intent.uid
    assert recovered_intent.deletion_generation == 7
    assert (
        deploy_release.load_desired_unit(
            deploy_release.unit_document_path(candidate, "application"), "application"
        ).metadata.uid
        == intent.uid
    )
    assert not deploy_release.load_desired_cleanup_roots(candidate)
    assert deploy_release.load_desired_cleanup_roots(current)


def test_recover_opaque_migrates_generation_one_intent_before_recovery(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    retained = tmp_path / "retained/units/application.yaml"
    _write_json(retained, payload)
    original = deploy_release.load_desired_unit(retained, "application")
    deploy_release.write_desired_candidate_unit(retained, original, tmp_path / "retained")
    original = deploy_release.load_desired_unit(retained, "application")
    canonical_payload = deploy_release.serialize_unit_document(original, profile="desired")
    intent = deploy_release.UnitDeletionIntent.from_unit(original, retained, tmp_path / "retained", 1)
    _current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, canonical_payload, intent=intent)

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args()) is True

    candidate = published[0]
    recovered = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert recovered.deletion_generation == 2
    assert deploy_release.load_desired_unit_incarnation_tombstones(candidate)["application"] == (
        deploy_release.UnitIncarnationTombstone(
            unit_name="application",
            uid="d1-application",
            state="active",
            next_deletion_generation=2,
        )
    )


def test_recover_opaque_rejects_changed_payload_against_immutable_intent_blob(tmp_path, monkeypatch):
    original_payload = _terraform_unit("application", "d1-application")
    retained = tmp_path / "retained/units/application.yaml"
    _write_json(retained, original_payload)
    original = deploy_release.load_desired_unit(retained, "application")
    deploy_release.write_desired_candidate_unit(retained, original, tmp_path / "retained")
    original = deploy_release.load_desired_unit(retained, "application")
    canonical_payload = deploy_release.serialize_unit_document(original, profile="desired")
    intent = deploy_release.UnitDeletionIntent.from_unit(original, retained, tmp_path / "retained", 7)
    changed_payload = json.loads(json.dumps(canonical_payload))
    changed_payload["spec"]["terraform"]["variables"] = {"changed": "after-teardown"}  # type: ignore[index]
    _current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, changed_payload, intent=intent)

    with pytest.raises(OperationError, match="immutable retained_unit_blob"):
        deploy_release.command_recover_opaque_unit(_recover_opaque_args())
    assert not published


def test_recover_opaque_blocks_stale_same_identity_source_payload(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application", source_revision="a" * 40)
    source_unit = json.loads(json.dumps(payload))
    source_unit["metadata"] = {"name": "application"}
    source_unit["spec"]["source"] = {"path": "infra/deploy"}
    current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, payload, source_unit=source_unit)

    with pytest.raises(OperationError, match="authoritative source.*changed"):
        deploy_release.command_recover_opaque_unit(_recover_opaque_args(source_revision="b" * 40))
    assert not published
    assert deploy_release.load_desired_cleanup_roots(current)["application"].metadata.uid == "d1-application"


def test_recover_opaque_adopts_parseable_legacy_payload_with_outer_uid(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    payload["metadata"] = {"name": "application"}
    source_unit = json.loads(json.dumps(payload))
    source_unit["spec"]["source"] = {"path": "infra/deploy"}
    _current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, payload, source_unit=source_unit)

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args()) is True

    restored = deploy_release.load_desired_unit(published[0] / "units/application.json", "application")
    assert not restored.is_legacy_compatibility
    assert restored.metadata.uid == "d1-application"


def test_recover_opaque_fences_dry_run_uid_and_active_lease(tmp_path, monkeypatch):
    payload = _terraform_unit("application", "d1-application")
    current, _source, published = _prepare_opaque_recovery(tmp_path, monkeypatch, payload)

    assert deploy_release.command_recover_opaque_unit(_recover_opaque_args(dry=True)) is False
    assert not published
    assert deploy_release.load_desired_cleanup_roots(current)["application"].metadata.uid == "d1-application"

    with pytest.raises(OperationError, match="opaque cleanup UID fence"):
        deploy_release.command_recover_opaque_unit(_recover_opaque_args(uid="d1-other"))

    with pytest.raises(OperationError, match="authoritative full"):
        deploy_release.command_recover_opaque_unit(_recover_opaque_args(source_revision="main"))

    deploy_release.write_effect_lease(
        current,
        deploy_release.EffectLease(
            unit_name="application",
            uid="d1-application",
            token="lease-test",
            owner="test",
            desired_revision="c" * 40,
            expires_at=None,
        ),
    )
    with pytest.raises(OperationError, match="active effect lease"):
        deploy_release.command_recover_opaque_unit(_recover_opaque_args())


def _copy_observed_files(files: dict[str, bytes]):
    def observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return "c" * 40

    return observed_tree


def test_source_absence_publishes_durable_deletion_intent_and_retries(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _source_root(source)
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    observed.mkdir()

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, observed, None, candidate, verbose=False
    )

    intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert result.blocked["application"] == deploy_release.deletion_intent_reason(intent)
    assert intent.uid == "d1-application"
    assert intent.deletion_generation == 2
    assert intent.retained_source == DesiredSource(
        path="infra/deploy",
        revision="a" * 40,
        inputHash="sha256:" + "1" * 64,
        driverVersion=deploy_release.DRIVER_VERSIONS["terraform"],
    )
    assert deploy_release.unit_document_path(candidate, "application").is_file()
    assert deploy_release.reconciliation_statuses([], candidate, observed) == [
        ("application", "WAIT", deploy_release.deletion_intent_reason(intent))
    ]

    deploy_release.build_desired_candidate("dev", source, "c" * 40, candidate, observed, None, repeated, verbose=False)

    assert (
        deploy_release.unit_document_path(repeated, "application").read_bytes()
        == deploy_release.unit_document_path(candidate, "application").read_bytes()
    )
    assert (repeated / ".gitopsctr/deletion-intents/units/application.json").read_bytes() == (
        candidate / ".gitopsctr/deletion-intents/units/application.json"
    ).read_bytes()
    assert deploy_release.load_desired_deletion_intents(repeated)["application"].deletion_generation == 2


def test_repeated_advance_migrates_an_existing_generation_one_intent_once(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _source_root(source)
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, tmp_path / "observed", None, candidate, verbose=False
    )

    migrated = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert migrated.deletion_generation == 2
    assert deploy_release.load_desired_unit_incarnation_tombstones(candidate)["application"] == (
        deploy_release.UnitIncarnationTombstone(
            unit_name="application",
            uid=intent.uid,
            state="active",
            next_deletion_generation=2,
        )
    )

    deploy_release.build_desired_candidate(
        "dev", source, "c" * 40, candidate, tmp_path / "observed", None, repeated, verbose=False
    )
    assert deploy_release.load_desired_deletion_intents(repeated)["application"] == migrated
    assert (
        deploy_release.load_desired_unit_incarnation_tombstones(repeated)["application"]
        == (deploy_release.load_desired_unit_incarnation_tombstones(candidate)["application"])
    )


def test_finalize_tears_down_then_publishes_absent_state_and_is_idempotent(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    deploy_release.write_desired_transition_blocks(
        current, {"application": deploy_release.deletion_intent_reason(intent)}
    )

    desired_files = deploy_release.directory_files(current)
    publications: list[dict[str, object]] = []
    teardown_calls = []

    def observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/dev":
            for relative, content in desired_files.items():
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return "c" * 40
        _write_json(
            output / "units/application.json",
            receipt_document(
                "terraform",
                "application",
                {"unitBlob": deploy_release.file_blob(unit_path), "revision": "c" * 40},
                {"applied": {"sourceRevision": "c" * 40}, "outputs": {}},
            ),
        )
        artifact = output / "artifacts/application/output.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("stale artifact")
        return None

    def publish_tree(ref: str, directory: Path, parent: str, message: str):
        publications.append(
            {"ref": ref, "parent": parent, "message": message, "files": deploy_release.directory_files(directory)}
        )
        return "d" * 40

    def materialize_revision(_revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"uid": context.resource_uid, "generation": context.deletion_generation})

    driver_type = type(deploy_release.UNIT_DRIVERS["terraform"])
    monkeypatch.setattr(driver_type, "teardown", teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize_revision)
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(deploy_release, "publish_tree", publish_tree)

    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is True
    assert len(teardown_calls) == 1
    assert teardown_calls[0].resource_uid == intent.uid
    assert teardown_calls[0].deletion_generation == 1
    assert teardown_calls[0].previous_receipt is not None
    assert teardown_calls[0].previous_receipt.spec.desired.unitBlob == deploy_release.file_blob(unit_path)
    evidence_files = publications[0]["files"]
    assert ".gitopsctr/teardowns/units/application.d1-application.1.json" in evidence_files
    evidence = json.loads(evidence_files[".gitopsctr/teardowns/units/application.d1-application.1.json"].decode())
    assert evidence["details"] == {"uid": intent.uid, "generation": 1}
    assert "units/application.json" not in evidence_files
    assert "artifacts/application/output.txt" not in evidence_files
    files = publications[-1]["files"]
    assert "units/application.json" not in files
    assert ".gitopsctr/deletion-intents/units/application.json" not in files
    assert ".gitopsctr/effect-leases/units/application.json" not in files
    assert json.loads(files[".gitopsctr/incarnations/units/application.json"].decode()) == {
        "schema": 1,
        "kind": "UnitIncarnationTombstone",
        "unitName": "application",
        "uid": intent.uid,
    }
    assert ".gitopsctr/transition-blocks.json" not in files
    assert capsys.readouterr().out == "d" * 40 + "\n"

    desired_files = files
    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is False
    assert len(teardown_calls) == 1


def test_finalize_source_materialization_failure_does_not_acquire_effect_lease(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, _output: (_ for _ in ()).throw(OperationError("source checkout failed")),
    )
    monkeypatch.setattr(
        deploy_release,
        "acquire_effect_lease",
        lambda *_args, **_kwargs: pytest.fail("pre-effect source failure acquired a lease"),
    )

    assert deploy_release.command_finalize(_finalize_args()) is False
    assert "retained source is unavailable" in capsys.readouterr().err


def test_finalize_releases_lease_when_local_lease_materialization_fails(tmp_path, monkeypatch):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    released: list[deploy_release.EffectLeaseAcquisition] = []

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, output: output.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        deploy_release,
        "write_effect_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("local lease write failed")),
    )
    monkeypatch.setattr(
        deploy_release, "release_pre_effect_lease", lambda _ref, acquisition: released.append(acquisition)
    )

    with pytest.raises(OperationError, match="local lease write failed"):
        deploy_release.command_finalize(_finalize_args())
    assert len(released) == 1
    assert released[0].lease.uid == intent.uid


def test_finalize_releases_lease_when_heartbeat_startup_fails(tmp_path, monkeypatch):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    released: list[deploy_release.EffectLeaseAcquisition] = []

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, output: output.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        deploy_release,
        "start_effect_lease_heartbeat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("heartbeat unavailable")),
    )
    monkeypatch.setattr(
        deploy_release, "release_pre_effect_lease", lambda _ref, acquisition: released.append(acquisition)
    )

    with pytest.raises(OperationError, match="heartbeat unavailable"):
        deploy_release.command_finalize(_finalize_args())
    assert len(released) == 1
    assert released[0].lease.uid == intent.uid


def test_finalize_blocks_unsupported_driver_and_retains_intent(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _vite_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is False
    assert deploy_release.load_desired_deletion_intents(current)["application"].uid == intent.uid
    assert "does not support teardown" in capsys.readouterr().err


def test_finalize_failure_keeps_intent_for_retry(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    def observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        for relative, content in deploy_release.directory_files(current).items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return "c" * 40

    def fail_teardown(_driver, _context):
        raise deploy_release.DriverError("destroy failed")

    published: list[dict[str, object]] = []

    monkeypatch.setattr(type(deploy_release.UNIT_DRIVERS["terraform"]), "teardown", fail_teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        deploy_release, "materialize_revision", lambda _revision, output: output.mkdir(parents=True, exist_ok=True)
    )
    monkeypatch.setattr(
        deploy_release,
        "publish_teardown_observation_cas",
        lambda *_args, **kwargs: published.append(kwargs),
    )

    assert deploy_release.command_finalize(_finalize_args()) is False
    assert published == []
    assert deploy_release.load_desired_deletion_intents(current)["application"].deletion_generation == 1
    assert "teardown failed: destroy failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [("uid", "d1-stale"), ("deletion_generation", 2)],
)
def test_finalize_rejects_stale_fences(tmp_path, monkeypatch, field, value):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(
        type(deploy_release.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args: pytest.fail("stale deletion intent reached teardown"),
    )

    with pytest.raises(OperationError, match="stale deletion"):
        deploy_release.command_finalize(_finalize_args(**{field: value}))


def test_finalize_requires_both_runtime_fences(tmp_path):
    with pytest.raises(SystemExit):
        deploy_release.build_parser().parse_args(["finalize", "--environment", "dev", "--unit", "application"])
    with pytest.raises(OperationError, match="requires --uid"):
        deploy_release.command_finalize(_finalize_args(uid=None))
    with pytest.raises(OperationError, match="requires --deletion-generation"):
        deploy_release.command_finalize(_finalize_args(deletion_generation=None))


def test_finalize_blocks_active_owned_children_before_teardown(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
        ),
    )
    parent = deploy_release.load_desired_unit(parent_path, "parent")
    intent = deploy_release.UnitDeletionIntent.from_unit(parent, parent_path, current)
    deploy_release.write_deletion_intent(current, intent)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(
        type(deploy_release.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args: pytest.fail("parent teardown ran with an active child"),
    )

    assert (
        deploy_release.command_finalize(_finalize_args(unit="parent", uid=intent.uid, deletion_generation=1)) is False
    )
    assert "active owned/dependent Units must be finalized first: child" in capsys.readouterr().err


def test_repeated_advance_preserves_opaque_payload_for_mutated_intent(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    _source_root(source)
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    unit_path.write_text("not parseable")

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, tmp_path / "observed", None, candidate, verbose=False
    )

    assert "application" in result.blocked
    assert not deploy_release.unit_document_path(candidate, "application").exists()
    opaque = deploy_release.load_desired_cleanup_roots(candidate)["application"]
    assert opaque.metadata.uid == intent.uid
    assert opaque.payload == "not parseable"
    assert deploy_release.load_desired_deletion_intents(candidate)["application"].uid == intent.uid


def test_rollback_cleanup_merge_carries_current_intent_and_discards_historical_intent(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current_unit_path = current / "units/application.json"
    historical_unit_path = candidate / "units/application.json"
    _write_json(current_unit_path, _terraform_unit("application", "d1-current"))
    _write_json(historical_unit_path, _terraform_unit("application", "d1-historical"))
    current_unit = deploy_release.load_desired_unit(current_unit_path, "application")
    historical_unit = deploy_release.load_desired_unit(historical_unit_path, "application")
    current_intent = deploy_release.UnitDeletionIntent.from_unit(current_unit, current_unit_path, current)
    historical_intent = deploy_release.UnitDeletionIntent.from_unit(historical_unit, historical_unit_path, candidate)
    deploy_release.write_deletion_intent(current, current_intent)
    deploy_release.write_deletion_intent(candidate, historical_intent)
    deploy_release.write_desired_transition_blocks(
        current, {"application": deploy_release.deletion_intent_reason(current_intent)}
    )
    deploy_release.write_desired_transition_blocks(candidate, {"application": "historical block"})

    deploy_release.merge_current_cleanup_state(current, candidate)

    merged = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert merged.uid == current_intent.uid
    assert deploy_release.unit_document_path(candidate, "application").read_bytes() == current_unit_path.read_bytes()
    assert deploy_release.load_desired_transition_blocks(candidate)[
        "application"
    ] == deploy_release.deletion_intent_reason(current_intent)


def test_source_absence_creates_owned_child_intent_before_parent_finalize(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    _source_root(source)
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
        ),
    )

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, tmp_path / "observed", None, candidate, verbose=False
    )

    intents = deploy_release.load_desired_deletion_intents(candidate)
    assert set(intents) == {"parent", "child"}
    assert intents["child"].retained_owner is not None
    assert intents["child"].retained_owner.uid == "d1-parent"
    assert result.blocked["parent"] == deploy_release.deletion_intent_reason(intents["parent"])
    assert result.blocked["child"] == deploy_release.deletion_intent_reason(intents["child"])


def test_source_absence_does_not_cascade_through_dependency_only(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    _source_root(source)
    _write_json(current / "units/parent.json", _terraform_unit("parent", "d1-parent"))
    _write_json(
        current / "units/child.json",
        _terraform_unit("child", "d1-child"),
    )
    _write_json(
        source / "deployment/environments/dev/units/child.json",
        {
            "schema": 1,
            "name": "child",
            "driver": "terraform",
            "source": {"path": "."},
            "terraform": {
                "backend": {},
                "variables": {
                    "parent": {
                        "fromReceipt": {"unit": "parent", "pointer": "/outputs/value"},
                    }
                },
                "observeOutputs": [],
            },
        },
    )

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, tmp_path / "observed", None, candidate, verbose=False
    )

    intents = deploy_release.load_desired_deletion_intents(candidate)
    assert set(intents) == {"parent"}
    assert "child" not in result.blocked or "deletion pending finalization" not in result.blocked["child"]


def test_opaque_active_child_intent_blocks_parent_teardown(tmp_path):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
        ),
    )
    child = deploy_release.load_desired_unit(child_path, "child")
    child_intent = deploy_release.UnitDeletionIntent.from_unit(child, child_path, current)
    deploy_release.write_deletion_intent(current, child_intent)
    child_path.unlink()
    deploy_release.write_opaque_cleanup_root(
        current,
        "child",
        deploy_release.OpaqueCleanupRoot(
            path=Path("child.yaml"),
            payload="opaque child payload",
            metadata=deploy_release.ResourceMetadata.source_tracked_from_provenance("child", "opaque-child"),
            source=None,
        ),
    )

    parent = deploy_release.load_desired_unit(parent_path, "parent")
    assert deploy_release.active_teardown_dependents(current, parent) == ("child",)


def test_opaque_child_without_intent_conservatively_blocks_parent(tmp_path):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    deploy_release.write_opaque_cleanup_root(
        current,
        "child",
        deploy_release.OpaqueCleanupRoot(
            path=Path("child.json"),
            payload="opaque child without identity",
            metadata=deploy_release.ResourceMetadata.source_tracked_from_provenance("child", "opaque-no-intent"),
            source=None,
        ),
    )

    parent = deploy_release.load_desired_unit(parent_path, "parent")
    dependents = deploy_release.active_teardown_dependents(current, parent)
    assert dependents == ("child (opaque cleanup root lacks a validated deletion identity)",)


def test_resolved_observation_inputs_preserve_teardown_dependencies_and_ignore_promotions(tmp_path):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            resolved_inputs={
                "receipts": {"parent": "receipt-fingerprint"},
                "artifacts": {"parent/image": "artifact-fingerprint"},
                "promotions": {"parent#/outputs/value": "promotion-fingerprint"},
            },
        ),
    )
    child = deploy_release.load_desired_unit(child_path, "child")
    parent = deploy_release.load_desired_unit(parent_path, "parent")

    assert deploy_release.desired_observation_reference_units(child) == frozenset({"parent"})
    intent = deploy_release.UnitDeletionIntent.from_unit(child, child_path, current)
    assert intent.retained_dependencies == ("parent",)
    deploy_release.write_deletion_intent(current, intent)
    assert deploy_release.active_teardown_dependents(current, parent) == ("child",)


def test_resolved_artifact_dependency_key_fails_closed(tmp_path):
    path = tmp_path / "units/child.json"
    _write_json(
        path,
        _terraform_unit(
            "child",
            "d1-child",
            resolved_inputs={"artifacts": {"parent/not/a-safe-key": "fingerprint"}},
        ),
    )
    child = deploy_release.load_desired_unit(path, "child")

    with pytest.raises(OperationError, match="invalid artifact dependency key"):
        deploy_release.UnitDeletionIntent.from_unit(child, path, tmp_path)


def test_schema_two_deletion_intent_migrates_owner_and_dependencies(tmp_path):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
            resolved_inputs={
                "receipts": {"parent": "receipt-fingerprint"},
                "artifacts": {"parent/image": "artifact-fingerprint"},
                "promotions": {"parent#/outputs/value": "promotion-fingerprint"},
            },
        ),
    )
    child = deploy_release.load_desired_unit(child_path, "child")
    intent = deploy_release.UnitDeletionIntent.from_unit(child, child_path, current)
    old_document = intent.document()
    old_document["retainedIdentity"] = {
        key: value for key, value in old_document["retainedIdentity"].items() if key not in {"owner", "dependencies"}
    }
    _write_json(current / ".gitopsctr/deletion-intents/units/child.json", old_document)

    migrated = deploy_release.load_desired_deletion_intents(current)["child"]
    assert migrated.retained_owner is not None
    assert migrated.retained_owner.uid == "d1-parent"
    assert migrated.retained_dependencies == ("parent",)


def test_schema_two_opaque_intent_preserves_unknown_identity_and_blocks_parent(tmp_path):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    child_path = current / "units/child.json"
    _write_json(child_path, _terraform_unit("child", "d1-child"))
    child = deploy_release.load_desired_unit(child_path, "child")
    intent = deploy_release.UnitDeletionIntent.from_unit(child, child_path, current)
    old_document = intent.document()
    old_document["retainedIdentity"] = {
        key: value for key, value in old_document["retainedIdentity"].items() if key not in {"owner", "dependencies"}
    }
    child_path.unlink()
    _write_json(current / ".gitopsctr/deletion-intents/units/child.json", old_document)
    deploy_release.write_opaque_cleanup_root(
        current,
        "child",
        deploy_release.OpaqueCleanupRoot(
            path=Path("child.yaml"),
            payload={"unparseable": True},
            metadata=deploy_release.ResourceMetadata.source_tracked_from_provenance("child", "opaque-child"),
            source=None,
        ),
    )

    migrated = deploy_release.load_desired_deletion_intents(current)["child"]
    assert migrated.retained_identity_known is False
    parent = deploy_release.load_desired_unit(parent_path, "parent")
    assert deploy_release.active_teardown_dependents(current, parent) == (
        "child (deletion intent lacks validated owner/dependency identity)",
    )


def test_schema_two_intent_rejects_mutated_parseable_retained_unit(tmp_path):
    current = tmp_path / "current"
    child_path = current / "units/child.json"
    _write_json(child_path, _terraform_unit("child", "d1-child"))
    child = deploy_release.load_desired_unit(child_path, "child")
    intent = deploy_release.UnitDeletionIntent.from_unit(child, child_path, current)
    _write_json(current / ".gitopsctr/deletion-intents/units/child.json", intent.document())

    mutated = _terraform_unit("child", "d1-child", source_revision="b" * 40)
    _write_json(child_path, mutated)

    loaded = deploy_release.load_desired_deletion_intents(current)["child"]
    assert loaded.retained_identity_known is False


def test_legacy_teardown_evidence_is_superseded_by_keyed_evidence(tmp_path, monkeypatch):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    observed_source = tmp_path / "observed-source"
    legacy = deploy_release.TeardownEvidence(
        unit_name="application", uid=intent.uid, deletion_generation=1, desired_revision="c" * 40
    )
    legacy_path = observed_source / ".gitopsctr/teardowns/units/application.json"
    legacy_document = legacy.document()
    legacy_document.pop("details")
    deploy_release.write_document(legacy_path, legacy_document, format=deploy_release.DocumentFormat.JSON)
    publications: list[dict[str, bytes]] = []

    def observed_tree(_ref, output):
        shutil.copytree(observed_source, output)
        return "c" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)

    def publish(_ref, directory, _parent, _message):
        publications.append(deploy_release.directory_files(directory))
        return "d" * 40

    monkeypatch.setattr(deploy_release, "publish_tree", publish)
    deploy_release.publish_teardown_observation_cas("observed/dev", intent, "c" * 40)

    assert len(publications) == 1
    assert ".gitopsctr/teardowns/units/application.json" not in publications[0]
    assert ".gitopsctr/teardowns/units/application.d1-application.1.json" in publications[0]


def test_rollback_preserves_current_opaque_payload_for_active_intent(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current_unit_path = current / "units/application.json"
    _write_json(current_unit_path, _terraform_unit("application", "d1-current"))
    unit = deploy_release.load_desired_unit(current_unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, current_unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    current_unit_path.unlink()
    payload = {"original": "opaque-current", "source": {"revision": "a" * 40}}
    deploy_release.write_opaque_cleanup_root(
        current,
        "application",
        deploy_release.OpaqueCleanupRoot(
            path=Path("application.yaml"),
            payload=payload,
            metadata=deploy_release.ResourceMetadata(
                name="application",
                uid=intent.uid,
                lifecycle=deploy_release.DesiredLifecycle(
                    management=deploy_release.LifecycleManagement(mode="sourceTracked")
                ),
            ),
            source=None,
        ),
    )
    _write_json(candidate / "units/application.json", _terraform_unit("application", "d1-history"))

    deploy_release.merge_current_cleanup_state(current, candidate)

    cleanup = deploy_release.load_desired_cleanup_roots(candidate)["application"]
    assert cleanup.path.suffix == ".yaml"
    assert cleanup.payload == payload
    assert deploy_release.load_desired_deletion_intents(candidate)["application"].uid == intent.uid


def test_teardown_evidence_is_selected_by_uid_and_generation(tmp_path):
    observed = tmp_path / "observed"
    old = deploy_release.TeardownEvidence(
        unit_name="application", uid="d1-old", deletion_generation=1, desired_revision="a" * 40
    )
    path = observed / deploy_release.OBSERVED_TEARDOWN_EVIDENCE_PATH / "application.d1-old.1.json"
    deploy_release.write_document(path, old.document(), format=deploy_release.DocumentFormat.JSON)

    assert deploy_release.load_teardown_evidence(observed, "application", "d1-new", 1) is None
    assert deploy_release.load_teardown_evidence(observed, "application", "d1-old", 1) == old


def test_teardown_evidence_details_round_trip_and_legacy_default():
    evidence = deploy_release.TeardownEvidence(
        unit_name="application",
        uid="d1-application",
        deletion_generation=2,
        desired_revision="a" * 40,
        details={"resource": {"id": "example", "count": 1}},
    )

    assert deploy_release.TeardownEvidence.from_document(evidence.document(), "application") == evidence
    legacy_document = evidence.document()
    legacy_document.pop("details")
    restored = deploy_release.TeardownEvidence.from_document(legacy_document, "application")
    assert restored.details == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_teardown_evidence_rejects_non_finite_details(value):
    evidence = deploy_release.TeardownEvidence(
        unit_name="application",
        uid="d1-application",
        deletion_generation=2,
        desired_revision="a" * 40,
    ).document()
    evidence["details"] = {"value": value}

    with pytest.raises(ValueError, match="invalid teardown evidence details"):
        deploy_release.TeardownEvidence.from_document(evidence, "application")


@pytest.mark.parametrize(
    "field",
    ["unit_blob", "subject_name", "subject_api_version", "subject_kind", "driver"],
)
def test_finalize_filters_stale_previous_receipt_identity(tmp_path, monkeypatch, field):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    desired_files = deploy_release.directory_files(current)
    teardown_calls = []

    receipt = deploy_release.RESOURCE_CATALOG.parse_receipt(
        receipt_document(
            "terraform",
            "application",
            {"unitBlob": intent.retained_unit_blob, "revision": "c" * 40},
            {"applied": {"sourceRevision": "c" * 40}, "outputs": {}},
        ),
        "application",
    )
    valid_receipt = receipt
    if field == "unit_blob":
        receipt = replace(receipt, spec=replace(receipt.spec, desired=replace(receipt.spec.desired, unitBlob="e" * 40)))
    elif field == "subject_name":
        receipt = replace(receipt, spec=replace(receipt.spec, subject=replace(receipt.spec.subject, name="other")))
    elif field == "subject_api_version":
        receipt = replace(
            receipt, spec=replace(receipt.spec, subject=replace(receipt.spec.subject, apiVersion="other.io/v1"))
        )
    elif field == "subject_kind":
        receipt = replace(receipt, spec=replace(receipt.spec, subject=replace(receipt.spec.subject, kind="Other")))
    else:
        receipt = replace(receipt, driver=deploy_release.UNIT_DRIVERS["vite-oci-bundle"])
    monkeypatch.setattr(deploy_release, "load_receipt", lambda *_args, **_kwargs: receipt)

    def observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/dev":
            for relative, content in desired_files.items():
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        else:
            _write_json(
                output / "units/application.json", deploy_release.RESOURCE_CATALOG.serialize_receipt(valid_receipt)
            )
        return "c" * 40 if ref == "deploy/dev" else None

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult()

    monkeypatch.setattr(type(deploy_release.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        deploy_release, "materialize_revision", lambda _revision, output: output.mkdir(parents=True, exist_ok=True)
    )
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(deploy_release, "publish_tree", lambda *_args, **_kwargs: "d" * 40)

    assert deploy_release.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 1
    assert teardown_calls[0].previous_receipt is None


def test_finalize_revision_fence_blocks_stale_reconcile_effect(tmp_path, monkeypatch):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "d" * 40)
    monkeypatch.setattr(
        type(deploy_release.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args: pytest.fail("stale desired revision reached teardown"),
    )

    with pytest.raises(OperationError, match="changed during effect preparation"):
        deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1))


def test_finalize_direct_intent_materializes_retained_source(tmp_path, monkeypatch):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    document = _terraform_unit("application", "direct-application")
    document["metadata"] = deploy_release.ResourceMetadata(
        name="application",
        uid="direct-application",
        lifecycle=deploy_release.DesiredLifecycle(management=deploy_release.LifecycleManagement(mode="direct")),
    ).document(profile="desired")
    _write_json(unit_path, document)
    unit = deploy_release.load_desired_unit(unit_path, "application")
    incarnation = deploy_release.UnitIncarnationTombstone(
        unit_name="application",
        uid="direct-application",
        state="active",
        next_deletion_generation=2,
    )
    deploy_release.write_unit_incarnation_tombstone(current, incarnation)
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current, 2)
    deploy_release.write_deletion_intent(current, intent)
    teardown_calls = []
    publications = []

    def observed_tree(ref: str, output: Path):
        if ref == "deploy/dev":
            shutil.copytree(current, output)
            return "c" * 40
        output.mkdir(parents=True, exist_ok=True)
        return None

    def publish_tree(ref: str, directory: Path, _parent: str | None, _message: str):
        publications.append((ref, deploy_release.directory_files(directory)))
        return "d" * 40

    materialized_revisions = []

    def materialize_revision(revision: str, output: Path):
        materialized_revisions.append(revision)
        output.mkdir(parents=True, exist_ok=True)

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult()

    monkeypatch.setattr(type(deploy_release.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize_revision)
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(deploy_release, "publish_tree", publish_tree)

    assert deploy_release.command_finalize(_finalize_args(uid="direct-application", deletion_generation=2)) is True
    assert len(teardown_calls) == 1
    assert materialized_revisions == ["a" * 40]
    assert teardown_calls[0].source_root is not None
    assert publications[-1][0] == "deploy/dev"
