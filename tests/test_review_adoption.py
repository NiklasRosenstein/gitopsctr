from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from dulwich.repo import Repo

from gitopsctr import controller
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.review_adoption import (
    GitReviewAdoptionEnvironmentResolver,
    GitReviewAdoptionService,
)
from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application import (
    ApplyResult,
    ChannelId,
    CoordinationChange,
    EnvironmentId,
    HmacApplyPublicationIdentityIssuer,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcomeState,
    PublicationTarget,
    SnapshotId,
    SourceId,
    SourceOwnershipChange,
    SourceSnapshotId,
)
from gitopsctr.application.apply_orchestration import ApplyPublicationAuthority
from gitopsctr.application.review_adoption import (
    ReviewAdoptionCommand,
    ReviewAdoptionConfiguration,
    ReviewAdoptionCoordinator,
    ReviewAdoptionEnvironmentResolver,
    ReviewAdoptionError,
)
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry

_DESIRED = ChannelId("desired/dev")
_REVIEW = ChannelId("review/dev")
_ENVIRONMENT = EnvironmentId("dev")


@dataclass(frozen=True)
class _Environment(ReviewAdoptionEnvironmentResolver):
    environment_id: EnvironmentId = _ENVIRONMENT
    desired: ChannelId = _DESIRED
    review: ChannelId = _REVIEW

    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        if environment_id != self.environment_id:
            raise ReviewAdoptionError("unknown review adoption environment")
        if desired_channel != self.desired:
            raise ReviewAdoptionError("unknown accepted desired channel")
        if candidate_channel != self.review:
            raise ReviewAdoptionError("unknown review candidate channel")
        return ReviewAdoptionConfiguration(self.desired, self.review)


def _identity() -> HmacApplyPublicationIdentityIssuer:
    return HmacApplyPublicationIdentityIssuer("review-tests", "explicit-review-test-seed")


def _candidate(store, value: bytes, *, parent=None):
    workspace = store.begin_candidate(parent_snapshot_id=parent)
    workspace.write("units/app.yaml", value)
    return store.seal_candidate(workspace)


def _publish_base(store):
    candidate = _candidate(store, b"base")
    intent = PublicationIntent(
        PublicationAttemptId("base"),
        _DESIRED,
        store.resolve_head(_DESIRED),
        candidate,
        (),
        OwnershipId("base-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert store.execute(intent).state is PublicationOutcomeState.COMMITTED
    return store.resolve_head(_DESIRED)


def _publish_review(store, retained=None):
    base = _publish_base(store)
    candidate = _candidate(store, b"reviewed", parent=base.snapshot_id)
    source_changes = (
        ()
        if retained is None
        else (
            SourceOwnershipChange(
                retained,
                store.ownership(retained.source_snapshot_id),
                OwnershipId("review-owner"),
            ),
        )
    )
    coordination = (CoordinationChange("reviews/dev", store.coordination("reviews/dev"), "requested"),)
    intent = PublicationIntent(
        PublicationAttemptId("review"),
        _REVIEW,
        store.resolve_head(_REVIEW),
        candidate,
        source_changes,
        OwnershipId("review-owner"),
        coordination,
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=_ENVIRONMENT,
    )
    outcome = store.execute(intent)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    return intent, store.recovery_locator(intent)


def _coordinator(store, environment: _Environment | None = None) -> ReviewAdoptionCoordinator:
    return ReviewAdoptionCoordinator(store, _identity(), environment or _Environment())


def _memory_retained(store: InMemorySnapshotStore, name: str):
    workspace = InMemoryWorkspace((WorkspaceEntry.file("source", name.encode()),), mutable=False)
    return store.retain_source(SourceSnapshotId(SourceId(name), SnapshotId(f"{name}-snapshot")), workspace.content_id)


def test_memory_review_adoption_transfers_exact_authority_and_is_idempotent():
    store = InMemorySnapshotStore()
    source_workspace = InMemoryWorkspace((WorkspaceEntry.file("source", b"source"),), mutable=False)
    retained = store.retain_source(
        source=SourceSnapshotId(SourceId("source"), SnapshotId("source-snapshot")),
        content_id=source_workspace.content_id,
    )

    review, locator = _publish_review(store, retained)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    command = ReviewAdoptionCommand(_ENVIRONMENT, locator)

    result = _coordinator(store).adopt(command)

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert result.snapshot_id == review.candidate.snapshot_id
    assert result.outcome.proof is not None
    assert store.resolve_head(_DESIRED) == result.outcome.proof.resulting_head
    assert store.ownership(retained.source_snapshot_id).owner == result.publication.publication_owner
    assert store.coordination("reviews/dev").value is None
    assert _coordinator(store).adopt(command).outcome.state is PublicationOutcomeState.COMMITTED


def test_committed_retry_precedes_mutable_policy_and_missing_attempt_still_reauthorizes():
    class RejectingEnvironment:
        def __init__(self) -> None:
            self.calls = 0

        def resolve_review_adoption(
            self,
            environment_id: EnvironmentId,
            desired_channel: ChannelId,
            candidate_channel: ChannelId,
        ) -> ReviewAdoptionConfiguration:
            self.calls += 1
            raise ReviewAdoptionError("current review gate rejects adoption")

    class CountingAuthority:
        def __init__(self, store: InMemorySnapshotStore) -> None:
            self.store = store
            self.observe_calls = 0
            self.execute_calls = 0

        def observe_review_acceptance(self, locator):  # type: ignore[no-untyped-def]
            self.observe_calls += 1
            return self.store.observe_review_acceptance(locator)

        def execute(self, intent):  # type: ignore[no-untyped-def]
            self.execute_calls += 1
            return self.store.execute(intent)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self.store, name)

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    command = ReviewAdoptionCommand(_ENVIRONMENT, locator)
    committed = _coordinator(store).adopt(command)
    rejecting = RejectingEnvironment()

    retried = ReviewAdoptionCoordinator(store, _identity(), rejecting).adopt(command)

    assert retried == committed
    assert retried.recovery_locator == committed.recovery_locator
    assert rejecting.calls == 0

    fresh = InMemorySnapshotStore()
    review, locator = _publish_review(fresh)
    fresh.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    counting = CountingAuthority(fresh)
    rejecting = RejectingEnvironment()
    with pytest.raises(ReviewAdoptionError, match="current review gate"):
        ReviewAdoptionCoordinator(cast(ApplyPublicationAuthority, counting), _identity(), rejecting).adopt(
            ReviewAdoptionCommand(_ENVIRONMENT, locator)
        )
    assert rejecting.calls == 1
    assert counting.observe_calls == 0
    assert counting.execute_calls == 0


def test_memory_raw_external_merge_and_cross_environment_proof_do_not_authorize_adoption():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    base = store.resolve_head(_DESIRED)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)

    direct_locator = store.recovery_locator(
        PublicationIntent(
            PublicationAttemptId("unpublished-direct"),
            _DESIRED,
            base,
            review.candidate,
            (),
            OwnershipId("direct-owner"),
            (),
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.DIRECT_ACCEPTED,
        )
    )
    with pytest.raises(ValueError, match="unknown"):
        store.observe_review_acceptance(direct_locator)
    assert store.resolve_head(_DESIRED) == base

    wrong_environment_id = EnvironmentId("other")
    wrong_environment = _Environment(environment_id=wrong_environment_id)
    with pytest.raises(ReviewAdoptionError, match="another environment"):
        _coordinator(store, wrong_environment).adopt(ReviewAdoptionCommand(wrong_environment_id, locator))
    assert store.resolve_head(_DESIRED) == base


def test_review_acceptance_tamper_and_mutable_fences_fail_closed():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    acceptance = store.observe_review_acceptance(locator)
    object.__setattr__(acceptance, "desired_channel", ChannelId("desired/forged"))
    with pytest.raises(TypeError, match="modified after issuance"):
        acceptance._validate()

    acceptance = store.observe_review_acceptance(locator)
    object.__setattr__(acceptance.accepted_base_head, "incarnation", "forged")
    with pytest.raises(TypeError, match="modified after issuance"):
        acceptance._validate()

    # The use case acquires its own evidence after the external state changes.
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.set_external_review_head(_DESIRED, store.resolve_head(_DESIRED).snapshot_id)
    with pytest.raises(ValueError, match="external accepted channel"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))


def test_review_adoption_rechecks_base_review_head_source_and_ownership():
    store = InMemorySnapshotStore()
    source_workspace = InMemoryWorkspace((WorkspaceEntry.file("source", b"source"),), mutable=False)
    retained = store.retain_source(
        SourceSnapshotId(SourceId("source"), SnapshotId("source-snapshot")),
        source_workspace.content_id,
    )
    review, locator = _publish_review(store, retained)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.make_source_unavailable(retained)
    with pytest.raises(ValueError, match="unavailable"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.set_head(_DESIRED, _candidate(store, b"accepted-drift").snapshot_id)
    with pytest.raises(ValueError, match="accepted base"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.set_head(_REVIEW, _candidate(store, b"review-drift").snapshot_id)
    with pytest.raises(ReviewAdoptionError, match="review candidate head"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    retained = _memory_retained(store, "owned")
    review, locator = _publish_review(store, retained)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.set_ownership(retained.source_snapshot_id, OwnershipId("competitor"))
    with pytest.raises(ValueError, match="ownership is stale"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.set_external_review_head(_DESIRED, store.resolve_head(_DESIRED).snapshot_id)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    assert (
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator)).outcome.state
        is PublicationOutcomeState.COMMITTED
    )


def test_adoption_rejects_foreign_store_wrong_target_and_scattered_review_ownership():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    with pytest.raises(ValueError, match="another transaction store"):
        _coordinator(InMemorySnapshotStore()).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    direct = PublicationIntent(
        PublicationAttemptId("direct-proof"),
        _DESIRED,
        store.resolve_head(_DESIRED),
        _candidate(store, b"direct"),
        (),
        OwnershipId("direct-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert store.execute(direct).state is PublicationOutcomeState.COMMITTED
    with pytest.raises(ValueError, match="does not identify a review candidate"):
        store.observe_review_acceptance(store.recovery_locator(direct))

    store = InMemorySnapshotStore()
    base = _publish_base(store)
    first, second = _memory_retained(store, "first"), _memory_retained(store, "second")
    candidate = _candidate(store, b"scattered")
    scattered = PublicationIntent(
        PublicationAttemptId("scattered-review"),
        _REVIEW,
        store.resolve_head(_REVIEW),
        candidate,
        (
            SourceOwnershipChange(first, store.ownership(first.source_snapshot_id), OwnershipId("review-owner")),
            SourceOwnershipChange(second, store.ownership(second.source_snapshot_id), OwnershipId("other-owner")),
        ),
        OwnershipId("review-owner"),
        (),
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=_ENVIRONMENT,
    )
    assert store.execute(scattered).state is PublicationOutcomeState.COMMITTED
    scattered_locator = store.recovery_locator(scattered)
    store.set_external_review_head(_DESIRED, candidate.snapshot_id)
    with pytest.raises(ReviewAdoptionError, match="every source"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, scattered_locator))


class _CountingAuthority:
    def __init__(self, store: InMemorySnapshotStore) -> None:
        self.store = store
        self.verify_calls = 0

    def verify(self, intent):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        return self.store.verify(intent)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.store, name)


def test_unknown_adoption_verifies_once_and_facade_recovers_exact_locator():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    store.make_next_publication_unknown()
    counting = _CountingAuthority(store)
    coordinator = ReviewAdoptionCoordinator(cast(ApplyPublicationAuthority, counting), _identity(), _Environment())
    dependencies = cast(tuple[Any, Any, Any, Any, Any], (store, object(), object(), object(), object()))
    services = ApplicationServices(*dependencies, review_adoption_service=coordinator)

    result = services.adopt_review(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.UNKNOWN
    assert counting.verify_calls == 1
    assert services.recover_review_adoption(result.recovery_locator).outcome.state is PublicationOutcomeState.UNKNOWN


def _git_store(tmp_path: Path):
    publication = tmp_path / "authority.git"
    Repo.init(publication, mkdir=True).close()
    source = MemorySourceRepository(SourceId("source"))
    snapshot = source.install(
        SnapshotId("source-snapshot"),
        InMemoryWorkspace((WorkspaceEntry.file("source", b"source"),), mutable=False),
    )
    return GitPublicationStore(publication, source), source, source.retain(snapshot)


def _external_accept(store: GitPublicationStore, candidate, expected) -> None:
    old = "0" * 40 if expected is None else expected.value.removeprefix("git-commit:")
    new = candidate.snapshot_id.value.removeprefix("git-commit:")
    subprocess.run(
        ["git", "-C", str(store.repository), "update-ref", "refs/heads/desired/dev", new, old],
        check=True,
    )


def test_git_review_adoption_is_publicly_visible_and_freshly_recoverable(tmp_path: Path):
    store, source, retained = _git_store(tmp_path)
    review, locator = _publish_review(store, retained)
    base = review.review_base_head
    assert base is not None
    _external_accept(store, review.candidate, base.snapshot_id)
    fresh = GitPublicationStore(store.repository, source)
    result = _coordinator(fresh).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert store.resolve_head(_DESIRED).snapshot_id == review.candidate.snapshot_id
    recovered_store = GitPublicationStore(store.repository, source)
    assert (
        _coordinator(recovered_store).recover(result.recovery_locator).outcome.state
        is PublicationOutcomeState.COMMITTED
    )


def test_git_review_adoption_public_ref_drift_rejects_without_advancing_authority(tmp_path: Path):
    store, _source, retained = _git_store(tmp_path)
    review, locator = _publish_review(store, retained)
    base = review.review_base_head
    assert base is not None
    _external_accept(store, review.candidate, base.snapshot_id)
    other = _candidate(store, b"other")
    _external_accept(store, other, review.candidate.snapshot_id)

    with pytest.raises(ValueError, match="external accepted channel"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    with pytest.raises(ValueError, match="drift"):
        store.resolve_head(_DESIRED)


@pytest.mark.parametrize(
    "fault",
    ("make_next_publication_crash_after_ref", "make_next_publication_crash_after_authority"),
)
def test_git_review_adoption_crash_windows_have_durable_recovery(tmp_path: Path, fault: str):
    store, source, retained = _git_store(tmp_path)
    review, locator = _publish_review(store, retained)
    base = review.review_base_head
    assert base is not None
    _external_accept(store, review.candidate, base.snapshot_id)
    getattr(store, fault)()

    result = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    fresh = GitPublicationStore(store.repository, source)
    recovered = _coordinator(fresh).recover(result.recovery_locator)
    assert recovered.outcome.state is PublicationOutcomeState.COMMITTED
    assert fresh.resolve_head(_DESIRED).snapshot_id == review.candidate.snapshot_id


def test_git_review_adoption_service_routes_local_origin_authority(tmp_path: Path):
    authority_root = tmp_path / "origin.git"
    worktree = tmp_path / "worktree"
    authority_root.mkdir()
    Repo.init_bare(authority_root).close()
    Repo.init(worktree, mkdir=True).close()
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(authority_root)],
        check=True,
    )
    store = GitPublicationStore(authority_root, MemorySourceRepository(SourceId("source")))
    review, locator = _publish_review(store)
    base = review.review_base_head
    assert base is not None
    _external_accept(store, review.candidate, base.snapshot_id)
    service = GitReviewAdoptionService(worktree, _Environment())

    result = service.adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert (
        GitPublicationStore(authority_root, MemorySourceRepository(SourceId("fresh-source")))
        .resolve_head(_DESIRED)
        .snapshot_id
        == review.candidate.snapshot_id
    )


def test_git_review_environment_resolver_enforces_configured_gate_and_channels(tmp_path: Path):
    repository = tmp_path / "repository"
    shutil.copytree(Path(__file__).parent / "fixtures" / "repository", repository)
    resolver = GitReviewAdoptionEnvironmentResolver(repository)

    configured_desired = ChannelId("gitopsctr/desired/staging")
    configured_candidate = ChannelId("gitopsctr/candidates/staging/review-123")
    configuration = resolver.resolve_review_adoption(EnvironmentId("staging"), configured_desired, configured_candidate)

    assert configuration.desired_channel == ChannelId("gitopsctr/desired/staging")
    assert configuration.candidate_channel == ChannelId("gitopsctr/candidates/staging/review-123")
    overridden = resolver.resolve_review_adoption(
        EnvironmentId("staging"), ChannelId("deploy/staging"), ChannelId("reviews/staging/custom")
    )
    assert overridden == ReviewAdoptionConfiguration(ChannelId("deploy/staging"), ChannelId("reviews/staging/custom"))


def test_controller_apply_emits_review_locator_and_adoption_routes_through_facade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    recovery = store.recover_publication(locator)
    apply_result = ApplyResult(
        review.candidate.snapshot_id,
        PublicationMode.REVIEW_REQUIRED,
        review,
        recovery.outcome,
        locator,
    )
    coordinator = _coordinator(store)

    class Application:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def apply(self, _command):  # type: ignore[no-untyped-def]
            return apply_result

        def adopt_review(self, command):  # type: ignore[no-untyped-def]
            assert command == ReviewAdoptionCommand(_ENVIRONMENT, locator)
            store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
            return coordinator.adopt(command)

        def recover_review_adoption(self, selected):  # type: ignore[no-untyped-def]
            return coordinator.recover(selected)

    monkeypatch.setattr("gitopsctr.composition.create_default_application", lambda _root: Application())
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    apply_args = argparse.Namespace(
        environment="dev",
        files=["unit.yaml"],
        desired_ref=None,
        observed_ref=None,
        candidate_ref=None,
        source_revision=None,
        partition=None,
        dry=False,
        verbose=False,
    )

    assert controller.command_apply(apply_args) == review.candidate.snapshot_id.value
    assert f"review_publication={locator.to_wire()}" in output.read_text()

    adoption = controller.command_adopt_review(argparse.Namespace(environment="dev", publication=locator.to_wire()))
    assert adoption == review.candidate.snapshot_id.value
    adopted_locator = output.read_text().split("adoption_publication=", 1)[1].splitlines()[0]
    assert (
        controller.command_recover_review_adoption(argparse.Namespace(publication=adopted_locator))
        == review.candidate.snapshot_id.value
    )
    assert review.candidate.snapshot_id.value in capsys.readouterr().out
