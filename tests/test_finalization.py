"""Generic metadata-based Unit finalization and cleanup safety tests."""

import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredOwnerReference
from gitopsctr.driver import TeardownResult
from gitopsctr.errors import OperationError
from tests.conftest import write_test_document


def _write(path: Path, document: object) -> None:
    write_test_document(path, document)


def _terraform_unit(
    name: str,
    uid: str,
    source_revision: str = "a" * 40,
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = controller.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=(
            None
            if owner is not None
            else controller.DesiredLifecycle(management=controller.LifecycleManagement(mode="sourceTracked"))
        ),
        ownerReferences=[owner] if owner is not None else None,
    )
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": source_revision,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": controller.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }


def _finalize_args(name: str = "application", **overrides: object) -> Namespace:
    values = {
        "kind": "Unit",
        "name": name,
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


def _stub_effect_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    def acquire(_ref: str, revision: str, unit_name: str, uid: str, **_kwargs: object):
        lease = controller.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="lease-test",
            owner="test-runner",
            desired_revision=revision,
            expires_at=2_000_000_000,
        )
        return controller.EffectLeaseAcquisition(lease=lease, revision=revision)

    class NoopHeartbeat:
        def __init__(self, acquisition):
            self.acquisition = acquisition

        def stop(self):
            return self.acquisition

    monkeypatch.setattr(controller, "acquire_effect_lease", acquire)
    monkeypatch.setattr(controller, "release_effect_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "start_effect_lease_heartbeat",
        lambda _ref, acquisition, **_kwargs: NoopHeartbeat(acquisition),
    )
    monkeypatch.setattr(controller, "validate_effect_lease_head", lambda _ref, *_args, **_kwargs: "c" * 40)
    monkeypatch.setattr(
        controller,
        "rebase_effect_completion",
        lambda _ref, acquisition, unit_name, uid, root, **_kwargs: (
            replace(
                acquisition,
                lease=replace(
                    acquisition.lease,
                    snapshot=controller.effect_lease_snapshot(root, unit_name, uid),
                ),
            )
            if acquisition.lease.snapshot is None
            else acquisition
        ),
    )


def _prepare_finalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: dict[str, object]):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    desired.mkdir()
    observed.mkdir()
    name = str(document["metadata"]["name"])  # type: ignore[index]
    _write(desired / f"units/{name}.yaml", document)
    desired_revision = "c" * 40
    publications: list[Path] = []
    teardown_publications: list[Path] = []

    def observed_tree(ref: str, output: Path):
        source = desired if ref == "deploy/dev" else observed
        shutil.copytree(source, output)
        return desired_revision if ref == "deploy/dev" else None

    def publish_tree(_ref: str, directory: Path, _parent: str | None, _message: str):
        snapshot = tmp_path / f"observed-{len(teardown_publications)}"
        shutil.copytree(directory, snapshot)
        teardown_publications.append(snapshot)
        shutil.rmtree(observed)
        shutil.copytree(directory, observed)
        return "e" * 40

    def publish_desired(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(publications)}"
        shutil.copytree(candidate, snapshot)
        publications.append(snapshot)
        shutil.rmtree(desired)
        shutil.copytree(candidate, desired)
        return "d" * 40, None

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: desired_revision)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: output.mkdir(parents=True))
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(controller, "publish_tree", publish_tree)
    monkeypatch.setattr(controller, "publish_desired_change", publish_desired)
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: "deploy/dev")
    _stub_effect_lease(monkeypatch)
    return desired, observed, publications, teardown_publications


def _mark(document: dict[str, object], name: str = "application") -> dict[str, object]:
    path = Path("/tmp") / f"gitopsctr-test-{name}.json"
    _write(path, document)
    resource = controller.load_desired_unit(path, name)
    marked = controller.mark_resource_for_deletion(resource)
    result = controller.serialize_unit_document(marked, profile="desired")
    path.unlink()
    return result


def test_source_absence_marks_retained_unit_and_retries_without_legacy_artifacts(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _write(
        source / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {"effectLease": None},
        },
    )
    _write(source / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
    _write(current / "units/application.yaml", _terraform_unit("application", "d1-application"))
    observed.mkdir()

    controller.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)
    retained = controller.load_desired_unit(candidate / "units/application.yaml", "application")
    deletion = controller.resource_deletion(retained)
    assert deletion is not None
    assert deletion.generation == 1
    assert deletion.resourceDigest == controller.resource_content_digest(retained)
    assert controller.deletion_reason(retained).startswith("deletion pending finalization")
    assert not any("deletion" in path.name for path in candidate.rglob("*"))

    controller.build_desired_candidate("dev", source, "c" * 40, candidate, observed, None, repeated, verbose=False)
    assert (repeated / "units/application.yaml").read_bytes() == (candidate / "units/application.yaml").read_bytes()


def test_finalize_runs_teardown_records_observed_evidence_and_removes_unit(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, publications, teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"destroyed": True})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 1
    assert len(publications) == 1
    assert teardown_publications
    assert not (desired / "units/application.yaml").exists()
    evidence = controller.load_teardown_evidence(observed, "application", "d1-application", 1)
    assert evidence is not None
    assert evidence.details == {"destroyed": True}


def test_finalize_without_effect_leases_runs_teardown_without_lease_mutations(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "acquire_effect_lease",
        lambda *_args, **_kwargs: pytest.fail("effect leases are disabled"),
    )
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, _context: TeardownResult(details={"destroyed": True}),
    )

    assert controller.command_finalize(_finalize_args()) is True
    assert not (desired / "units/application.yaml").exists()
    assert controller.load_teardown_evidence(observed, "application", "d1-application", 1) is not None


def test_finalize_retry_releases_separate_store_lease_after_desired_removal(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    controller.write_resource_incarnation_tombstone(
        current,
        controller.ResourceIncarnationTombstone(
            api_version=controller.UNIT_API_VERSION,
            kind="Terraform",
            name="application",
            uid="d1-application",
            deletion_generation=1,
        ),
    )
    controller.write_effect_lease(
        lease_root,
        controller.EffectLease(
            unit_name="application",
            uid="d1-application",
            token="lease-finalize",
            owner="test",
            desired_revision="c" * 40,
            expires_at=None,
        ),
    )
    released: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: "gitopsctr/leases")
    monkeypatch.setattr(
        controller,
        "observed_tree",
        lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1],
    )
    monkeypatch.setattr(
        controller,
        "_effect_lease_store_root",
        lambda *_args, **_kwargs: (lease_root, "e" * 40),
    )
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda desired_ref, name, token, _uid=None, **kwargs: released.append(
            (desired_ref, name, kwargs.get("lease_ref"))
        ),
    )

    assert controller.command_finalize(_finalize_args()) is True
    assert released == [("deploy/dev", "application", "gitopsctr/leases")]


def test_finalize_retries_from_observed_evidence_without_repeating_teardown(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"attempt": len(teardown_calls)})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    original_publish = controller.publish_desired_change
    monkeypatch.setattr(
        controller,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash after evidence")),
    )
    with pytest.raises(RuntimeError, match="crash after evidence"):
        controller.command_finalize(_finalize_args())
    assert len(teardown_calls) == 1
    assert controller.load_teardown_evidence(observed, "application", "d1-application", 1) is not None

    monkeypatch.setattr(controller, "publish_desired_change", original_publish)
    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 1
    assert not (desired / "units/application.json").exists()


def test_finalize_rejects_stale_uid_generation_and_digest_fences(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    for overrides, message in (
        ({"uid": "d1-other"}, "stale Unit UID fence"),
        ({"deletion_generation": 2}, "stale Unit deletion generation fence"),
    ):
        with pytest.raises(OperationError, match=message):
            controller.command_finalize(_finalize_args(**overrides))

    changed = controller.load_desired_unit(desired / "units/application.yaml", "application")
    changed_document = controller.serialize_unit_document(changed, profile="desired")
    changed_document["spec"]["terraform"]["variables"] = {"changed": True}  # type: ignore[index]
    _write(desired / "units/application.yaml", changed_document)
    with pytest.raises(OperationError, match="changed after deletion started"):
        controller.command_finalize(_finalize_args())


def test_finalize_blocks_owned_children_and_dependency_dependents(tmp_path, monkeypatch):
    parent = _terraform_unit("parent", "d1-parent")
    child = _terraform_unit(
        "child",
        "d1-child",
        owner=DesiredOwnerReference(
            apiVersion="unit.gitopsctr.io/v1", kind="Terraform", name="parent", uid="d1-parent"
        ),
    )
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(
        tmp_path, monkeypatch, _mark(parent, "parent")
    )
    _write(desired / "units/child.yaml", _mark(child, "child"))
    with pytest.raises(OperationError, match="owned resources must be finalized first"):
        controller.command_finalize(_finalize_args(name="parent", uid="d1-parent"))


def test_finalize_requires_runtime_fences(tmp_path, monkeypatch):
    _prepare_finalization(tmp_path, monkeypatch, _mark(_terraform_unit("application", "d1-application")))
    with pytest.raises(OperationError, match="requires --uid"):
        controller.command_finalize(_finalize_args(uid=None))
    with pytest.raises(OperationError, match="deletion-generation"):
        controller.command_finalize(_finalize_args(deletion_generation=None))


def test_generic_finalize_parser_uses_kind_and_name_fences():
    args = controller.build_parser().parse_args(
        [
            "finalize",
            "unit",
            "--environment",
            "dev",
            "--name",
            "application",
            "--uid",
            "d1-application",
            "--deletion-generation",
            "1",
        ]
    )
    assert args.kind == "unit"
    assert args.name == "application"
    assert args.uid == "d1-application"
    assert args.deletion_generation == 1


def test_finalize_blocks_observation_dependents_before_teardown(tmp_path, monkeypatch):
    parent = _mark(_terraform_unit("parent", "d1-parent"), "parent")
    child = _terraform_unit("child", "d1-child")
    child["spec"]["resolvedInputs"] = {"receipts": {"parent": "receipt-parent"}}  # type: ignore[index]
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, parent)
    _write(desired / "units/child.yaml", child)
    with pytest.raises(OperationError, match="active owned/dependent Units"):
        controller.command_finalize(_finalize_args(name="parent", uid="d1-parent"))


def test_opaque_cleanup_resolution_remains_supported(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-opaque-application"
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.mark_opaque_cleanup_for_deletion(
            controller.OpaqueCleanupRoot(
                path=current / ".gitopsctr/cleanup/units/application.json",
                payload="unparseable",
                metadata=controller.ResourceMetadata(
                    name="application",
                    uid=uid,
                    lifecycle=controller.DesiredLifecycle(
                        management=controller.LifecycleManagement(mode="sourceTracked")
                    ),
                ),
                source=None,
            )
        ),
    )
    published: list[Path] = []

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        controller, "observed_tree", lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1]
    )
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        deletion_generation=1,
        reason="external cleanup confirmed",
        confirm_external_cleanup=True,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    assert controller.command_resolve_opaque_unit(args) is True
    assert controller.load_desired_cleanup_roots(published[0]) == {}
    assert not any("deletion" in path.name for path in published[0].rglob("*"))
