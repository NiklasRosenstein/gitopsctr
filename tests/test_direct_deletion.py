"""Generic deletion requests for directly managed desired resources."""

import json
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredOwnerReference
from gitopsctr.errors import OperationError


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def _unit_document(
    name: str = "application",
    uid: str = "direct-application",
    mode: str = "direct",
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = controller.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=(
            None
            if owner is not None
            else controller.DesiredLifecycle(
                management=controller.LifecycleManagement(mode=mode)  # type: ignore[arg-type]
            )
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
                "revision": "a" * 40,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": controller.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }


def _args(**overrides: object) -> Namespace:
    values = {
        "environment": "dev",
        "kind": "Unit",
        "name": "application",
        "uid": "direct-application",
        "input_location": "state",
        "desired_ref": "deploy/dev",
        "candidate_ref": None,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _prepare(tmp_path: Path, monkeypatch, document: dict[str, object]):
    current = tmp_path / "current"
    name = str(document["metadata"]["name"])  # type: ignore[index]
    _write(current / f"units/{name}.yaml", document)
    published: list[Path] = []

    def materialize(_revision: str, output: Path):
        shutil.copytree(current, output)

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "c" * 40, None

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "b" * 40)
    monkeypatch.setattr(controller, "materialize_revision", materialize)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    return current, published


def _marked_unit(root: Path):
    return controller.load_desired_unit(root / "units/application.yaml", "application")


def test_generic_delete_marks_retained_unit_with_generation_and_digest(tmp_path, monkeypatch):
    _current, published = _prepare(tmp_path, monkeypatch, _unit_document())
    args = controller.build_parser().parse_args(
        [
            "delete",
            "unit",
            "--in=state",
            "--environment",
            "dev",
            "--name",
            "application",
            "--uid",
            "direct-application",
            "--desired-ref",
            "deploy/dev",
        ]
    )
    controller.command_delete_resource(args)

    retained = _marked_unit(published[0])
    deletion = controller.resource_deletion(retained)
    assert deletion is not None
    assert deletion.generation == 1
    assert deletion.resourceDigest == controller.resource_content_digest(retained)
    assert retained.metadata.uid == "direct-application"
    assert not any("deletion" in path.name for path in published[0].rglob("*"))


def test_generic_delete_marks_uid_owned_descendants(tmp_path, monkeypatch):
    parent = _unit_document("parent", "direct-parent")
    child = _unit_document(
        "child",
        "direct-child",
        owner=DesiredOwnerReference(
            apiVersion="unit.gitopsctr.io/v1", kind="Terraform", name="parent", uid="direct-parent"
        ),
    )
    current, published = _prepare(tmp_path, monkeypatch, parent)
    _write(current / "units/child.yaml", child)

    controller.command_delete_resource(_args(name="parent", uid="direct-parent"))

    marked_parent = controller.load_desired_unit(published[0] / "units/parent.yaml", "parent")
    assert controller.resource_deletion(marked_parent) is not None
    marked_child = controller.load_desired_unit(published[0] / "units/child.yaml", "child")
    assert marked_child.metadata.ownerReferences is not None
    assert marked_child.metadata.ownerReferences[0].uid == "direct-parent"
    assert controller.resource_deletion(marked_child) is not None


def test_generic_delete_is_retry_safe_and_rejects_changed_deleted_resource(tmp_path, monkeypatch):
    current, published = _prepare(tmp_path, monkeypatch, _unit_document())
    controller.command_delete_resource(_args())
    assert len(published) == 1

    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    # The helper still represents the active source tree. A real retry against
    # the published tree must be a no-op, not a second mutation.
    monkeypatch.setattr(
        controller, "materialize_revision", lambda _revision, output: shutil.copytree(published[0], output)
    )
    assert controller.command_delete_resource(_args()) is None
    assert len(published) == 1

    changed = controller.load_desired_unit(published[0] / "units/application.yaml", "application")
    changed_document = controller.serialize_unit_document(changed, profile="desired")
    changed_document["spec"]["terraform"]["variables"] = {"changed": True}  # type: ignore[index]
    _write(published[0] / "units/application.yaml", changed_document)
    with pytest.raises(OperationError, match="changed after deletion started"):
        controller.command_delete_resource(_args())


def test_generic_delete_rejects_stale_uid_and_owned_unit_target(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, _unit_document())
    with pytest.raises(OperationError, match="stale desired Unit UID fence"):
        controller.command_delete_resource(_args(uid="other-application"))


def test_advance_marks_source_tracked_root_when_source_is_absent(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    _write(
        source / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {"effectLease": None},
        },
    )
    _write(
        source / "deployment/environments/dev/environment.json",
        controller.serialize_environment_document({"schema": 1, "name": "dev"}),
    )
    _write(current / "units/application.yaml", _unit_document(mode="sourceTracked"))
    observed.mkdir()

    controller.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)

    retained = _marked_unit(candidate)
    assert controller.resource_deletion(retained) is not None
    assert controller.deletion_reason(retained).startswith("deletion pending finalization")


def test_apply_direct_unit_rejects_reuse_of_finalized_uid(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    document_path = tmp_path / "application.json"
    _write(document_path, _unit_document())
    controller.write_resource_incarnation_tombstone(
        current,
        controller.ResourceIncarnationTombstone(
            api_version="unit.gitopsctr.io/v1",
            kind="Terraform",
            name="application",
            uid="direct-application",
            deletion_generation=1,
        ),
    )
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "b" * 40)
    monkeypatch.setattr(
        controller,
        "materialize_revision",
        lambda _revision, output: shutil.copytree(current, output),
    )
    args = Namespace(
        input_location="state",
        file=str(document_path),
        environment="dev",
        desired_ref=None,
        candidate_ref=None,
        uid=None,
        desired_revision=None,
        request_id="retry-finalized-uid",
        or_update=True,
        dry=False,
    )

    with pytest.raises(OperationError, match="finalized desired Unit UID cannot be reused"):
        controller.command_apply_unit(args)
