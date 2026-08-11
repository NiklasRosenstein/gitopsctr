"""Focused contract coverage for direct Stack updates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore
from tests.stack_support import commit, git, project_repository, write_projected_units, write_stack_source


def _args(
    source: Path,
    *,
    uid: str,
    desired_revision: str,
    source_revision: str,
    request_id: str = "github:example/application#update-1",
    **overrides: object,
):
    values = [
        "update-direct-stack",
        "--environment",
        "dev",
        "--stack",
        "web",
        "--uid",
        uid,
        "--desired-revision",
        desired_revision,
        "--template",
        "preview",
        "--source-revision",
        source_revision,
        "--parameters",
        '{"source-path":"."}',
        "--request-id",
        request_id,
        "--desired-ref",
        "deploy/dev",
        "--observed-ref",
        "observed/dev",
    ]
    for name, value in overrides.items():
        values.extend((f"--{name.replace('_', '-')}", str(value)))
    return controller.build_parser().parse_args(values)


def _direct_stack(tmp_path: Path, monkeypatch) -> tuple[Path, GitStateStore, str, str, str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "init", "--bare", str(remote))
    environment = project_repository(source)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    write_stack_source(environment)
    (environment / "stacks/web.json").unlink()
    first_source_revision = commit(source, "add template-only Stack source")
    git(source, "push", "-u", "origin", "main")

    store = GitStateStore(source)
    initial = tmp_path / "initial"
    controller.project_stack_resources(source, "dev", first_source_revision, initial, source)
    desired = store.publish("deploy/dev", initial, None, "publish initial desired state")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    instantiate = controller.build_parser().parse_args(
        [
            "instantiate-stack",
            "--environment",
            "dev",
            "--stack",
            "web",
            "--template",
            "preview",
            "--source-revision",
            first_source_revision,
            "--parameters",
            '{"source-path":"."}',
            "--request-id",
            "github:example/application#123",
            "--desired-ref",
            "deploy/dev",
            "--observed-ref",
            "observed/dev",
        ]
    )
    assert controller.command_instantiate_stack(instantiate)
    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None and desired_revision != desired.revision
    current = tmp_path / "current"
    store.materialize(desired_revision, current)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((current / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert stack.metadata.uid is not None

    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["unitTemplates"]["preview-app"]["spec"]["terraform"] = {"variables": {"revision": "v2"}}
    template_path.write_text(json.dumps(template))
    second_source_revision = commit(source, "update StackTemplate revision")
    git(source, "push", "origin", "main")
    return source, store, desired_revision, stack.metadata.uid, second_source_revision


def test_update_direct_stack_preserves_uid_owner_and_updates_pin_provenance(tmp_path: Path, monkeypatch):
    source, store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    args = _args(source, uid=uid, desired_revision=desired_revision, source_revision=source_revision)

    assert controller.command_update_direct_stack(args)
    updated_revision = store.fetch("deploy/dev").revision
    assert updated_revision is not None and updated_revision != desired_revision
    updated = tmp_path / "updated"
    store.materialize(updated_revision, updated)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((updated / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert stack.metadata.uid == uid
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.provenance is not None
    assert stack.spec.provenance.templateRevision == source_revision
    unit = controller.load_desired_unit(next((updated / "units").glob("web--preview-app.*")), "web--preview-app")
    assert unit.metadata.lifecycle is not None
    assert unit.metadata.lifecycle.owner is not None
    assert unit.metadata.lifecycle.owner.uid == uid
    pin = next(pin for pin in store.list_controller_pins() if pin.name == f"stacks/dev/web/{uid}")
    assert pin.revision == source_revision


def test_update_direct_stack_replay_is_idempotent(tmp_path: Path, monkeypatch):
    source, store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    args = _args(source, uid=uid, desired_revision=desired_revision, source_revision=source_revision)
    assert controller.command_update_direct_stack(args)
    updated_revision = store.fetch("deploy/dev").revision
    assert updated_revision is not None
    assert controller.command_update_direct_stack(args) is False
    assert store.fetch("deploy/dev").revision == updated_revision


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"uid": "d1-stale"}, "stale desired Stack UID"),
        ({"desired_revision": "a" * 40}, "stale desired Stack head"),
    ],
)
def test_update_direct_stack_rejects_stale_fences(tmp_path: Path, monkeypatch, override, message):
    source, _store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    args = _args(
        source,
        uid=override.get("uid", uid),
        desired_revision=override.get("desired_revision", desired_revision),
        source_revision=source_revision,
    )
    with pytest.raises(OperationError, match=message):
        controller.command_update_direct_stack(args)


def test_update_direct_stack_rejects_source_tracked_stack(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    environment = project_repository(source)
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    write_stack_source(environment)
    source_revision = commit(source, "source Stack")
    store = GitStateStore(source)
    desired = tmp_path / "desired"
    projection = controller.project_stack_resources(source, "dev", source_revision, desired, source)
    write_projected_units(desired, projection, source)
    desired_revision = store.publish("deploy/dev", desired, None, "publish source Stack").revision
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((desired / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert stack.metadata.uid is not None
    args = _args(
        source,
        uid=stack.metadata.uid,
        desired_revision=desired_revision,
        source_revision=source_revision,
    )
    with pytest.raises(OperationError, match="source-tracked"):
        controller.command_update_direct_stack(args)


def test_update_direct_stack_deletion_uses_new_cleanup_source(tmp_path: Path, monkeypatch):
    source, store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    args = _args(source, uid=uid, desired_revision=desired_revision, source_revision=source_revision)
    assert controller.command_update_direct_stack(args)
    updated_revision = store.fetch("deploy/dev").revision
    assert updated_revision is not None
    delete_args = controller.build_parser().parse_args(
        [
            "request-delete-direct-stack",
            "--environment",
            "dev",
            "--stack",
            "web",
            "--uid",
            uid,
            "--desired-ref",
            "deploy/dev",
        ]
    )
    assert controller.command_request_delete_direct_stack(delete_args)
    deletion_revision = store.fetch("deploy/dev").revision
    assert deletion_revision is not None
    deletion = tmp_path / "deletion"
    store.materialize(deletion_revision, deletion)
    intent = controller.load_desired_stack_deletion_intents(deletion)["web"]
    assert intent.retained_provenance is not None
    assert intent.retained_provenance.templateRevision == source_revision


def test_update_direct_stack_publication_failure_restores_old_pin(tmp_path: Path, monkeypatch):
    source, store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    old_pin = next(pin for pin in store.list_controller_pins() if pin.name == f"stacks/dev/web/{uid}")
    args = _args(source, uid=uid, desired_revision=desired_revision, source_revision=source_revision)

    def fail_publish(*_args, **_kwargs):
        raise OperationError("simulated publication failure")

    monkeypatch.setattr(controller, "publish_desired_change", fail_publish)
    with pytest.raises(OperationError, match="simulated publication failure"):
        controller.command_update_direct_stack(args)
    assert next(pin for pin in store.list_controller_pins() if pin.name == old_pin.name).revision == old_pin.revision
    assert store.fetch("deploy/dev").revision == desired_revision


def test_update_direct_stack_replay_repairs_claim_after_publication(tmp_path: Path, monkeypatch):
    source, store, desired_revision, uid, source_revision = _direct_stack(tmp_path, monkeypatch)
    args = _args(source, uid=uid, desired_revision=desired_revision, source_revision=source_revision)

    def fail_claim_update(*_args, **_kwargs):
        raise OperationError("simulated claim update failure")

    with monkeypatch.context() as failing:
        failing.setattr(GitStateStore, "update_controller_pin_claim", fail_claim_update)
        with pytest.raises(OperationError, match="simulated claim update failure"):
            controller.command_update_direct_stack(args)

    updated_revision = store.fetch("deploy/dev").revision
    assert updated_revision is not None and updated_revision != desired_revision
    assert controller.command_update_direct_stack(args) is False
    claim = store.read_controller_pin_claim(f"stacks/dev/web/{uid}")
    assert claim is not None
    assert claim.state == "active"
    assert claim.pin_revision == source_revision
    assert claim.target_revision == updated_revision
