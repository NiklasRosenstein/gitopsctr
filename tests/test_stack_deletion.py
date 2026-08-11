"""UID- and generation-fenced direct Stack deletion requests."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import ControllerPin, ControllerPinClaim
from tests.stack_deletion_support import deletion_args as _args
from tests.stack_deletion_support import fake_git as _fake_git
from tests.stack_deletion_support import stack_tree as _stack_tree


def test_stack_pin_release_validates_claim_before_releasing_pin(monkeypatch):
    pin = ControllerPin(
        name="stacks/dev/preview/d1-stack-direct",
        ref="refs/heads/stacks/dev/preview/d1-stack-direct",
        revision="a" * 40,
    )
    intent = SimpleNamespace(controller_pin=pin, uid="d1-stack-direct", stack_name="preview")
    claim = ControllerPinClaim(
        environment="dev",
        stack_name="preview",
        uid="different-stack",
        pin_name=pin.name,
        pin_revision=pin.revision,
        target_ref="deploy/dev",
        target_revision="b" * 40,
        candidate_ref="candidate/dev",
        candidate_revision=None,
        state="active",
        revision="c" * 40,
    )
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            read_controller_pin_claim=lambda _name: claim,
            release_controller_pin=lambda name, revision: released.append((name, revision)),
        ),
    )

    with pytest.raises(OperationError, match="claim fence"):
        controller._release_stack_controller_pin(intent)
    assert released == []


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

    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(controller, "state_store", lambda: store)
    monkeypatch.setattr(controller, "git", _fake_git)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_request_delete_direct_stack(_args()) is True

    candidate = published[0]
    intent = controller.load_desired_stack_deletion_intents(candidate)["preview"]
    child_intent = controller.load_desired_deletion_intents(candidate)["preview--preview-app"]
    assert intent.uid == "d1-stack-direct"
    assert intent.owned_unit_closure[0].unit_name == "preview--preview-app"
    assert child_intent.retained_owner is not None
    assert child_intent.retained_owner.uid == intent.uid
    assert pin_calls == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]

    monkeypatch.setattr(
        controller, "materialize_revision", lambda _revision, output: shutil.copytree(published[0], output)
    )
    with pytest.raises(OperationError, match="active owned Units"):
        controller.command_finalize_stack(_args())


@pytest.mark.parametrize(
    ("gated", "release_fails", "candidate_override"),
    [
        (False, False, None),
        (True, False, None),
        (True, False, "candidate/explicit"),
        (False, True, None),
    ],
)
def test_finalize_stack_retains_pin_cleanup_until_target_finalization(
    tmp_path: Path,
    monkeypatch,
    gated: bool,
    release_fails: bool,
    candidate_override: str | None,
):
    current = tmp_path / "current"
    _stack_tree(current)
    monkeypatch.setattr(controller, "git", _fake_git)
    request_candidate = tmp_path / "request"
    shutil.copytree(current, request_candidate)
    intent = controller._stack_intent_for_resource(
        "dev",
        controller.RESOURCE_CATALOG.parse_stack(
            controller.RESOURCE_CATALOG.load_document(current / "stacks/preview.json"),
            profile="desired",
            expected_name="preview",
        ),
        current / "stacks/preview.json",
        current,
        [controller.load_desired_unit(current / "units/preview--preview-app.json", "preview--preview-app")],
        dry=True,
    )
    controller.write_stack_deletion_intent(request_candidate, intent)
    controller.write_desired_transition_blocks(
        request_candidate,
        {"preview": "deleting", "preview--preview-app": "deleting"},
    )
    (request_candidate / "units/preview--preview-app.json").unlink()
    for path in controller.document_candidates(
        request_candidate / ".gitopsctr/deletion-intents/units", "preview--preview-app"
    ):
        path.unlink()

    published: list[Path] = []
    released: list[tuple[str, str]] = []
    deleted_claims: list[tuple[str, str]] = []
    pin_available = True
    release_failure_pending = release_fails
    claim = ControllerPinClaim(
        environment="dev",
        stack_name="preview",
        uid=intent.uid,
        pin_name=intent.controller_pin.name,
        pin_revision=intent.controller_pin.revision,
        target_ref="deploy/dev",
        target_revision="c" * 40,
        candidate_ref="candidate/dev",
        candidate_revision=None,
        state="active",
        revision="f" * 40,
    )
    claim_present = True

    def release_pin(name: str, revision: str) -> bool:
        nonlocal pin_available, release_failure_pending
        if release_failure_pending:
            release_failure_pending = False
            raise OperationError("temporary pin release failure")
        if not pin_available:
            return False
        pin_available = False
        released.append((name, revision))
        return True

    def read_claim(_name: str):
        return claim if claim_present else None

    def delete_claim(name: str, revision: str) -> bool:
        nonlocal claim_present
        claim_present = False
        deleted_claims.append((name, revision))
        return True

    store = SimpleNamespace(
        release_controller_pin=release_pin,
        read_controller_pin_claim=read_claim,
        delete_controller_pin_claim=delete_claim,
    )
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        controller, "materialize_revision", lambda _revision, output: shutil.copytree(request_candidate, output)
    )
    monkeypatch.setattr(controller, "state_store", lambda: store)
    monkeypatch.setattr(controller, "git", _fake_git)

    def resolve_candidate(_root, _environment, _operation, _candidate_id, override=None):
        return override or "candidate/dev"

    monkeypatch.setattr(controller, "resolve_candidate_ref", resolve_candidate)

    candidate_refs: list[str] = []

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        candidate_refs.append(_args[2])
        if not gated:
            return "e" * 40, None
        return (
            "e" * 40,
            controller.ManualChangeRequest(
                reason="delegated",
                head=candidate_override or "candidate/dev",
                base="deploy/dev",
                title="Finalize direct Stack preview",
                body="Finalize direct Stack preview.",
                remote_url=None,
            ),
        )

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    if release_fails:
        with pytest.raises(OperationError, match="temporary pin release failure"):
            controller.command_finalize_stack(_args(candidate_ref=candidate_override))
    else:
        assert controller.command_finalize_stack(_args(candidate_ref=candidate_override)) is True
    assert not list((published[0] / "stacks").glob("preview.*"))
    assert controller.load_desired_stack_deletion_intents(published[0])["preview"].uid == "d1-stack-direct"
    assert list((published[0] / "stack-templates").glob("preview.*"))
    assert controller.load_desired_stack_incarnation_tombstones(published[0])["preview"].uid == "d1-stack-direct"
    assert released == ([] if gated or release_fails else [("stacks/dev/preview/d1-stack-direct", "a" * 40)])

    # A gated candidate is now considered finalized only after it is merged. The
    # second invocation models the post-merge cleanup publication and must be
    # able to release the retained pin even though the Stack root is gone.
    monkeypatch.setattr(
        controller, "materialize_revision", lambda _revision, output: shutil.copytree(published[0], output)
    )
    assert controller.command_finalize_stack(_args(candidate_ref=candidate_override)) is True
    assert controller.load_desired_stack_deletion_intents(published[1]) == {}
    assert released == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]
    assert deleted_claims == [("stacks/dev/preview/d1-stack-direct", "f" * 40)]
    if candidate_override is not None:
        assert candidate_refs == [candidate_override, "candidate/dev"]
