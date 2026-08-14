"""Generic StackTemplate source retention across desired-state publication."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import ControllerPin
from tests.stack_deletion_support import stack_tree
from tests.stack_support import commit, git, project_repository, write_stack_source


def _pin(name: str, revision: str) -> ControllerPin:
    return ControllerPin(name, f"refs/heads/gitopsctr/pins/{name}", revision)


def test_publish_retains_local_stack_source_before_advancing_desired(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "candidate"
    stack_uid, _unit_name = stack_tree(candidate)
    events: list[tuple[str, str, str] | tuple[str]] = []

    def create_pins(revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        pins = []
        for name, revision in revisions.items():
            events.append(("pin", name, revision))
            pins.append(_pin(name, revision))
        return tuple(pins)

    def publish_tree(*_args: object) -> str:
        events.append(("publish",))
        return "d" * 40

    monkeypatch.setattr(controller, "state_store", lambda: SimpleNamespace(create_controller_pins=create_pins))
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(controller, "publish_tree", publish_tree)

    revision, outcome = controller.publish_desired_change(
        "dev",
        candidate,
        "deploy/dev",
        "c" * 40,
        "candidate/dev",
        "Apply Stack",
        "Apply Stack",
        "Apply one Stack.",
        False,
    )

    expected_name = f"stacks/dev/preview/{stack_uid}/{'a' * 40}"
    assert events == [("pin", expected_name, "a" * 40), ("publish",)]
    assert revision == "d" * 40
    assert outcome is None


def test_pin_acquisition_failure_cannot_publish_desired_state(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "candidate"
    stack_tree(candidate)
    published = False

    def publish_tree(*_args: object) -> str:
        nonlocal published
        published = True
        return "d" * 40

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            create_controller_pins=lambda *_args: (_ for _ in ()).throw(OperationError("pin unavailable"))
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(controller, "publish_tree", publish_tree)

    with pytest.raises(OperationError, match="pin unavailable"):
        controller.publish_desired_change(
            "dev",
            candidate,
            "deploy/dev",
            "c" * 40,
            "candidate/dev",
            "Apply Stack",
            "Apply Stack",
            "Apply one Stack.",
            False,
        )
    assert not published


def test_remote_stack_source_does_not_create_a_local_controller_pin(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "candidate"
    stack_tree(candidate)
    path = candidate / "stacks/preview.json"
    document = json.loads(path.read_text())
    source = document["spec"]["resolvedSource"]["fromGit"]
    source.pop("path")
    source["remote"] = "https://example.com/templates.git"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: pytest.fail("remote StackTemplate sources must not use deployment-repository pins"),
    )

    assert controller._ensure_stack_source_pins("dev", candidate) == ()


def test_stack_source_pin_identity_is_partition_independent(tmp_path: Path):
    candidate = tmp_path / "candidate"
    stack_uid, _unit_name = stack_tree(candidate)
    stack_path = candidate / "stacks/preview.json"
    document = json.loads(stack_path.read_text())
    document["metadata"]["labels"]["gitopsctr.io/partition"] = "another-partition"
    stack_path.write_text(json.dumps(document))

    assert controller._required_stack_source_pins("dev", candidate) == (
        (f"stacks/dev/preview/{stack_uid}/{'a' * 40}", "a" * 40),
    )


def test_reapplying_authored_stack_preserves_uid_while_source_pin_advances(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    first = tmp_path / "first"
    second = tmp_path / "second"
    controller.project_stack_resources(source, "dev", "a" * 40, first, source, partition="application")
    controller.project_stack_resources(
        source,
        "dev",
        "b" * 40,
        second,
        source,
        current_desired=first,
        partition="application",
    )

    first_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(first / "stacks/web.json"),
        profile="desired",
        expected_name="web",
    )
    second_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(second / "stacks/web.json"),
        profile="desired",
        expected_name="web",
    )
    assert first_stack.metadata.uid == second_stack.metadata.uid
    assert isinstance(first_stack.spec, controller.DesiredStackSpec)
    assert isinstance(second_stack.spec, controller.DesiredStackSpec)
    assert first_stack.spec.resolvedSource is not None
    assert second_stack.spec.resolvedSource is not None
    assert first_stack.spec.resolvedSource.fromGit.commit == "a" * 40
    assert second_stack.spec.resolvedSource.fromGit.commit == "b" * 40


def test_noop_stack_apply_repairs_a_missing_source_pin(tmp_path: Path, monkeypatch):
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "init", "--bare", str(remote))
    environment = project_repository(source)
    write_stack_source(environment)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    source_revision = commit(source, "initialize source")
    git(source, "push", "-u", "origin", "main")
    store = controller.GitStateStore(source)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / ".gitkeep").write_text("")
    store.publish("deploy/dev", baseline, None, "initialize desired state")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    arguments = [
        "apply",
        "--environment",
        "dev",
        "--source-revision",
        source_revision,
        "--desired-ref",
        "deploy/dev",
        "--observed-ref",
        "observed/dev",
        "--partition",
        "application",
        "-f",
        str(environment / "stacks/web.json"),
    ]
    args = controller.build_parser().parse_args(arguments)

    controller.command_apply(args)
    stable_revision = controller.command_apply(args)
    desired = tmp_path / "desired"
    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None
    store.materialize(desired_revision, desired)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(desired / "stacks/web.json"),
        profile="desired",
        expected_name="web",
    )
    assert stack.metadata.uid is not None
    pin_name = f"stacks/dev/web/{stack.metadata.uid}/{source_revision}"
    assert store.release_controller_pin(pin_name, source_revision)
    assert store.list_controller_pins() == ()

    assert controller.command_apply(args) == stable_revision
    assert store.list_controller_pins() == (
        ControllerPin(pin_name, f"refs/heads/gitopsctr/pins/{pin_name}", source_revision),
    )
