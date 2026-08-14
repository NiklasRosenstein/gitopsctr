"""Generic metadata-based Stack deletion and finalization coverage."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests.stack_deletion_support import deletion_args as _args
from tests.stack_deletion_support import fake_git as _fake_git
from tests.stack_deletion_support import stack_tree as _stack_tree


def _stack(root: Path):
    return controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(root / "stacks/preview.json"),
        profile="desired",
        expected_name="preview",
    )


def _unit(root: Path):
    return controller.load_desired_unit(root / "units/preview--preview-app.json", "preview--preview-app")


def _publish_snapshots(tmp_path: Path):
    published: list[Path] = []

    def publish(_environment: str, candidate: Path, *_args: object, **_kwargs: object):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    return published, publish


def _configure_state(monkeypatch, tmp_path: Path, current: Path, store=None) -> None:
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(controller, "git", _fake_git)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    if store is not None:
        monkeypatch.setattr(controller, "state_store", lambda: store)


def test_delete_stack_marks_stack_and_owned_units_with_metadata(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)

    assert controller.command_delete_resource(_args()) is None

    candidate = published[0]
    stack = _stack(candidate)
    unit = _unit(candidate)
    assert stack.metadata.deletion is not None
    assert stack.metadata.deletion.generation == 1
    assert stack.metadata.deletion.resourceDigest == controller.resource_content_digest(stack)
    assert unit.metadata.deletion is not None
    assert unit.metadata.deletion.resourceDigest == controller.resource_content_digest(unit)
    assert unit.metadata.ownerReferences is not None
    assert unit.metadata.ownerReferences[0].uid == stack.metadata.uid


def test_delete_and_finalize_standalone_stacktemplate(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(
        monkeypatch,
        tmp_path,
        current,
        SimpleNamespace(list_controller_pins=lambda: (), release_controller_pin=lambda *_args: True),
    )
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    args = _args(kind="StackTemplate", name="preview", uid="d1-template")

    assert controller.command_delete_resource(args) is None
    marked = published[0]
    retained = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(marked / "stack-templates/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    assert retained.metadata.deletion is not None

    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))
    assert controller.command_finalize(args) is True
    finalized = published[1]
    assert not list((finalized / "stack-templates").glob("preview.*"))
    assert (
        controller.load_resource_incarnation_tombstones(finalized)[
            (controller.CORE_API_VERSION, "StackTemplate", "preview")
        ].uid
        == "d1-template"
    )


def test_finalize_stack_after_child_finalization(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)

    assert controller.command_delete_resource(_args()) is None
    marked = published[0]
    (marked / "units/preview--preview-app.json").unlink()
    controller.write_resource_incarnation_tombstone(
        marked,
        controller.ResourceIncarnationTombstone(
            api_version=controller.UNIT_API_VERSION,
            kind="Terraform",
            name="preview--preview-app",
            uid="d1-preview-app",
            deletion_generation=1,
        ),
    )
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))

    assert controller.command_finalize(_args()) is True
    assert not list((published[1] / "stacks").glob("preview.*"))


def test_finalize_stack_refuses_a_present_child(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None
    marked = published[0]
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))

    with pytest.raises(OperationError, match="owned resources must be finalized first"):
        controller.command_finalize(_args())


def test_finalize_stack_rejects_missing_child_without_tombstone(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None
    marked = published[0]
    (marked / "units/preview--preview-app.json").unlink()
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))

    with pytest.raises(OperationError, match="missing without a matching incarnation tombstone"):
        controller.command_finalize(_args())


def test_finalize_stack_publication_failure_is_retryable(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None
    marked = published[0]
    (marked / "units/preview--preview-app.json").unlink()
    controller.write_resource_incarnation_tombstone(
        marked,
        controller.ResourceIncarnationTombstone(
            api_version=controller.UNIT_API_VERSION,
            kind="Terraform",
            name="preview--preview-app",
            uid="d1-preview-app",
            deletion_generation=1,
        ),
    )
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("publication unavailable")
        return publish(*args, **kwargs)

    monkeypatch.setattr(controller, "publish_desired_change", fail_once)
    with pytest.raises(RuntimeError, match="publication unavailable"):
        controller.command_finalize(_args())
    assert controller.command_finalize(_args()) is True
    assert attempts == 2


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"uid": "d1-other"}, "stale Stack UID fence"),
        ({"deletion_generation": 2}, "stale Stack deletion generation"),
    ],
)
def test_finalize_stack_rejects_stale_fences(tmp_path: Path, monkeypatch, override, message):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None
    marked = published[0]
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(marked, output))

    with pytest.raises(OperationError, match=message):
        controller.command_finalize(_args(**override))


def test_finalize_stack_rejects_changed_retained_resource_digest(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published, publish = _publish_snapshots(tmp_path)
    _configure_state(monkeypatch, tmp_path, current)
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None

    changed = tmp_path / "changed"
    shutil.copytree(published[0], changed)
    document = json.loads((changed / "stacks/preview.json").read_text())
    document["spec"]["parameters"] = {"changed": True}
    (changed / "stacks/preview.json").write_text(json.dumps(document))
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(changed, output))

    with pytest.raises(OperationError, match="changed after deletion started"):
        controller.command_finalize(_args())
