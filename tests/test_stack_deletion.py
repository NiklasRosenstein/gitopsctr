"""UID- and generation-fenced direct Stack deletion requests."""

import hashlib
import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli
from gitopsctr.contracts import (
    DesiredLifecycle,
    DesiredStackSpec,
    LifecycleManagement,
    StackInstantiationProvenance,
    StackTemplateResource,
    StackTemplateSpec,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, StackResource
from gitopsctr.state import ControllerPin, ControllerPinClaim, GitRefSnapshot
from gitopsctr.templates import ParameterTemplateObject


def _stack_tree(root: Path) -> tuple[str, str]:
    stack_uid = "d1-stack-direct"
    template = StackResource(
        cli.GVK(cli.CORE_API_VERSION, "StackTemplate"),
        ResourceMetadata(
            name="preview",
            uid="d1-template",
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked")),
        ),
        StackTemplateSpec(
            parameters=[],
            resources=[
                StackTemplateResource(
                    apiVersion=cli.UNIT_API_VERSION,
                    kind="Terraform",
                    name="preview-app",
                    spec=ParameterTemplateObject({}),
                ),
            ],
        ),
    )
    provenance = StackInstantiationProvenance(
        templateRevision="a" * 40,
        templatePath="deployment/environments/dev/stack-templates/preview.yaml",
        templateDigest="b" * 64,
        requestIdentity="pull-123",
    )
    stack = StackResource(
        cli.GVK(cli.CORE_API_VERSION, "Stack"),
        ResourceMetadata(
            name="preview",
            uid=stack_uid,
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="direct")),
        ),
        DesiredStackSpec(template="preview", parameters=JsonObjectValue({}), provenance=provenance),
    )
    root.mkdir(parents=True)
    (root / "stack-templates").mkdir()
    (root / "stacks").mkdir()
    (root / "stack-templates/preview.json").write_text(
        json.dumps(cli.RESOURCE_CATALOG.serialize_stack_resource(template, profile="desired"))
    )
    (root / "stacks/preview.json").write_text(
        json.dumps(cli.RESOURCE_CATALOG.serialize_stack_resource(stack, profile="desired"))
    )
    unit = cli.RESOURCE_CATALOG.parse_unit(
        {
            "apiVersion": cli.UNIT_API_VERSION,
            "kind": "Terraform",
            "metadata": {
                "name": "preview--preview-app",
                "uid": "d1-preview-app",
                "lifecycle": {
                    "owner": {
                        "apiVersion": cli.CORE_API_VERSION,
                        "kind": "Stack",
                        "name": "preview",
                        "uid": stack_uid,
                    }
                },
            },
            "spec": {
                "source": {
                    "path": ".",
                    "revision": "a" * 40,
                    "inputHash": "sha256:" + "0" * 64,
                    "driverVersion": cli.DRIVER_VERSIONS["terraform"],
                },
                "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
            },
        },
        profile="desired",
        expected_name="preview--preview-app",
    )
    cli.write_desired_candidate_unit(root / "units/preview--preview-app.json", unit, root)
    return stack_uid, "preview--preview-app"


def _args(**overrides: object) -> Namespace:
    values = {
        "environment": "dev",
        "stack": "preview",
        "uid": "d1-stack-direct",
        "desired_ref": "deploy/dev",
        "observed_ref": None,
        "candidate_ref": None,
        "dry": False,
        "deletion_generation": 1,
    }
    values.update(overrides)
    return Namespace(**values)


def _fake_git(*args: str, **_kwargs: object) -> SimpleNamespace:
    if args[0] == "hash-object":
        return SimpleNamespace(stdout=hashlib.sha1(Path(args[1]).read_bytes()).hexdigest() + "\n", returncode=0)
    return SimpleNamespace(stdout="", returncode=0)


def test_request_delete_direct_stack_retains_children_and_creates_fenced_intents(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    published: list[Path] = []
    pin_calls: list[tuple[str, str]] = []
    store = SimpleNamespace(
        create_controller_pin=lambda name, revision: (
            pin_calls.append((name, revision)) or ControllerPin(name, f"refs/heads/gitopsctr/pins/{name}", revision)
        )
    )

    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(cli, "state_store", lambda: store)
    monkeypatch.setattr(cli, "git", _fake_git)
    monkeypatch.setattr(cli, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(cli, "publish_desired_change", publish)
    assert cli.command_request_delete_direct_stack(_args()) is True

    candidate = published[0]
    intent = cli.load_desired_stack_deletion_intents(candidate)["preview"]
    child_intent = cli.load_desired_deletion_intents(candidate)["preview--preview-app"]
    assert intent.uid == "d1-stack-direct"
    assert intent.owned_unit_closure[0].unit_name == "preview--preview-app"
    assert child_intent.retained_owner is not None
    assert child_intent.retained_owner.uid == intent.uid
    assert pin_calls == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]

    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(published[0], output))
    with pytest.raises(OperationError, match="active owned Units"):
        cli.command_finalize_stack(_args())


def test_finalize_stack_removes_root_and_releases_pin_after_children(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    monkeypatch.setattr(cli, "git", _fake_git)
    request_candidate = tmp_path / "request"
    shutil.copytree(current, request_candidate)
    intent = cli._stack_intent_for_resource(
        "dev",
        cli.RESOURCE_CATALOG.parse_stack(
            cli.RESOURCE_CATALOG.load_document(current / "stacks/preview.json"),
            profile="desired",
            expected_name="preview",
        ),
        current / "stacks/preview.json",
        current,
        [cli.load_desired_unit(current / "units/preview--preview-app.json", "preview--preview-app")],
        dry=True,
    )
    cli.write_stack_deletion_intent(request_candidate, intent)
    cli.write_desired_transition_blocks(
        request_candidate,
        {"preview": "deleting", "preview--preview-app": "deleting"},
    )
    (request_candidate / "units/preview--preview-app.json").unlink()
    for path in cli.document_candidates(
        request_candidate / ".gitopsctr/deletion-intents/units", "preview--preview-app"
    ):
        path.unlink()

    published: list[Path] = []
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        cli, "materialize_revision", lambda _revision, output: shutil.copytree(request_candidate, output)
    )
    monkeypatch.setattr(cli, "state_store", lambda: store)
    monkeypatch.setattr(cli, "git", _fake_git)
    monkeypatch.setattr(cli, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "e" * 40, None

    monkeypatch.setattr(cli, "publish_desired_change", publish)
    assert cli.command_finalize_stack(_args()) is True
    assert not list((published[0] / "stacks").glob("preview.*"))
    assert cli.load_desired_stack_deletion_intents(published[0]) == {}
    assert list((published[0] / "stack-templates").glob("preview.*"))
    assert cli.load_desired_stack_incarnation_tombstones(published[0])["preview"].uid == "d1-stack-direct"
    assert released == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]


def test_recover_orphaned_stack_requests_cleanup_for_forge_ineligible_preview(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    published: list[Path] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        create_controller_pin=lambda name, revision: pin,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(cli, "state_store", lambda: store)
    monkeypatch.setattr(cli, "git", _fake_git)
    monkeypatch.setattr(
        cli,
        "preview_eligibility",
        lambda *_args, **_kwargs: SimpleNamespace(status="ineligible", reason="pull request is closed"),
    )
    monkeypatch.setattr(cli, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(cli, "publish_desired_change", publish)
    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is True
    assert cli.load_desired_stack_deletion_intents(published[0])["preview"].uid == "d1-stack-direct"


def test_recover_orphan_pin_releases_only_after_finalized_stack_tombstone(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    cli.write_stack_incarnation_tombstone(
        current,
        cli.StackIncarnationTombstone(stack_name="preview", uid="d1-stack-direct"),
    )
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(cli, "state_store", lambda: store)
    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is True
    assert released == [(pin.name, pin.revision)]


def test_recover_orphan_pin_retains_pin_owned_by_published_direct_stack_candidate(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    candidate = tmp_path / "candidate"
    _stack_tree(candidate)
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    candidate_ref = "candidate/dev/0123456789ab"
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_remote_refs=lambda: (GitRefSnapshot(candidate_ref, "b" * 40),),
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        cli,
        "fetch_ref",
        lambda ref: {"deploy/dev": "c" * 40, candidate_ref: "b" * 40}.get(ref),
    )
    monkeypatch.setattr(
        cli,
        "materialize_revision",
        lambda revision, output: shutil.copytree(candidate if revision == "b" * 40 else current, output),
    )
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}/{id}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is False
    assert released == []


def test_recover_orphan_pin_retains_pin_owned_by_published_deletion_candidate(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    candidate = tmp_path / "candidate"
    stack_uid, unit_name = _stack_tree(candidate)
    stack_path = candidate / "stacks/preview.json"
    stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(stack_path), profile="desired", expected_name="preview"
    )
    intent = cli._stack_intent_for_resource(
        "dev",
        stack,
        stack_path,
        candidate,
        [cli.load_desired_unit(candidate / "units" / f"{unit_name}.json", unit_name)],
        dry=True,
    )
    cli.write_stack_deletion_intent(candidate, intent)
    stack_path.unlink()
    pin = ControllerPin(
        "stacks/dev/preview/" + stack_uid,
        "refs/heads/gitopsctr/pins/stacks/dev/preview/" + stack_uid,
        "a" * 40,
    )
    candidate_ref = "candidate/dev/0123456789ab"
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_remote_refs=lambda: (GitRefSnapshot(candidate_ref, "b" * 40),),
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        cli,
        "fetch_ref",
        lambda ref: {"deploy/dev": "c" * 40, candidate_ref: "b" * 40}.get(ref),
    )
    monkeypatch.setattr(
        cli,
        "materialize_revision",
        lambda revision, output: shutil.copytree(candidate if revision == "b" * 40 else current, output),
    )
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}/{id}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is False
    assert released == []


def test_recover_orphan_pin_without_claim_remains_retained_after_complete_candidate_scan(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_remote_refs=lambda: (),
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda ref: "c" * 40 if ref == "deploy/dev" else None)
    monkeypatch.setattr(cli, "materialize_revision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}/{id}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is False
    assert released == []


def test_recover_orphan_pin_reaps_claim_before_release(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    claim = ControllerPinClaim(
        environment="dev",
        stack_name="preview",
        uid="d1-stack-direct",
        pin_name=pin.name,
        pin_revision=pin.revision,
        target_ref="deploy/dev",
        target_revision="c" * 40,
        candidate_ref="candidate/dev/0123456789ab",
        candidate_revision=None,
        state="active",
        revision="d" * 40,
    )
    released: list[tuple[str, str]] = []
    updates: list[tuple[str, str]] = []
    deleted: list[tuple[str, str]] = []

    def update_claim(updated: ControllerPinClaim, expected: str) -> ControllerPinClaim:
        updates.append((updated.state, expected))
        return replace(updated, revision="e" * 40)

    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_controller_pin_claims=lambda: [claim],
        list_remote_refs=lambda: (),
        update_controller_pin_claim=update_claim,
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
        delete_controller_pin_claim=lambda name, revision: deleted.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda ref: "c" * 40 if ref == "deploy/dev" else None)
    monkeypatch.setattr(cli, "materialize_revision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}/{id}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is True
    assert updates == [("reaping", "d" * 40)]
    assert released == [(pin.name, pin.revision)]
    assert deleted == [(claim.ref.removeprefix("gitopsctr/pin-claims/"), "e" * 40)]


@pytest.mark.parametrize(
    ("claim_revision", "candidate_revision", "fetch_values"),
    [
        (None, None, {"deploy/dev": "f" * 40, "candidate/dev/0123456789ab": "a" * 40}),
        ("a" * 40, "b" * 40, {"deploy/dev": "c" * 40, "candidate/dev/0123456789ab": "e" * 40}),
    ],
)
def test_recover_orphan_pin_retains_claim_when_a_fence_changed(
    tmp_path: Path,
    monkeypatch,
    claim_revision: str | None,
    candidate_revision: str | None,
    fetch_values: dict[str, str],
):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    claim = ControllerPinClaim(
        environment="dev",
        stack_name="preview",
        uid="d1-stack-direct",
        pin_name=pin.name,
        pin_revision=pin.revision,
        target_ref="deploy/dev",
        target_revision="c" * 40,
        candidate_ref="candidate/dev/0123456789ab",
        candidate_revision=claim_revision,
        state="active",
        revision="d" * 40,
    )
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_controller_pin_claims=lambda: [claim],
        list_remote_refs=lambda: (),
        update_controller_pin_claim=lambda *_args: pytest.fail("changed claim must not be reaped"),
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
        delete_controller_pin_claim=lambda *_args: pytest.fail("changed claim must not be deleted"),
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda ref: fetch_values.get(ref))
    monkeypatch.setattr(cli, "materialize_revision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}/{id}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is False
    assert released == []


def test_recover_orphan_pin_uses_custom_candidate_ref_template_without_id(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    _stack_tree(current)
    (current / "stacks/preview.json").unlink()
    (current / "units/preview--preview-app.json").unlink()
    candidate = tmp_path / "candidate"
    _stack_tree(candidate)
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    candidate_ref = "candidate/dev"
    released: list[tuple[str, str]] = []
    store = SimpleNamespace(
        list_controller_pins=lambda: [pin],
        list_remote_refs=lambda: (GitRefSnapshot(candidate_ref, "b" * 40),),
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        cli,
        "fetch_ref",
        lambda ref: {"deploy/dev": "c" * 40, candidate_ref: "b" * 40}.get(ref),
    )
    monkeypatch.setattr(
        cli,
        "materialize_revision",
        lambda revision, output: shutil.copytree(candidate if revision == "b" * 40 else current, output),
    )
    monkeypatch.setattr(cli, "candidate_ref_template", lambda *_args, **_kwargs: "candidate/{environment}")
    monkeypatch.setattr(cli, "state_store", lambda: store)

    args = cli.build_parser().parse_args(
        ["recover-orphaned-stacks", "--environment", "dev", "--desired-ref", "deploy/dev"]
    )

    assert args.handler(args) is False
    assert released == []
