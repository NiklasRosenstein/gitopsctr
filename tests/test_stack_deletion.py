"""Generic metadata-based Stack deletion coverage."""

import shutil
from pathlib import Path

from gitopsctr import controller
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
