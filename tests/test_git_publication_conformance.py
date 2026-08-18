from __future__ import annotations

import base64
import json
import secrets
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from dulwich.objects import Commit, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo

from gitopsctr.adapters.git import publication as git_publication
from gitopsctr.adapters.git.publication import (
    CandidateLocator,
    GitPublicationError,
    GitPublicationStore,
    PublicationAttemptLocator,
)
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application import (
    ChannelId,
    CoordinationChange,
    EnvironmentId,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationTarget,
    SourceId,
    SourceOwnershipChange,
    SourceSnapshotId,
)
from gitopsctr.application.sources import SourceRepository, SourceRequest, SourceRetentionError
from gitopsctr.git_local import DulwichLocalRepository

_OWNER = OwnershipId("publication-owner")


def _store(path: Path, source_repository=None) -> GitPublicationStore:
    Repo.init(path, mkdir=True).close()
    return GitPublicationStore(path, source_repository or _empty_source())


def _empty_source() -> MemorySourceRepository:
    return MemorySourceRepository(SourceId("publication-empty-source"))


def _sealed(store: GitPublicationStore, value: bytes = b"candidate"):
    candidate = store.begin_candidate()
    candidate.write("resource.yaml", value)
    return store.seal_candidate(candidate)


def _retained(name: str = "source"):
    memory = InMemorySnapshotStore()
    candidate = memory.begin_candidate()
    candidate.write("source.yaml", name.encode())
    sealed = memory.seal_candidate(candidate)
    return memory.retain_source(SourceSnapshotId(SourceId(name), sealed.snapshot_id), sealed.content_id)


def _git_retained(root: Path):
    root.mkdir()
    source_root = root / "source"
    Repo.init(source_root, mkdir=True).close()
    (source_root / "source.yaml").write_text("source")
    local = DulwichLocalRepository(source_root)
    revision = local.create_commit(
        local.write_tree(source_root), None, "source", "tests", "tests@example.invalid", timestamp=1
    )
    source_id = SourceId("durable-source")
    retention_root = root / "retention"
    retention_root.mkdir()
    source = GitSourceRepository.from_path(source_id, source_root, retention_root)
    snapshot = source.resolve(SourceRequest(source_id, revision))
    return source, source.retain(snapshot)


class _AvailableSource:
    def recover(self, _retained):
        return None


def _intent(
    store: GitPublicationStore,
    *,
    attempt: str = "attempt-1",
    candidate=None,
    source_changes=(),
    expected_head=None,
    coordination=(),
    publication_owner: OwnershipId = _OWNER,
    target: PublicationTarget = PublicationTarget.ACCEPTED_DESIRED,
    mode: PublicationMode = PublicationMode.DIRECT_ACCEPTED,
):
    channel = ChannelId("desired-dev" if target is PublicationTarget.ACCEPTED_DESIRED else "review-dev")
    candidate = candidate or _sealed(store)
    review_base = store.resolve_head(ChannelId("desired-dev")) if mode is PublicationMode.REVIEW_REQUIRED else None
    return PublicationIntent(
        PublicationAttemptId(attempt),
        channel,
        expected_head or store.resolve_head(channel),
        candidate,
        tuple(source_changes),
        publication_owner,
        tuple(coordination),
        target,
        mode,
        review_base_head=review_base,
        environment_id=EnvironmentId("dev") if mode is PublicationMode.REVIEW_REQUIRED else None,
    )


def _shared_sealed(store: Any, value: bytes):
    workspace = store.begin_candidate()
    workspace.write("resource.yaml", value)
    return store.seal_candidate(workspace)


def _shared_intent(
    store: Any,
    *,
    attempt: str,
    source_changes=(),
    coordination=(),
    publication_owner: OwnershipId = _OWNER,
) -> PublicationIntent:
    channel = ChannelId("shared-fences")
    return PublicationIntent(
        PublicationAttemptId(attempt),
        channel,
        store.resolve_head(channel),
        _shared_sealed(store, attempt.encode()),
        tuple(source_changes),
        publication_owner,
        tuple(coordination),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )


def test_git_publication_candidates_are_store_owned_and_direct_or_review_publish(tmp_path: Path):
    store = _store(tmp_path / "repository", _AvailableSource())
    direct = _intent(store)
    outcome = store.execute(direct)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    assert outcome.proof is not None
    assert outcome.proof.resulting_head.snapshot_id == direct.candidate.snapshot_id

    review = _intent(
        store,
        attempt="review",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        coordination=(CoordinationChange("review", store.coordination("review"), "request-1"),),
    )
    review_outcome = store.execute(review)
    assert review_outcome.proof is not None
    assert review_outcome.proof.intent.target is PublicationTarget.REVIEW_CANDIDATE
    assert store.coordination("review").value == "request-1"

    other = _store(tmp_path / "other")
    with pytest.raises(ValueError, match="another candidate store"):
        store.execute(_intent(store, attempt="foreign", candidate=_sealed(other)))


def test_git_publication_fences_absence_head_aba_and_candidate_disappearance(tmp_path: Path):
    store = _store(tmp_path / "repository")
    expected_absence = store.resolve_head(ChannelId("desired-dev"))
    first = _intent(store)
    assert store.execute(first).state is PublicationOutcomeState.COMMITTED
    with pytest.raises(ValueError, match="expected head"):
        store.execute(_intent(store, attempt="absence", expected_head=expected_absence))

    observed = store.resolve_head(first.channel_id)
    alternate = _sealed(store, b"alternate")
    store.set_head(first.channel_id, alternate.snapshot_id)
    store.set_head(first.channel_id, first.candidate.snapshot_id)
    with pytest.raises(ValueError, match="expected head"):
        store.execute(_intent(store, attempt="aba", expected_head=observed))

    raw_observed = store.resolve_head(first.channel_id)
    raw_alternate = _sealed(store, b"raw-alternate")
    repository = Repo(tmp_path / "repository")
    try:
        channel_ref = Ref(git_publication._channel_ref(first.channel_id).encode())
        current = first.candidate.snapshot_id.value.removeprefix("git-commit:").encode()
        alternate = raw_alternate.snapshot_id.value.removeprefix("git-commit:").encode()
        assert repository.refs.set_if_equals(channel_ref, current, alternate)
        assert repository.refs.set_if_equals(channel_ref, alternate, current)
    finally:
        repository.close()
    # Raw mirrors are non-authoritative. Even an external A→B→A sequence
    # cannot manufacture a new authority incarnation or adoption.
    assert store.resolve_head(first.channel_id) == raw_observed
    assert repository is not None
    repository = Repo(tmp_path / "repository")
    try:
        channel_ref = Ref(git_publication._channel_ref(first.channel_id).encode())
        assert repository.refs.set_if_equals(channel_ref, current, alternate)
    finally:
        repository.close()
    with pytest.raises(GitPublicationError, match="channel mirror drift"):
        store.resolve_head(first.channel_id)
    repository = Repo(tmp_path / "repository")
    try:
        channel_ref = Ref(git_publication._channel_ref(first.channel_id).encode())
        assert repository.refs.set_if_equals(channel_ref, alternate, current)
    finally:
        repository.close()

    unavailable = _sealed(store, b"unavailable")
    store.make_candidate_unavailable(unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        store.execute(_intent(store, attempt="unavailable", candidate=unavailable))


def test_git_publication_adopts_source_claim_transfer_release_and_rejects_stale_source(tmp_path: Path):
    store = _store(tmp_path / "repository", _AvailableSource())
    source = _retained()
    claim = _intent(
        store,
        attempt="claim",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
    )
    assert store.execute(claim).proof is not None
    adopted = OwnershipId("adopted")
    transfer = _intent(
        store,
        attempt="transfer",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), adopted),),
    )
    # The transfer owner is explicit in the intent, so it cannot be decorative.
    transfer = PublicationIntent(
        transfer.attempt_id,
        transfer.channel_id,
        transfer.expected_head,
        transfer.candidate,
        transfer.source_ownership_changes,
        adopted,
        transfer.coordination_changes,
        transfer.target,
        transfer.mode,
    )
    assert store.execute(transfer).proof is not None
    release = _intent(
        store,
        attempt="release",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), None),),
        publication_owner=adopted,
    )
    assert store.execute(release).proof is not None
    assert store.ownership(source.source_snapshot_id).is_absent

    stale = store.ownership(source.source_snapshot_id)
    store.set_ownership(source.source_snapshot_id, OwnershipId("other"))
    with pytest.raises(ValueError, match="ownership"):
        store.execute(_intent(store, attempt="stale", source_changes=(SourceOwnershipChange(source, stale, _OWNER),)))


class _DisappearingSource:
    available = True

    def recover(self, _retained):
        if not self.available:
            raise SourceRetentionError("source disappeared")
        return None


def test_git_publication_rejects_source_disappearance_before_ref_mutation(tmp_path: Path):
    verifier = _DisappearingSource()
    store = _store(tmp_path / "repository", verifier)
    source = _retained("disappearing")
    intent = _intent(
        store,
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
    )
    verifier.available = False
    with pytest.raises(SourceRetentionError, match="disappeared"):
        store.execute(intent)
    assert store.resolve_head(intent.channel_id) == intent.expected_head

    without_source = _store(tmp_path / "without-source")
    with pytest.raises(SourceRetentionError, match="unknown or has been released"):
        without_source.execute(
            _intent(
                without_source,
                source_changes=(
                    SourceOwnershipChange(source, without_source.ownership(source.source_snapshot_id), _OWNER),
                ),
            )
        )


def test_git_publication_attempt_binding_ambiguity_and_fresh_adapter_recovery(tmp_path: Path):
    root = tmp_path / "repository"
    store = _store(root)
    intent = _intent(store, attempt="ambiguous")
    store.make_next_publication_ambiguous()
    assert store.execute(intent).state is PublicationOutcomeState.UNKNOWN

    fresh = GitPublicationStore(root, _empty_source())
    verified = fresh.verify(intent)
    assert verified.state is PublicationOutcomeState.COMMITTED
    assert verified.proof is not None
    with pytest.raises(ValueError, match="different intent"):
        fresh.execute(_intent(fresh, attempt="ambiguous", candidate=_sealed(fresh, b"other")))

    unknown = _intent(fresh, attempt="unknown")
    fresh.make_next_publication_unknown()
    assert fresh.execute(unknown).state is PublicationOutcomeState.UNKNOWN
    assert GitPublicationStore(root, _empty_source()).verify(unknown).state is PublicationOutcomeState.UNKNOWN


def test_git_publication_reissues_only_authenticated_candidate_and_attempt_evidence(tmp_path: Path):
    root = tmp_path / "repository"
    store = _store(root)
    intent = _intent(store, attempt="locator")
    committed = store.execute(intent)
    assert committed.state is PublicationOutcomeState.COMMITTED
    assert committed.proof is not None
    candidate_locator = store.candidate_locator(intent.candidate)
    attempt_locator = store.attempt_locator(intent)

    fresh = GitPublicationStore(root, _empty_source())
    assert fresh.reissue_candidate(CandidateLocator.from_wire(candidate_locator.to_wire())) == intent.candidate
    assert fresh.reissue_intent(PublicationAttemptLocator.from_wire(attempt_locator.to_wire())) == intent

    script = """
from pathlib import Path
from gitopsctr.adapters.git.publication import GitPublicationStore, PublicationAttemptLocator
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application import SourceId
import sys
store = GitPublicationStore(Path(sys.argv[1]), MemorySourceRepository(SourceId("publication-empty-source")))
intent = store.reissue_intent(PublicationAttemptLocator.from_wire(sys.argv[2]))
print(store.verify(intent).state.value)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), attempt_locator.to_wire()], capture_output=True, check=True, text=True
    )
    assert completed.stdout.strip() == "committed"


def test_git_publication_subprocess_reissues_retained_source_intent_and_coordination(tmp_path: Path):
    root = tmp_path / "publication"
    source, retained = _git_retained(tmp_path / "durable-source")
    store = _store(root, source)
    intent = _intent(
        store,
        attempt="retained-subprocess",
        source_changes=(SourceOwnershipChange(retained, store.ownership(retained.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("review", store.coordination("review"), "request-1"),),
    )
    committed = store.execute(intent)
    assert committed.state is PublicationOutcomeState.COMMITTED
    assert committed.proof is not None
    locator = store.attempt_locator(intent)

    script = """
from pathlib import Path
import sys
from gitopsctr.adapters.git.publication import GitPublicationStore, PublicationAttemptLocator
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application import SourceId
source = GitSourceRepository.from_path(SourceId("durable-source"), Path(sys.argv[2]), Path(sys.argv[3]))
store = GitPublicationStore(Path(sys.argv[1]), source)
intent = store.reissue_intent(PublicationAttemptLocator.from_wire(sys.argv[4]))
outcome = store.verify(intent)
assert outcome.proof is not None
assert len(outcome.proof.ownership_results) == 1
assert len(outcome.proof.coordination_results) == 1
print(outcome.state.value)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            str(tmp_path / "durable-source" / "source"),
            str(tmp_path / "durable-source" / "retention"),
            locator.to_wire(),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert completed.stdout.strip() == "committed"
    source.release(retained)
    historical_script = """
from pathlib import Path
import sys
from gitopsctr.adapters.git.publication import GitPublicationStore, PublicationAttemptLocator
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application import SourceId
store = GitPublicationStore(Path(sys.argv[1]), MemorySourceRepository(SourceId("publication-empty-source")))
proof = store.reissue_committed_proof(PublicationAttemptLocator.from_wire(sys.argv[2]))
print(proof.proof_id.value)
"""
    historical = subprocess.run(
        [sys.executable, "-c", historical_script, str(root), locator.to_wire()],
        capture_output=True,
        check=True,
        text=True,
    )
    assert historical.stdout.strip() == committed.proof.proof_id.value


def test_git_publication_recovers_the_post_ref_crash_window_and_expected_head_race(tmp_path: Path):
    root = tmp_path / "repository"
    store = _store(root)
    crashed = _intent(store, attempt="crash-window")
    store.make_next_publication_crash_after_ref()
    with pytest.raises(GitPublicationError, match="interruption"):
        store.execute(crashed)
    assert store.resolve_head(crashed.channel_id) == crashed.expected_head
    assert GitPublicationStore(root, _empty_source()).verify(crashed).state is PublicationOutcomeState.COMMITTED

    expected = store.resolve_head(ChannelId("desired-dev"))
    intents = (
        _intent(store, attempt="race-a", expected_head=expected),
        _intent(store, attempt="race-b", expected_head=expected),
    )

    def execute(intent: PublicationIntent) -> PublicationOutcomeState:
        try:
            return store.execute(intent).state
        except ValueError:
            return PublicationOutcomeState.NOT_COMMITTED

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(execute, intents))
    assert outcomes.count(PublicationOutcomeState.COMMITTED) == 1
    assert outcomes.count(PublicationOutcomeState.NOT_COMMITTED) == 1


def test_git_publication_recovers_crash_after_authority_promotion_idempotently(tmp_path: Path):
    root = tmp_path / "repository"
    store = _store(root)
    intent = _intent(store, attempt="authority-crash")
    store.make_next_publication_crash_after_authority()
    with pytest.raises(GitPublicationError, match="authority marker promotion"):
        store.execute(intent)
    fresh = GitPublicationStore(root, _empty_source())
    outcome = fresh.verify(intent)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    assert fresh.resolve_head(intent.channel_id).snapshot_id == intent.candidate.snapshot_id


@pytest.mark.parametrize(
    "tamper",
    (
        "delete-marker",
        "substitute-marker",
        "delete-object",
        "raw-mirror",
        "delete-public",
        "substitute-public",
    ),
)
def test_git_preparing_tamper_preserves_old_authority_without_adoption(tmp_path: Path, tamper: str):
    root = tmp_path / tamper
    source = _retained(f"source-{tamper}")
    store = _store(root, _AvailableSource())
    intent = _intent(
        store,
        attempt=f"prepare-{tamper}",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("gate", store.coordination("gate"), "adopted"),),
    )
    store.make_next_publication_crash_after_ref()
    with pytest.raises(GitPublicationError, match="interruption"):
        store.execute(intent)

    repository = Repo(root)
    try:
        marker_ref = next(
            ref for ref in repository.refs.keys() if ref.startswith(b"refs/gitopsctr/publication/v1/attempts/")
        )
        marker_id = repository.refs[marker_ref]
        channel_ref = Ref(git_publication._channel_ref(intent.channel_id).encode())
        public_ref = Ref(git_publication._public_channel_ref(intent.channel_id).encode())
        candidate_id = intent.candidate.snapshot_id.value.removeprefix("git-commit:").encode()
        if tamper == "delete-marker":
            assert repository.refs.remove_if_equals(marker_ref, marker_id)
        elif tamper == "substitute-marker":
            alternate = _sealed(store, b"foreign-marker").snapshot_id.value.removeprefix("git-commit:").encode()
            assert repository.refs.set_if_equals(marker_ref, marker_id, alternate)
        elif tamper == "delete-object":
            encoded = marker_id.decode()
            marker_path = Path(repository.controldir()) / "objects" / encoded[:2] / encoded[2:]
            marker_path.unlink()
        elif tamper == "raw-mirror":
            alternate = _sealed(store, b"raw-drift").snapshot_id.value.removeprefix("git-commit:").encode()
            assert repository.refs.set_if_equals(channel_ref, candidate_id, alternate)
        elif tamper == "delete-public":
            assert repository.refs.remove_if_equals(public_ref, candidate_id)
        else:
            alternate = _sealed(store, b"public-drift").snapshot_id.value.removeprefix("git-commit:").encode()
            assert repository.refs.set_if_equals(public_ref, candidate_id, alternate)
    finally:
        repository.close()

    fresh = GitPublicationStore(root, _empty_source())
    assert fresh.resolve_head(intent.channel_id) == intent.expected_head
    assert fresh.verify(intent).state is PublicationOutcomeState.UNKNOWN
    assert fresh.ownership(source.source_snapshot_id).is_absent
    assert fresh.coordination("gate").value is None


def test_git_publication_allows_only_one_unresolved_attempt_per_channel(tmp_path: Path):
    store = _store(tmp_path / "repository")
    first = _intent(store, attempt="pending-a")
    store.make_next_publication_unknown()
    assert store.execute(first).state is PublicationOutcomeState.UNKNOWN
    second = _intent(store, attempt="pending-b")
    assert store.execute(second).state is PublicationOutcomeState.UNKNOWN
    assert store.verify(second).state is PublicationOutcomeState.NOT_COMMITTED
    assert store.verify(first).state is PublicationOutcomeState.UNKNOWN
    assert store.resolve_head(first.channel_id) == first.expected_head


def test_git_publication_authority_validates_nested_executable_blob(tmp_path: Path):
    root = tmp_path / "repository"
    store = _store(root)
    workspace = store.begin_candidate()
    workspace.write("bin/tool", b"#!/bin/sh\n", executable=True)
    candidate = store.seal_candidate(workspace)
    intent = _intent(store, attempt="nested", candidate=candidate)
    assert store.execute(intent).state is PublicationOutcomeState.COMMITTED
    assert store.resolve_head(intent.channel_id).snapshot_id == candidate.snapshot_id

    repository = Repo(root)
    try:
        commit = cast(Commit, repository[candidate.snapshot_id.value.removeprefix("git-commit:").encode()])
        tree = cast(Tree, repository[commit.tree])
        nested = cast(Tree, repository[tree[b"bin"][1]])
        blob_id = nested[b"tool"][1].decode()
        object_path = Path(repository.controldir()) / "objects" / blob_id[:2] / blob_id[2:]
    finally:
        repository.close()
    object_path.unlink()
    with pytest.raises(GitPublicationError, match="missing or invalid"):
        store.resolve_head(intent.channel_id)


def test_git_preparing_recovery_never_adopts_when_source_or_fences_changed(tmp_path: Path):
    root = tmp_path / "repository"
    source = _retained("recovery")
    verifier = _DisappearingSource()
    store = _store(root, verifier)
    intent = _intent(
        store,
        attempt="recover-stale",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("review", store.coordination("review"), "request"),),
    )
    store.make_next_publication_crash_after_ref()
    with pytest.raises(GitPublicationError):
        store.execute(intent)
    store.set_ownership(source.source_snapshot_id, OwnershipId("other"))
    verifier.available = False
    fresh = GitPublicationStore(root, cast(SourceRepository, verifier))
    assert fresh.verify(intent).state is PublicationOutcomeState.UNKNOWN
    assert fresh.resolve_head(intent.channel_id) == intent.expected_head


def test_git_preparing_recovery_reopens_durable_source_and_never_adopts_released_retention(tmp_path: Path):
    root = tmp_path / "publication"
    source_root = tmp_path / "durable-source"
    source, retained = _git_retained(source_root)
    store = _store(root, source)
    intent = _intent(
        store,
        attempt="released-retention",
        source_changes=(SourceOwnershipChange(retained, store.ownership(retained.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("review", store.coordination("review"), "request-1"),),
    )
    store.make_next_publication_crash_after_ref()
    with pytest.raises(GitPublicationError):
        store.execute(intent)
    source.release(retained)
    fresh_source = GitSourceRepository.from_path(
        SourceId("durable-source"), source_root / "source", source_root / "retention"
    )
    fresh = GitPublicationStore(root, fresh_source)
    # The preparing raw ref is deliberately hidden and ownership/coordination
    # are not adopted after durable retained evidence has disappeared.
    assert fresh.verify(intent).state is PublicationOutcomeState.UNKNOWN
    assert fresh.resolve_head(intent.channel_id) == intent.expected_head
    assert fresh.ownership(retained.source_snapshot_id).is_absent
    assert fresh.coordination("review").value is None


@pytest.mark.parametrize("backend", ("memory", "git"))
def test_publication_contract_shared_candidate_attempt_and_proof_tamper(tmp_path: Path, backend: str):
    if backend == "memory":
        store = InMemorySnapshotStore()
        other = InMemorySnapshotStore()
        candidate = store.begin_candidate()
        candidate.write("f.yaml", b"frozen")
        sealed = store.seal_candidate(candidate)
        foreign = other.seal_candidate(other.begin_candidate())
    else:
        store = _store(tmp_path / "shared")
        other = _store(tmp_path / "other")
        candidate = store.begin_candidate()
        candidate.write("f.yaml", b"frozen")
        sealed = store.seal_candidate(candidate)
        foreign = other.seal_candidate(other.begin_candidate())
    # The committed snapshot never follows later workspace edits.
    candidate.write("f.yaml", b"changed")
    channel = ChannelId("shared")
    intent = PublicationIntent(
        PublicationAttemptId(f"shared-{backend}"),
        channel,
        store.resolve_head(channel),
        sealed,
        (),
        _OWNER,
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    outcome = store.execute(intent)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    assert outcome.proof is not None
    assert outcome.proof.resulting_head.snapshot_id == sealed.snapshot_id
    with pytest.raises(ValueError, match="another candidate store"):
        store.execute(
            PublicationIntent(
                PublicationAttemptId(f"foreign-{backend}"),
                channel,
                store.resolve_head(channel),
                foreign,
                (),
                _OWNER,
                (),
                PublicationTarget.ACCEPTED_DESIRED,
                PublicationMode.DIRECT_ACCEPTED,
            )
        )
    object.__setattr__(outcome.proof, "resulting_head", intent.expected_head)
    with pytest.raises(ValueError, match="sealed candidate snapshot|authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, outcome.proof)


@pytest.mark.parametrize("backend", ("memory", "git"))
def test_publication_contract_shared_atomic_ownership_and_coordination_aba(tmp_path: Path, backend: str):
    if backend == "memory":
        store: Any = InMemorySnapshotStore()
        source_snapshot = _shared_sealed(store, b"source")
        source = store.retain_source(
            SourceSnapshotId(SourceId("shared-source"), source_snapshot.snapshot_id), source_snapshot.content_id
        )
    else:
        store = _store(tmp_path / "shared-fences", _AvailableSource())
        source = _retained("shared-source")

    claim = _shared_intent(
        store,
        attempt=f"{backend}-claim",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("gate", store.coordination("gate"), "A"),),
    )
    assert store.execute(claim).state is PublicationOutcomeState.COMMITTED
    stale_owner = store.ownership(source.source_snapshot_id)
    stale_coordination = store.coordination("gate")
    other = OwnershipId("other-owner")
    transfer = _shared_intent(
        store,
        attempt=f"{backend}-transfer",
        source_changes=(SourceOwnershipChange(source, stale_owner, other),),
        coordination=(CoordinationChange("gate", stale_coordination, "B"),),
    )
    assert store.execute(transfer).state is PublicationOutcomeState.COMMITTED
    return_to_owner = _shared_intent(
        store,
        attempt=f"{backend}-return",
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _OWNER),),
        coordination=(CoordinationChange("gate", store.coordination("gate"), "A"),),
    )
    assert store.execute(return_to_owner).state is PublicationOutcomeState.COMMITTED
    before_head = store.resolve_head(ChannelId("shared-fences"))
    stale = _shared_intent(
        store,
        attempt=f"{backend}-stale",
        source_changes=(SourceOwnershipChange(source, stale_owner, other),),
        coordination=(CoordinationChange("gate", stale_coordination, "C"),),
    )
    with pytest.raises(ValueError, match="ownership is stale"):
        store.execute(stale)
    # Neither the candidate head nor either keyed adoption changed on the
    # rejected mixed/ABA transaction.
    assert store.resolve_head(ChannelId("shared-fences")) == before_head
    assert store.ownership(source.source_snapshot_id).owner == _OWNER
    assert store.coordination("gate").value == "A"


@pytest.mark.parametrize("backend", ("memory", "git"))
def test_publication_contract_shared_concurrent_expected_head_race(tmp_path: Path, backend: str):
    store: Any = (
        InMemorySnapshotStore() if backend == "memory" else _store(tmp_path / "shared-race", _AvailableSource())
    )
    channel = ChannelId("shared-race")
    expected = store.resolve_head(channel)
    intents = tuple(
        PublicationIntent(
            PublicationAttemptId(f"{backend}-race-{index}"),
            channel,
            expected,
            _shared_sealed(store, f"candidate-{index}".encode()),
            (),
            _OWNER,
            (),
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.DIRECT_ACCEPTED,
        )
        for index in range(2)
    )

    def execute(intent: PublicationIntent) -> PublicationOutcomeState:
        try:
            return store.execute(intent).state
        except ValueError:
            return PublicationOutcomeState.NOT_COMMITTED

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(execute, intents))
    assert outcomes.count(PublicationOutcomeState.COMMITTED) == 1
    assert outcomes.count(PublicationOutcomeState.NOT_COMMITTED) == 1


def test_git_publication_private_metadata_fails_closed_and_concurrent_open_is_safe(tmp_path: Path):
    root = tmp_path / "repository"
    Repo.init(root, mkdir=True).close()
    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = tuple(executor.map(lambda _number: GitPublicationStore(root, _empty_source()), range(8)))
    assert len({store._store_id for store in stores}) == 1

    state_root = root / ".git" / "gitopsctr-publication-v1"
    state_path = state_root / "state.json"
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((root.parent / f".{root.name}.gitopsctr-publication-key").stat().st_mode) == 0o600

    state = json.loads(state_path.read_text())
    state["secret"] = base64.b64encode(secrets.token_bytes(32)).decode()
    state_path.write_text(json.dumps(state))
    with pytest.raises(GitPublicationError, match="corrupted"):
        GitPublicationStore(root, _empty_source())

    state_path.write_text("{")
    with pytest.raises(GitPublicationError, match="corrupted"):
        GitPublicationStore(root, _empty_source())
