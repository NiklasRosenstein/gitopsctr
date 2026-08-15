"""StackTemplate source ownership across desired-state publication."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import AcceptedDesiredTarget, ControllerPin
from tests.stack_deletion_support import stack_tree
from tests.stack_support import commit, git, project_repository, write_stack_source


def _pin(name: str, revision: str) -> ControllerPin:
    return ControllerPin(name, f"refs/heads/gitopsctr/pins/{name}", revision)


def _source_backed_tree(root: Path) -> tuple[str, str, str]:
    _stack_uid, unit_name = stack_tree(root)
    template_uid = "d1-template"
    path = root / "stack-templates/preview.json"
    document = json.loads(path.read_text())
    revision = "a" * 40
    document["spec"]["sourceContext"] = {"repository": ".", "revision": revision}
    path.write_text(json.dumps(document))
    return template_uid, unit_name, revision


def test_publish_pins_template_source_before_advancing_desired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    events: list[tuple[str, ...]] = []

    def create_pins(revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        events.extend(("pin", name, revision) for name, revision in revisions.items())
        return tuple(_pin(name, revision) for name, revision in revisions.items())

    monkeypatch.setattr(controller, "state_store", lambda: SimpleNamespace(create_controller_pins=create_pins))
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(
        controller,
        "publish_tree",
        lambda *_args, **_kwargs: events.append(("publish",)) or "d" * 40,
    )

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

    expected_name = f"stack-templates/dev/preview/{template_uid}/{source_revision}"
    assert events == [("pin", expected_name, source_revision), ("publish",)]
    assert revision == "d" * 40
    assert outcome is None


def test_pin_acquisition_failure_cannot_publish_desired_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    _source_backed_tree(candidate)
    published = False

    def publish_tree(*_args: object, **_kwargs: object) -> str:
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


def test_successful_publication_releases_attempt_claim_after_atomic_owner_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    canonical = f"stack-templates/dev/preview/{template_uid}/{source_revision}"
    claim = _pin(f"claims/attempt/{canonical}", source_revision)
    released: list[str] = []
    canonical_attempts = 0

    def create_canonical(_revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        nonlocal canonical_attempts
        canonical_attempts += 1
        if canonical_attempts == 1:
            raise OperationError("canonical pin unavailable")
        return (_pin(canonical, source_revision),)

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            create_controller_pin_claims=lambda _revisions, _token: (claim,),
            create_controller_pins=create_canonical,
            list_controller_pins=lambda: (claim,),
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(controller, "publish_tree", lambda *_args, **_kwargs: "d" * 40)

    with pytest.raises(OperationError, match="canonical pin unavailable"):
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
    assert released == [claim.name]

    acquisition = controller.ControllerPinAcquisition(pins=(claim,), newly_created=(claim,), claims=(claim,))
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            create_controller_pins=lambda _revisions: (_pin(canonical, source_revision),),
            list_controller_pins=lambda: (_pin(canonical, source_revision), claim),
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )
    controller._promote_stack_template_source_pins("dev", candidate, acquisition)
    assert released == [claim.name, claim.name]


def test_failed_publication_releases_only_its_attempt_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    _source_backed_tree(candidate)
    claim = _pin("claims/attempt/stack-templates/dev/preview/d1-template/" + "a" * 40, "a" * 40)
    released: list[str] = []
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            create_controller_pin_claims=lambda _revisions, _token: (claim,),
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(
        controller,
        "publish_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "push"])),
    )

    with pytest.raises(subprocess.CalledProcessError):
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
    assert released == [claim.name]


def test_ambiguous_publication_releases_claim_only_after_owner_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    claim = _pin(f"claims/attempt/stack-templates/dev/preview/{template_uid}/{source_revision}", source_revision)
    released: list[str] = []
    promoted: list[dict[str, str]] = []
    verified: list[tuple[str, str | None]] = []

    def fail_promotion(revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        promoted.append(revisions)
        raise OperationError("canonical pin unavailable")

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            create_controller_pin_claims=lambda _revisions, _token: (claim,),
            verify_published_tree=lambda ref, _candidate, parent: verified.append((ref, parent)) or object(),
            create_controller_pins=fail_promotion,
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(
        controller,
        "publish_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "push"])),
    )

    with pytest.raises(subprocess.CalledProcessError):
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

    assert verified == [("deploy/dev", "c" * 40)]
    assert promoted
    assert released == [claim.name]


def test_source_materialization_recovers_from_exact_attempt_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(candidate / "stack-templates/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    canonical = f"stack-templates/dev/preview/{template_uid}/{source_revision}"
    claim = _pin(f"claims/attempt/{canonical}", source_revision)
    git_calls: list[tuple[str, ...]] = []
    materializations = 0

    def fake_git(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        git_calls.append(args)
        return subprocess.CompletedProcess(args, 2 if "ls-remote" in args else 0, "", "")

    def fake_materialize(_revision: str, output: Path) -> None:
        nonlocal materializations
        materializations += 1
        if materializations == 1:
            raise OperationError("source object pruned")
        output.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(git=fake_git, list_controller_pins=lambda: (claim,)),
    )
    monkeypatch.setattr(controller, "materialize_revision", fake_materialize)

    checkout, revision = controller._materialize_template_source_context(
        template,
        candidate,
        "b" * 40,
        candidate,
        "dev",
    )
    assert checkout.name == f".stack-template-source-{source_revision}"
    assert revision == source_revision
    assert any(claim.ref in item for call in git_calls if "fetch" in call for item in call)


def test_pin_validation_failure_creates_no_source_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    _source_backed_tree(candidate)
    created = False

    def create_pins(_revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        nonlocal created
        created = True
        return ()

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(create_controller_pins=create_pins),
    )
    monkeypatch.setattr(
        controller,
        "validate_desired_resource_transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("invalid transition")),
    )
    with pytest.raises(OperationError, match="invalid transition"):
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
            tmp_path / "current",
        )
    assert not created


def test_failed_desired_publication_retains_source_pins_for_race_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    pin_name = f"stack-templates/dev/preview/{template_uid}/{source_revision}"
    live_pins: list[ControllerPin] = []
    released: list[tuple[str, str]] = []

    def create_pins(revisions: dict[str, str]) -> tuple[ControllerPin, ...]:
        # Simulate another writer publishing the same deterministic pin after
        # this attempt's initial absence check.
        live_pins.extend(_pin(name, revision) for name, revision in revisions.items())
        return tuple(live_pins)

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            list_controller_pins=lambda: tuple(live_pins),
            create_controller_pins=create_pins,
            release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")

    def fail_publish(*_args: object, **_kwargs: object) -> str:
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="non-fast-forward")

    monkeypatch.setattr(controller, "publish_tree", fail_publish)
    with pytest.raises(subprocess.CalledProcessError):
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
    # The deterministic pin may have been acquired by a concurrent successful
    # writer after this attempt observed it as absent. Failed publication is
    # therefore monotonic: finalization, not the failed writer, owns cleanup.
    assert released == []
    assert [pin.name for pin in live_pins] == [pin_name]


def test_failed_publication_does_not_release_a_preexisting_live_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    pin_name = f"stack-templates/dev/preview/{template_uid}/{source_revision}"
    live_pin = _pin(pin_name, source_revision)
    released: list[str] = []
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            list_controller_pins=lambda: (live_pin,),
            create_controller_pins=lambda revisions: tuple(
                _pin(name, revision) for name, revision in revisions.items()
            ),
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "direct")
    monkeypatch.setattr(
        controller,
        "publish_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "push"])),
    )

    with pytest.raises(subprocess.CalledProcessError):
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
    assert released == []


def test_template_pin_identity_is_independent_of_partition(tmp_path: Path):
    template_uid, _unit_name, source_revision = _source_backed_tree(tmp_path / "candidate")
    path = tmp_path / "candidate/stack-templates/preview.json"
    document = json.loads(path.read_text())
    document["metadata"]["labels"] = {"gitopsctr.io/partition": "another-partition"}
    path.write_text(json.dumps(document))

    assert controller._required_stack_template_source_pins("dev", path.parent.parent) == (
        (f"stack-templates/dev/preview/{template_uid}/{source_revision}", source_revision),
    )


def test_finalized_template_releases_all_incarnation_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    (candidate / "stack-templates/preview.json").unlink()
    (candidate / "stacks/preview.json").unlink()
    (candidate / "units/preview--preview-app.json").unlink()
    tombstone = controller.ResourceIncarnationTombstone(
        api_version=controller.CORE_API_VERSION,
        kind="StackTemplate",
        name="preview",
        uid=template_uid,
        deletion_generation=1,
    )
    controller.write_resource_incarnation_tombstone(candidate, tombstone)
    older = "b" * 40
    pins = (
        _pin(f"stack-templates/dev/preview/{template_uid}/{source_revision}", source_revision),
        _pin(f"stack-templates/dev/preview/{template_uid}/{older}", older),
    )
    released: list[tuple[str, str]] = []

    def release(name: str, revision: str) -> bool:
        released.append((name, revision))
        return True

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(list_controller_pins=lambda: pins, release_controller_pin=release),
    )

    assert controller._release_finalized_stack_template_pins("dev", "preview", template_uid, 1, candidate) is True
    assert released == [(pin.name, pin.revision) for pin in pins]


def test_finalization_does_not_delete_live_candidate_publication_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "candidate"
    template_uid, _unit_name, source_revision = _source_backed_tree(candidate)
    (candidate / "stack-templates/preview.json").unlink()
    (candidate / "stacks/preview.json").unlink()
    (candidate / "units/preview--preview-app.json").unlink()
    controller.write_resource_incarnation_tombstone(
        candidate,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid=template_uid,
            deletion_generation=1,
        ),
    )
    pin = _pin(f"stack-templates/dev/preview/{template_uid}/{source_revision}", source_revision)
    owner = SimpleNamespace(
        source_pin_name=pin.name,
        revision=source_revision,
        publication_ref="custom/candidate",
        publication_revision="b" * 40,
    )
    released: list[str] = []
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            list_controller_publication_owners=lambda: (owner,),
            publication_owner_is_live_candidate=lambda _owner, _target: True,
            list_controller_pins=lambda: (pin,),
            release_controller_pin=lambda name, _revision: released.append(name) or True,
        ),
    )

    assert (
        controller._release_finalized_stack_template_pins(
            "dev",
            "preview",
            template_uid,
            1,
            candidate,
            AcceptedDesiredTarget("deploy/dev", "c" * 40),
        )
        is False
    )
    assert released == []


def test_finalization_releases_source_pins_for_source_less_tombstone_with_new_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "candidate"
    old_uid, _unit_name, old_revision = _source_backed_tree(candidate)
    template_path = candidate / "stack-templates/preview.json"
    template_document = json.loads(template_path.read_text())
    template_document["metadata"]["uid"] = "new-template"
    template_path.write_text(json.dumps(template_document))
    (candidate / "stacks/preview.json").unlink()
    (candidate / "units/preview--preview-app.json").unlink()
    controller.write_resource_incarnation_tombstone(
        candidate,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid=old_uid,
            deletion_generation=1,
        ),
    )
    pin = _pin(f"stack-templates/dev/preview/{old_uid}/{old_revision}", old_revision)
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            list_controller_pins=lambda: (pin,),
            release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
        ),
    )

    assert controller._release_finalized_stack_template_pins("dev", "preview", old_uid, 1, candidate) is True
    assert released == [(pin.name, pin.revision)]


def test_finalization_refuses_old_uid_references_after_same_name_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = tmp_path / "candidate"
    old_uid, _unit_name, old_revision = _source_backed_tree(candidate)
    template_path = candidate / "stack-templates/preview.json"
    template_document = json.loads(template_path.read_text())
    template_document["metadata"]["uid"] = "new-template"
    template_path.write_text(json.dumps(template_document))
    controller.write_resource_incarnation_tombstone(
        candidate,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid=old_uid,
            deletion_generation=1,
        ),
    )
    pin = _pin(f"stack-templates/dev/preview/{old_uid}/{old_revision}", old_revision)
    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(list_controller_pins=lambda: (pin,)),
    )

    with pytest.raises(OperationError, match="Stacks reference this StackTemplate"):
        controller._release_finalized_stack_template_pins("dev", "preview", old_uid, 1, candidate)


def test_old_incarnation_cleanup_evidence_survives_new_uid_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    desired = tmp_path / "desired"
    desired.mkdir()
    old_uid = "old-template"
    new_uid = "new-template"
    old_revision = "a" * 40
    new_revision = "b" * 40
    controller.write_resource_incarnation_tombstone(
        desired,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid=old_uid,
            deletion_generation=1,
        ),
    )
    old_pin = _pin(f"stack-templates/dev/preview/{old_uid}/{old_revision}", old_revision)
    new_pin = _pin(f"stack-templates/dev/preview/{new_uid}/{new_revision}", new_revision)
    released: list[str] = []
    failed = True

    def release(name: str, _revision: str) -> bool:
        nonlocal failed
        if failed:
            failed = False
            raise OperationError("transient release failure")
        released.append(name)
        return True

    monkeypatch.setattr(
        controller,
        "state_store",
        lambda: SimpleNamespace(
            list_controller_pins=lambda: (old_pin, new_pin),
            release_controller_pin=release,
        ),
    )
    with pytest.raises(OperationError, match="transient release failure"):
        controller._release_finalized_stack_template_pins("dev", "preview", old_uid, 1, desired)

    controller.write_resource_incarnation_tombstone(
        desired,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid=new_uid,
            deletion_generation=1,
        ),
    )
    assert controller._release_finalized_stack_template_pins("dev", "preview", old_uid, 1, desired) is True
    assert released == [old_pin.name]


def test_noop_apply_repairs_a_missing_template_source_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "init", "--bare", str(remote))
    environment = project_repository(source)
    write_stack_source(environment)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    source_revision = commit(source, "publish source-backed StackTemplate")
    git(source, "push", "-u", "origin", "main")
    store = controller.GitStateStore(source)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / ".gitkeep").write_text("")
    store.publish("deploy/dev", baseline, None, "initialize desired state", expected_publication_head=None)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    stack = environment / "stacks/web.json"
    template_path = source / "deployment/stack-templates/preview.json"
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
        str(template_path),
        "-f",
        str(stack),
    ]
    args = controller.build_parser().parse_args(arguments)

    first_revision = controller.command_apply(args)
    assert first_revision is not None
    desired = tmp_path / "desired"
    store.materialize(first_revision, desired)
    template_resource = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(
            controller.document_candidates(desired / "stack-templates", "preview")[0]
        ),
        profile="desired",
        expected_name="preview",
    )
    assert template_resource.metadata.uid is not None
    pin_name = f"stack-templates/dev/preview/{template_resource.metadata.uid}/{source_revision}"
    assert store.release_controller_pin(pin_name, source_revision)
    assert store.list_controller_pins() == ()

    template_file_index = arguments.index(str(template_path))
    stack_only_args = arguments[: template_file_index - 1] + arguments[template_file_index + 1 :]
    partition_index = stack_only_args.index("--partition")
    stack_only_args = stack_only_args[:partition_index] + stack_only_args[partition_index + 2 :]
    assert str(template_path) not in stack_only_args
    assert "--partition" not in stack_only_args
    stack_only_namespace = controller.build_parser().parse_args(stack_only_args)
    assert stack_only_namespace.partition is None
    assert controller.command_apply(stack_only_namespace) == first_revision
    assert store.list_controller_pins() == (
        ControllerPin(pin_name, f"refs/heads/gitopsctr/pins/{pin_name}", source_revision),
    )
