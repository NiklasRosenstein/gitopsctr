from __future__ import annotations

import ast
import base64
import gc
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

import pytest
from dulwich.repo import Repo

from gitopsctr import controller
from gitopsctr.adapters.git.publication import GitPublicationError, GitPublicationStore
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application import (
    ChannelId,
    ContentId,
    CoordinationChange,
    EnvironmentId,
    HmacApplyPublicationIdentityIssuer,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationRecoveryLocator,
    PublicationTarget,
    SealedCandidateHandle,
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
    ReviewAdoptionResult,
)
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.errors import OperationError
from gitopsctr.resource_model import ResourcePlane
from tests.stack_support import cloned_project_repository, commit, git

_DESIRED = ChannelId("desired/dev")
_REVIEW = ChannelId("review/dev")
_ENVIRONMENT = EnvironmentId("dev")
_COORDINATION_KEY = "reviews/dev"


@dataclass(frozen=True)
class _Environment(ReviewAdoptionEnvironmentResolver):
    desired: ChannelId = _DESIRED
    review: ChannelId = _REVIEW

    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        if environment_id != _ENVIRONMENT:
            raise ReviewAdoptionError("unknown environment")
        if desired_channel != self.desired:
            raise ReviewAdoptionError("unknown desired channel")
        if candidate_channel != self.review:
            raise ReviewAdoptionError("unknown review channel")
        return ReviewAdoptionConfiguration(self.desired, self.review)


def _identity() -> HmacApplyPublicationIdentityIssuer:
    return HmacApplyPublicationIdentityIssuer("review-adoption-conformance", "review-adoption-test-seed")


def _coordinator(authority: ApplyPublicationAuthority) -> ReviewAdoptionCoordinator:
    return ReviewAdoptionCoordinator(authority, _identity(), _Environment())


def _candidate(store: Any, content: bytes, *, parent: SnapshotId | None = None):
    workspace = store.begin_candidate(parent_snapshot_id=parent)
    workspace.write("units/app.yaml", content, executable=content.startswith(b"#!"))
    return store.seal_candidate(workspace)


def _publish_base(store: Any):
    candidate = _candidate(store, b"base\n")
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


def _publish_review(
    store: Any,
    *,
    source_changes: tuple[SourceOwnershipChange, ...] = (),
    coordination: tuple[CoordinationChange, ...] | None = None,
):
    base = _publish_base(store)
    candidate = _candidate(store, b"reviewed\n", parent=base.snapshot_id)
    intent = PublicationIntent(
        PublicationAttemptId("review"),
        _REVIEW,
        store.resolve_head(_REVIEW),
        candidate,
        source_changes,
        OwnershipId("review-owner"),
        (
            (CoordinationChange(_COORDINATION_KEY, store.coordination(_COORDINATION_KEY), "requested"),)
            if coordination is None
            else coordination
        ),
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=_ENVIRONMENT,
    )
    outcome = store.execute(intent)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    return intent, store.recovery_locator(intent)


def _memory_accept(store: InMemorySnapshotStore, review: PublicationIntent, locator):
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    return store.observe_review_acceptance(locator)


def _git_store(tmp_path: Path) -> tuple[GitPublicationStore, MemorySourceRepository]:
    repository = tmp_path / "authority.git"
    repository.mkdir(parents=True)
    Repo.init_bare(repository).close()
    source = MemorySourceRepository(SourceId("source"))
    return GitPublicationStore(repository, source), source


def _git_ref(store: GitPublicationStore, ref: str, revision: str | None, old: str | None = None) -> None:
    command = ["git", "-C", str(store.repository), "update-ref"]
    if revision is None:
        command.extend(["-d", ref])
        if old is not None:
            command.append(old)
    else:
        command.extend([ref, revision])
        if old is not None:
            command.append(old)
    subprocess.run(command, check=True, capture_output=True, text=True)


def _revision(snapshot: SnapshotId | None) -> str | None:
    return None if snapshot is None else snapshot.value.removeprefix("git-commit:")


def _private_ref(channel: ChannelId) -> str:
    encoded = base64.urlsafe_b64encode(channel.value.encode()).decode().rstrip("=")
    return f"refs/gitopsctr/publication/v1/channels/{encoded}"


def _authority_ref(channel: ChannelId) -> str:
    encoded = base64.urlsafe_b64encode(channel.value.encode()).decode().rstrip("=")
    return f"refs/gitopsctr/publication/v1/authority/{encoded}"


def _candidate_ref(handle: SealedCandidateHandle) -> str:
    value = handle.value
    return f"refs/gitopsctr/publication/v1/candidates/{value.removeprefix('git-candidate:')}"


def _git_accept(store: GitPublicationStore, review: PublicationIntent, locator):
    base = review.review_base_head
    assert base is not None
    _git_ref(
        store,
        "refs/heads/desired/dev",
        cast(str, _revision(review.candidate.snapshot_id)),
        _revision(base.snapshot_id) or "0" * 40,
    )
    return store.observe_review_acceptance(locator)


def test_memory_rejects_foreign_store_wrong_policy_direct_proof_and_raw_merge():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    accepted_base = review.review_base_head
    assert accepted_base is not None

    with pytest.raises(ValueError, match="another transaction store"):
        _coordinator(InMemorySnapshotStore()).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    with pytest.raises(ReviewAdoptionError, match="unknown desired channel"):
        ReviewAdoptionCoordinator(
            store,
            _identity(),
            _Environment(desired=ChannelId("desired/other")),
        ).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    direct_candidate = _candidate(store, b"direct\n")
    direct = PublicationIntent(
        PublicationAttemptId("direct"),
        ChannelId("desired/other"),
        store.resolve_head(ChannelId("desired/other")),
        direct_candidate,
        (),
        OwnershipId("direct-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert store.execute(direct).state is PublicationOutcomeState.COMMITTED
    with pytest.raises(ValueError, match="does not identify a review candidate"):
        store.observe_review_acceptance(store.recovery_locator(direct))

    # The simulated external merge is observable evidence, not desired authority.
    assert store.resolve_head(_DESIRED) == accepted_base


def test_review_publication_authenticates_original_environment_against_cross_environment_remap():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)

    @dataclass(frozen=True)
    class _AliasedEnvironment(ReviewAdoptionEnvironmentResolver):
        def resolve_review_adoption(
            self,
            environment_id: EnvironmentId,
            desired_channel: ChannelId,
            candidate_channel: ChannelId,
        ) -> ReviewAdoptionConfiguration:
            assert environment_id == EnvironmentId("other")
            return ReviewAdoptionConfiguration(desired_channel, candidate_channel)

    coordinator = ReviewAdoptionCoordinator(store, _identity(), _AliasedEnvironment())
    with pytest.raises(ReviewAdoptionError, match="another environment"):
        coordinator.adopt(ReviewAdoptionCommand(EnvironmentId("other"), locator))


def test_memory_rejects_released_mixed_and_aba_fenced_source_authority():
    store = InMemorySnapshotStore()
    first = store.retain_source(
        SourceSnapshotId(SourceId("first"), SnapshotId("first-snapshot")),
        ContentId("sha256:" + "1" * 64),
    )
    second = store.retain_source(
        SourceSnapshotId(SourceId("second"), SnapshotId("second-snapshot")),
        ContentId("sha256:" + "2" * 64),
    )
    changes = (
        SourceOwnershipChange(first, store.ownership(first.source_snapshot_id), OwnershipId("review-owner")),
        SourceOwnershipChange(second, store.ownership(second.source_snapshot_id), OwnershipId("other-owner")),
    )
    review, locator = _publish_review(store, source_changes=changes)
    _memory_accept(store, review, locator)
    with pytest.raises(ReviewAdoptionError, match="every source"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    retained = store.retain_source(
        SourceSnapshotId(SourceId("source"), SnapshotId("source-snapshot")),
        ContentId("sha256:" + "3" * 64),
    )
    change = SourceOwnershipChange(
        retained,
        store.ownership(retained.source_snapshot_id),
        OwnershipId("review-owner"),
    )
    review, locator = _publish_review(store, source_changes=(change,))
    _memory_accept(store, review, locator)
    store.make_source_unavailable(retained)
    with pytest.raises(ValueError, match="unavailable"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    retained = store.retain_source(
        SourceSnapshotId(SourceId("source"), SnapshotId("source-snapshot")),
        ContentId("sha256:" + "4" * 64),
    )
    change = SourceOwnershipChange(
        retained,
        store.ownership(retained.source_snapshot_id),
        OwnershipId("review-owner"),
    )
    review, locator = _publish_review(store, source_changes=(change,))
    _memory_accept(store, review, locator)
    store.set_ownership(retained.source_snapshot_id, OwnershipId("competitor"))
    store.set_ownership(retained.source_snapshot_id, OwnershipId("review-owner"))
    with pytest.raises(ValueError, match="ownership is stale"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))


def _advance_coordination(store: Any, value: str) -> None:
    channel = ChannelId("coordination-test")
    candidate = _candidate(store, value.encode())
    change = CoordinationChange(_COORDINATION_KEY, store.coordination(_COORDINATION_KEY), value)
    intent = PublicationIntent(
        PublicationAttemptId(f"coordination-{value}"),
        channel,
        store.resolve_head(channel),
        candidate,
        (),
        OwnershipId("coordination-owner"),
        (change,),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert store.execute(intent).state is PublicationOutcomeState.COMMITTED


def test_memory_rejects_coordination_aba_and_allows_one_concurrent_adoption_identity():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    _advance_coordination(store, "competitor")
    _advance_coordination(store, "requested")
    with pytest.raises(ValueError, match="coordination fence is stale"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    command = ReviewAdoptionCommand(_ENVIRONMENT, locator)
    coordinator = _coordinator(store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: coordinator.adopt(command), range(2)))
    assert {result.outcome.state for result in results} == {PublicationOutcomeState.COMMITTED}
    assert len({result.publication.attempt_id for result in results}) == 1
    assert len({result.outcome.proof.proof_id for result in results if result.outcome.proof is not None}) == 1


def _git_retained_source(source: MemorySourceRepository):
    workspace = InMemoryWorkspace((WorkspaceEntry.file("source", b"source\n"),), mutable=False)
    return source.retain(source.install(SnapshotId("source-snapshot"), workspace))


def test_git_ownership_and_coordination_aba_reject_the_same_current_values_as_memory(tmp_path: Path):
    store, source = _git_store(tmp_path)
    retained = _git_retained_source(source)
    source_change = SourceOwnershipChange(
        retained,
        store.ownership(retained.source_snapshot_id),
        OwnershipId("review-owner"),
    )
    review, locator = _publish_review(store, source_changes=(source_change,))
    _git_accept(store, review, locator)
    store.set_ownership(retained.source_snapshot_id, OwnershipId("competitor"))
    current = store.set_ownership(retained.source_snapshot_id, OwnershipId("review-owner"))
    assert current.owner == OwnershipId("review-owner")
    with pytest.raises(ValueError, match="ownership is stale"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store, _source = _git_store(tmp_path / "coordination")
    review, locator = _publish_review(store)
    _git_accept(store, review, locator)
    _advance_coordination(store, "competitor")
    _advance_coordination(store, "requested")
    assert store.coordination(_COORDINATION_KEY).value == "requested"
    with pytest.raises(ValueError, match="coordination fence is stale"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))


def test_review_acceptance_issuance_registry_returns_to_baseline_after_gc_churn():
    from gitopsctr.application import model as application_model

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    store.set_external_review_head(_DESIRED, review.candidate.snapshot_id)
    gc.collect()
    baseline = len(application_model._PUBLICATION_PROOF_ISSUERS)
    observations = [store.observe_review_acceptance(locator) for _index in range(64)]
    assert len(application_model._PUBLICATION_PROOF_ISSUERS) == baseline

    observations.clear()
    del observations
    gc.collect()
    assert len(application_model._PUBLICATION_PROOF_ISSUERS) == baseline


@dataclass(frozen=True)
class _AnyReviewEnvironment(ReviewAdoptionEnvironmentResolver):
    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        assert environment_id == _ENVIRONMENT
        return ReviewAdoptionConfiguration(desired_channel, candidate_channel)


class _NoncommittingAuthority:
    def __init__(self, store: InMemorySnapshotStore) -> None:
        self.store = store
        self.intents: list[PublicationIntent] = []

    def execute(self, intent: PublicationIntent) -> PublicationOutcome:
        self.intents.append(intent)
        return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)

    def __getattr__(self, name: str):
        return getattr(self.store, name)


def _publish_same_candidate_review(
    store: InMemorySnapshotStore,
    candidate: Any,
    base: Any,
    channel: ChannelId,
    attempt: str,
):
    intent = PublicationIntent(
        PublicationAttemptId(attempt),
        channel,
        store.resolve_head(channel),
        candidate,
        (),
        OwnershipId("review-owner"),
        (),
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=_ENVIRONMENT,
    )
    assert store.execute(intent).state is PublicationOutcomeState.COMMITTED
    return store.recovery_locator(intent)


def test_distinct_review_proofs_for_same_candidate_have_distinct_stable_adoption_attempts():
    store = InMemorySnapshotStore()
    base = _publish_base(store)
    candidate = _candidate(store, b"same candidate\n", parent=base.snapshot_id)
    channels = (ChannelId("review/dev/first"), ChannelId("review/dev/second"))
    locators = tuple(
        _publish_same_candidate_review(store, candidate, base, channel, f"review-{index}")
        for index, channel in enumerate(channels)
    )
    store.set_external_review_head(_DESIRED, candidate.snapshot_id)
    authority = _NoncommittingAuthority(store)
    coordinator = ReviewAdoptionCoordinator(
        cast(ApplyPublicationAuthority, authority),
        _identity(),
        _AnyReviewEnvironment(),
    )
    commands = tuple(ReviewAdoptionCommand(_ENVIRONMENT, locator) for locator in locators)

    attempts = tuple(coordinator.adopt(command).publication.attempt_id for command in commands)
    repeated = tuple(coordinator.adopt(command).publication.attempt_id for command in commands)

    assert attempts == repeated
    assert attempts[0] != attempts[1]


@pytest.mark.parametrize(
    "target",
    [
        "desired-public",
        "desired-private",
        "desired-marker",
        "review-public",
        "review-private",
        "review-marker",
        "candidate",
    ],
)
def test_git_adoption_fails_closed_on_public_private_marker_or_candidate_drift(tmp_path: Path, target: str):
    store, _source = _git_store(tmp_path)
    review, locator = _publish_review(store)
    _git_accept(store, review, locator)
    other = _candidate(store, b"other\n")
    other_revision = cast(str, _revision(other.snapshot_id))
    refs = {
        "desired-public": "refs/heads/desired/dev",
        "desired-private": _private_ref(_DESIRED),
        "desired-marker": _authority_ref(_DESIRED),
        "review-public": "refs/heads/review/dev",
        "review-private": _private_ref(_REVIEW),
        "review-marker": _authority_ref(_REVIEW),
        "candidate": _candidate_ref(review.candidate.handle),
    }
    if target == "candidate":
        _git_ref(store, refs[target], None, cast(str, _revision(review.candidate.snapshot_id)))
    else:
        _git_ref(store, refs[target], other_revision)

    with pytest.raises((GitPublicationError, ReviewAdoptionError, ValueError)):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))


class _CountingAuthority:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.verify_calls = 0

    def verify(self, intent: PublicationIntent):
        self.verify_calls += 1
        return self.store.verify(intent)

    def __getattr__(self, name: str):
        return getattr(self.store, name)


@dataclass
class _MutableReviewPolicy(ReviewAdoptionEnvironmentResolver):
    reject: bool = False
    calls: int = 0

    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        self.calls += 1
        if self.reject:
            raise ReviewAdoptionError("current review policy rejected adoption")
        assert environment_id == _ENVIRONMENT
        return ReviewAdoptionConfiguration(desired_channel, candidate_channel)


class _AdoptionCountingAuthority:
    def __init__(self, store: InMemorySnapshotStore) -> None:
        self.store = store
        self.observe_calls = 0
        self.execute_calls = 0

    def observe_review_acceptance(self, locator: PublicationRecoveryLocator):
        self.observe_calls += 1
        return self.store.observe_review_acceptance(locator)

    def execute(self, intent: PublicationIntent):
        self.execute_calls += 1
        return self.store.execute(intent)

    def __getattr__(self, name: str):
        return getattr(self.store, name)


def test_committed_adoption_retry_bypasses_changed_policy_but_new_attempt_does_not():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    authority = _AdoptionCountingAuthority(store)
    policy = _MutableReviewPolicy()
    coordinator = ReviewAdoptionCoordinator(
        cast(ApplyPublicationAuthority, authority),
        _identity(),
        policy,
    )
    command = ReviewAdoptionCommand(_ENVIRONMENT, locator)
    committed = coordinator.adopt(command)
    assert committed.outcome.state is PublicationOutcomeState.COMMITTED

    policy.reject = True
    policy.calls = 0
    authority.observe_calls = 0
    authority.execute_calls = 0
    retried = coordinator.adopt(command)

    assert retried == committed
    assert retried.recovery_locator == committed.recovery_locator
    assert policy.calls == 0
    assert authority.observe_calls == 0
    assert authority.execute_calls == 0

    fresh_store = InMemorySnapshotStore()
    fresh_review, fresh_locator = _publish_review(fresh_store)
    _memory_accept(fresh_store, fresh_review, fresh_locator)
    fresh_authority = _AdoptionCountingAuthority(fresh_store)
    rejecting_policy = _MutableReviewPolicy(reject=True)
    fresh_coordinator = ReviewAdoptionCoordinator(
        cast(ApplyPublicationAuthority, fresh_authority),
        _identity(),
        rejecting_policy,
    )

    with pytest.raises(ReviewAdoptionError, match="current review policy rejected"):
        fresh_coordinator.adopt(ReviewAdoptionCommand(_ENVIRONMENT, fresh_locator))
    assert rejecting_policy.calls == 1
    assert fresh_authority.observe_calls == 0
    assert fresh_authority.execute_calls == 0


def test_memory_unknown_is_verified_once_and_remains_recoverable():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    store.make_next_publication_unknown()
    authority = _CountingAuthority(store)
    coordinator = _coordinator(cast(ApplyPublicationAuthority, authority))

    result = coordinator.adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.UNKNOWN
    assert authority.verify_calls == 1
    assert coordinator.recover(result.recovery_locator).outcome.state is PublicationOutcomeState.UNKNOWN


@pytest.mark.parametrize("crash", ["ref", "authority"])
def test_git_crash_is_verified_once_and_recovers_from_a_fresh_store(tmp_path: Path, crash: str):
    store, source = _git_store(tmp_path)
    review, locator = _publish_review(store)
    _git_accept(store, review, locator)
    if crash == "ref":
        store.make_next_publication_crash_after_ref()
    else:
        store.make_next_publication_crash_after_authority()
    authority = _CountingAuthority(store)

    result = _coordinator(cast(ApplyPublicationAuthority, authority)).adopt(
        ReviewAdoptionCommand(_ENVIRONMENT, locator)
    )

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert authority.verify_calls == 1
    fresh = GitPublicationStore(store.repository, source)
    assert _coordinator(fresh).recover(result.recovery_locator).outcome.state is PublicationOutcomeState.COMMITTED


def test_nested_acceptance_intent_and_result_tampering_fails_closed():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    acceptance = _memory_accept(store, review, locator)
    object.__setattr__(acceptance.accepted_base_head, "incarnation", "forged")
    with pytest.raises((TypeError, ValueError), match="modified|incarnation|binding"):
        acceptance._validate()

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    object.__setattr__(review.candidate, "content_id", ContentId("sha256:" + "9" * 64))
    with pytest.raises((TypeError, ValueError), match="modified|sealed|candidate"):
        _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    result = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    assert result.outcome.proof is not None
    object.__setattr__(result.outcome.proof.resulting_head, "incarnation", "forged")
    with pytest.raises((TypeError, ValueError), match="modified|incarnation|proof"):
        result.__post_init__()


def test_git_raw_external_merge_is_not_authority_and_phase3_reads_adopted_bytes(tmp_path: Path):
    store, _source = _git_store(tmp_path)
    review, locator = _publish_review(store)
    _git_accept(store, review, locator)
    with pytest.raises(GitPublicationError, match="drift"):
        store.resolve_head(_DESIRED)

    result = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    reader = GitSnapshotReader.from_path(store.repository)
    try:
        provider = GitWorkspacePlaneProvider(store.repository, reader)
        visible = provider.snapshot(ResourcePlane.DESIRED, _DESIRED.value)
        assert visible.snapshot_id == result.snapshot_id
        assert visible.workspace.read("units/app.yaml") == b"reviewed\n"
    finally:
        reader.close()


def _authored_unit(path: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                "apiVersion: unit.gitopsctr.io/v1",
                "kind: Terraform",
                "metadata:",
                "  name: application",
                "spec:",
                "  source:",
                "    path: .",
                "",
            )
        )
    )
    return path


def _review_locator(output: Path) -> PublicationRecoveryLocator:
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return PublicationRecoveryLocator.from_wire(values["review_publication"])


def test_reviewed_cli_apply_emits_locator_and_adoption_makes_exact_candidate_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, _legacy_store, _revision = cloned_project_repository(tmp_path, monkeypatch)
    environment_path = source / "deployment/environments/dev/environment.json"
    environment = json.loads(environment_path.read_text())
    environment["spec"]["changeGate"] = "pullRequest"
    environment_path.write_text(json.dumps(environment))
    authored = _authored_unit(source / "application.yaml")
    revision = commit(source, "reviewed application")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    apply_args = controller.build_parser().parse_args(
        [
            "apply",
            "--environment",
            "dev",
            "--source-revision",
            revision,
            "-f",
            str(authored),
        ]
    )

    candidate_revision = controller.command_apply(apply_args)

    assert candidate_revision is not None
    review_locator = _review_locator(output)
    remote = Path(git(source, "remote", "get-url", "origin"))
    desired_ref = "refs/heads/gitopsctr/desired/dev"
    accepted_base = git(remote, "rev-parse", desired_ref)
    candidate_reader = GitSnapshotReader.from_path(remote)
    try:
        candidate_view = candidate_reader.open_snapshot(SnapshotId(f"git-commit:{candidate_revision}"))
    finally:
        candidate_reader.close()
    git(remote, "update-ref", desired_ref, candidate_revision, accepted_base)

    # A raw external fast-forward is not authenticated desired authority.
    raw_store = GitPublicationStore(remote, MemorySourceRepository(SourceId("raw-reader")))
    with pytest.raises(GitPublicationError, match="drift"):
        raw_store.resolve_head(ChannelId("gitopsctr/desired/dev"))

    adopted = controller.command_adopt_review(
        controller.build_parser().parse_args(
            [
                "adopt-review",
                "--environment",
                "dev",
                "--publication",
                review_locator.to_wire(),
            ]
        )
    )

    assert adopted == candidate_revision
    reader = GitSnapshotReader.from_path(remote)
    try:
        provider = GitWorkspacePlaneProvider(source, reader)
        visible = provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev")
        assert visible.workspace.content_id == candidate_view.content_id
        assert visible.workspace.list_entries() == candidate_view.workspace.list_entries()
    finally:
        reader.close()


def test_reviewed_apply_overrides_are_authenticated_and_adoptable_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, _legacy_store, _revision = cloned_project_repository(tmp_path, monkeypatch)
    environment_path = source / "deployment/environments/dev/environment.json"
    environment = json.loads(environment_path.read_text())
    environment["spec"]["changeGate"] = "pullRequest"
    environment_path.write_text(json.dumps(environment))
    authored = _authored_unit(source / "application.yaml")
    revision = commit(source, "reviewed override")
    output = tmp_path / "override-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    desired = ChannelId("override/desired")
    candidate = ChannelId("override/candidate")
    args = controller.build_parser().parse_args(
        [
            "apply",
            "--environment",
            "dev",
            "--source-revision",
            revision,
            "--desired-ref",
            desired.value,
            "--candidate-ref",
            candidate.value,
            "-f",
            str(authored),
        ]
    )
    remote = Path(git(source, "remote", "get-url", "origin"))

    candidate_revision = controller.command_apply(args)
    assert candidate_revision is not None
    locator = _review_locator(output)
    assert git(remote, "rev-parse", f"refs/heads/{candidate.value}") == candidate_revision
    desired_ref = f"refs/heads/{desired.value}"
    accepted_base = git(remote, "rev-parse", desired_ref)
    git(remote, "update-ref", desired_ref, candidate_revision, accepted_base)
    adopted = controller.command_adopt_review(
        controller.build_parser().parse_args(
            ["adopt-review", "--environment", "dev", "--publication", locator.to_wire()]
        )
    )
    assert adopted == candidate_revision


def test_cli_unknown_adoption_emits_locator_and_recovery_command_uses_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    store = InMemorySnapshotStore()
    review, review_locator = _publish_review(store)
    _memory_accept(store, review, review_locator)
    committed = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, review_locator))
    assert committed.snapshot_id is not None
    unknown = ReviewAdoptionResult(
        None,
        committed.publication,
        PublicationOutcome(PublicationOutcomeState.UNKNOWN),
        committed.recovery_locator,
    )

    class _Application:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def adopt_review(self, command: ReviewAdoptionCommand):
            assert command.review_publication == review_locator
            return unknown

        def recover_review_adoption(self, locator: PublicationRecoveryLocator):
            assert locator == committed.recovery_locator
            return committed

    monkeypatch.setattr("gitopsctr.composition.create_default_application", lambda _root: _Application())
    with pytest.raises(OperationError, match="review adoption outcome is unknown") as raised:
        controller.command_adopt_review(
            controller.build_parser().parse_args(
                [
                    "adopt-review",
                    "--environment",
                    "dev",
                    "--publication",
                    review_locator.to_wire(),
                ]
            )
        )
    assert committed.recovery_locator.to_wire() in str(raised.value)

    recovered = controller.command_recover_review_adoption(
        controller.build_parser().parse_args(
            ["recover-review-adoption", "--publication", committed.recovery_locator.to_wire()]
        )
    )
    assert recovered == committed.snapshot_id.value
    assert committed.snapshot_id.value in capsys.readouterr().out


class _ReviewServiceSpy:
    def __init__(self, result: object, *, fail_close: bool = False) -> None:
        self.result = result
        self.fail_close = fail_close
        self.calls: list[tuple[str, object]] = []

    def adopt(self, command: object) -> object:
        self.calls.append(("adopt", command))
        return self.result

    def recover(self, locator: object) -> object:
        self.calls.append(("recover", locator))
        return self.result

    def close(self) -> None:
        self.calls.append(("close", self))
        if self.fail_close:
            raise RuntimeError("review close failed")


class _DependencySpy:
    def __init__(self, name: str, closed: list[str]) -> None:
        self.name = name
        self.closed = closed

    def close(self) -> None:
        self.closed.append(self.name)


def test_application_services_dispatches_exact_values_and_closes_every_dependency_after_failure():
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    result = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    command = ReviewAdoptionCommand(_ENVIRONMENT, locator)
    service = _ReviewServiceSpy(result, fail_close=True)
    closed: list[str] = []
    dependencies = [_DependencySpy(name, closed) for name in ("snapshot", "validator", "resource", "status", "deps")]
    services = ApplicationServices(*cast(Any, dependencies), review_adoption_service=cast(Any, service))

    assert services.adopt_review(command) is result
    assert services.recover_review_adoption(result.recovery_locator) is result
    assert service.calls[:2] == [("adopt", command), ("recover", result.recovery_locator)]
    with pytest.raises(RuntimeError, match="review close failed"):
        services.close()
    assert closed == ["deps", "status", "resource", "validator", "snapshot"]
    services.close()
    assert service.calls.count(("close", service)) == 1


def test_controller_adoption_invokes_one_closed_application_use_case(
    monkeypatch: pytest.MonkeyPatch,
):
    store = InMemorySnapshotStore()
    review, locator = _publish_review(store)
    _memory_accept(store, review, locator)
    result = _coordinator(store).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
    assert result.snapshot_id is not None
    calls: list[ReviewAdoptionCommand] = []

    class _Application:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def adopt_review(self, command: ReviewAdoptionCommand):
            calls.append(command)
            return result

    monkeypatch.setattr("gitopsctr.composition.create_default_application", lambda _root: _Application())

    assert (
        controller.command_adopt_review(
            controller.build_parser().parse_args(
                ["adopt-review", "--environment", "dev", "--publication", locator.to_wire()]
            )
        )
        == result.snapshot_id.value
    )
    assert len(calls) == 1
    assert calls[0] == ReviewAdoptionCommand(_ENVIRONMENT, locator)
    assert tuple(field.name for field in fields(calls[0])) == ("environment_id", "review_publication")

    tree = ast.parse(Path(controller.__file__).read_text(), filename=controller.__file__)
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "command_adopt_review"
    )
    application_calls = [
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "application"
    ]
    assert application_calls == ["adopt_review"]


def test_review_adoption_application_boundary_has_no_controller_path_or_git_imports():
    application = Path(__file__).parents[1] / "src" / "gitopsctr" / "application"
    paths = (
        application / "review_adoption.py",
        application / "apply_orchestration.py",
        application / "model.py",
        application / "services.py",
    )
    forbidden: list[tuple[str, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                targets = (node.module or "",)
            else:
                continue
            for target in targets:
                if target in {"git", "pathlib", "gitopsctr.controller"} or target.startswith(
                    ("git.", "gitopsctr.controller.", "gitopsctr.adapters.git", "dulwich")
                ):
                    forbidden.append((path.name, target))
    assert forbidden == []
