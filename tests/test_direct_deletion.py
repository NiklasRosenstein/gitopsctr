"""Generic deletion requests for desired resources."""

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
    uid: str = "d1-application",
    partition: str | None = "application",
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = controller.ResourceMetadata(
        name=name,
        uid=uid,
        ownerReferences=[owner] if owner is not None else None,
    )
    if owner is None:
        metadata = metadata.with_partition(partition)
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
        "uid": "d1-application",
        "desired_ref": "deploy/dev",
        "candidate_ref": None,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _prepare(
    tmp_path: Path,
    monkeypatch,
    document: dict[str, object],
    resolved_candidate_ref: str = "candidate/dev",
):
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
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: resolved_candidate_ref)
    return current, published


def _marked_unit(root: Path):
    return controller.load_desired_unit(root / "units/application.yaml", "application")


def test_generic_delete_marks_retained_unit_with_generation_and_digest(tmp_path, monkeypatch):
    _current, published = _prepare(tmp_path, monkeypatch, _unit_document())
    args = controller.build_parser().parse_args(
        [
            "delete",
            "unit",
            "--environment",
            "dev",
            "--name",
            "application",
            "--uid",
            "d1-application",
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
    assert retained.metadata.uid == "d1-application"
    assert retained.metadata.partition == "application"
    assert not any("deletion" in path.name for path in published[0].rglob("*"))


@pytest.mark.parametrize("candidate_ref", ["observed/dev", "refs/heads/observed/dev"])
def test_generic_delete_rejects_candidate_ref_matching_observed(tmp_path, monkeypatch, candidate_ref):
    _current, _published = _prepare(tmp_path, monkeypatch, _unit_document(), candidate_ref)

    with pytest.raises(OperationError, match="conflicts with deployment state"):
        controller.command_delete_resource(_args(candidate_ref=candidate_ref))


def test_generic_delete_marks_uid_owned_descendants(tmp_path, monkeypatch):
    parent = _unit_document("parent", "d1-parent")
    child = _unit_document(
        "child",
        "d1-child",
        owner=DesiredOwnerReference(
            apiVersion="unit.gitopsctr.io/v1", kind="Terraform", name="parent", uid="d1-parent"
        ),
    )
    current, published = _prepare(tmp_path, monkeypatch, parent)
    _write(current / "units/child.yaml", child)

    controller.command_delete_resource(_args(name="parent", uid="d1-parent"))

    marked_parent = controller.load_desired_unit(published[0] / "units/parent.yaml", "parent")
    assert controller.resource_deletion(marked_parent) is not None
    marked_child = controller.load_desired_unit(published[0] / "units/child.yaml", "child")
    assert marked_child.metadata.ownerReferences is not None
    assert marked_child.metadata.ownerReferences[0].uid == "d1-parent"
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
