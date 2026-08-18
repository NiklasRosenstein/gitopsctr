from __future__ import annotations

import copy
import gc
import hashlib
import json
import pickle
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.application import (
    ChannelId,
    CoordinationChange,
    CoordinationObservation,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
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
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry

_PUBLICATION_OWNER = OwnershipId("publication-owner")


def _sealed(store: InMemorySnapshotStore, value: bytes = b"candidate"):
    candidate = store.begin_candidate()
    candidate.write("resource.yaml", value)
    return store.seal_candidate(candidate)


def _source(store: InMemorySnapshotStore, name: str = "source"):
    sealed = _sealed(store, name.encode())
    return store.retain_source(SourceSnapshotId(SourceId(name), sealed.snapshot_id), sealed.content_id)


def _intent(
    store: InMemorySnapshotStore,
    *,
    attempt: str = "attempt-1",
    candidate=None,
    source_changes=(),
    expected_head=None,
    coordination=(),
    publication_owner: OwnershipId = _PUBLICATION_OWNER,
    target: PublicationTarget = PublicationTarget.ACCEPTED_DESIRED,
    mode: PublicationMode = PublicationMode.DIRECT_ACCEPTED,
):
    channel = ChannelId("desired-dev" if target is PublicationTarget.ACCEPTED_DESIRED else "review-dev")
    candidate = candidate or _sealed(store)
    expected_head = expected_head or store.resolve_head(channel)
    return PublicationIntent(
        PublicationAttemptId(attempt),
        channel,
        expected_head,
        candidate,
        tuple(source_changes),
        publication_owner,
        tuple(coordination),
        target,
        mode,
    )


def test_candidate_is_store_issued_and_sealing_freezes_exact_content():
    store = InMemorySnapshotStore()
    candidate = store.begin_candidate(InMemoryWorkspace((WorkspaceEntry.file("base", b"base"),), mutable=False))
    candidate.write("next", b"before")
    sealed = store.seal_candidate(candidate)
    candidate.write("next", b"after")

    assert store.open_snapshot(sealed.snapshot_id).workspace.read("next") == b"before"
    with pytest.raises(TypeError, match="CandidateStore"):
        type(sealed)(sealed.handle, sealed.snapshot_id, sealed.content_id)


def test_publication_expected_absence_and_head_aba_fail_closed():
    store = InMemorySnapshotStore()
    expected_absence = store.resolve_head(ChannelId("desired-dev"))
    first = _intent(store)
    assert store.execute(first).state is PublicationOutcomeState.COMMITTED
    with pytest.raises(ValueError, match="expected head"):
        store.execute(_intent(store, attempt="expected-absence", expected_head=expected_absence))

    observed = store.resolve_head(first.channel_id)
    alternate = _sealed(store, b"alternate")
    store.set_head(first.channel_id, alternate.snapshot_id)
    store.set_head(first.channel_id, first.candidate.snapshot_id)
    with pytest.raises(ValueError, match="expected head"):
        store.execute(_intent(store, attempt="head-aba", expected_head=observed))


def test_source_ownership_aba_and_source_disappearance_fail_without_partial_updates():
    store = InMemorySnapshotStore()
    source = _source(store)
    original = store.ownership(source.source_snapshot_id)
    store.set_ownership(source.source_snapshot_id, OwnershipId("other"))
    store.set_ownership(source.source_snapshot_id, None)
    intent = _intent(
        store,
        source_changes=(SourceOwnershipChange(source, original, OwnershipId("publication-owner")),),
    )
    before = store.resolve_head(intent.channel_id)
    with pytest.raises(ValueError, match="ownership"):
        store.execute(intent)
    assert store.resolve_head(intent.channel_id) == before
    assert store.verify(intent).state is PublicationOutcomeState.NOT_COMMITTED

    source = _source(store, "disappearing")
    intent = _intent(
        store,
        attempt="source-disappears",
        source_changes=(
            SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), OwnershipId("publication-owner")),
        ),
    )
    store.make_source_unavailable(source)
    with pytest.raises(ValueError, match="unavailable"):
        store.execute(intent)


def test_attempt_collision_candidate_substitution_and_partial_failure_are_rejected():
    store = InMemorySnapshotStore()
    source_one, source_two = _source(store, "one"), _source(store, "two")
    stale = store.ownership(source_two.source_snapshot_id)
    store.set_ownership(source_two.source_snapshot_id, OwnershipId("other"))
    intent = _intent(
        store,
        source_changes=(
            SourceOwnershipChange(
                source_one, store.ownership(source_one.source_snapshot_id), OwnershipId("publication-owner")
            ),
            SourceOwnershipChange(source_two, stale, OwnershipId("publication-owner")),
        ),
    )
    before_head = store.resolve_head(intent.channel_id)
    before_owner = store.ownership(source_one.source_snapshot_id)
    with pytest.raises(ValueError, match="ownership"):
        store.execute(intent)
    assert store.resolve_head(intent.channel_id) == before_head
    assert store.ownership(source_one.source_snapshot_id) == before_owner

    committed = _intent(store, attempt="collision")
    assert store.execute(committed).state is PublicationOutcomeState.COMMITTED
    substituted = _intent(store, attempt="collision", candidate=_sealed(store, b"substituted"))
    with pytest.raises(ValueError, match="different intent"):
        store.execute(substituted)

    foreign = InMemorySnapshotStore()
    foreign_candidate = _sealed(foreign)
    assert foreign_candidate.candidate_store_id != committed.candidate.candidate_store_id
    with pytest.raises(ValueError, match="another candidate store"):
        store.execute(_intent(store, attempt="foreign", candidate=foreign_candidate))


def test_publication_claim_transfer_release_and_owner_participation_are_explicit():
    store = InMemorySnapshotStore()
    source = _source(store)
    claim = _intent(
        store,
        attempt="claim",
        source_changes=(
            SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), OwnershipId("publication-owner")),
        ),
    )
    assert store.execute(claim).proof is not None
    transfer = _intent(
        store,
        attempt="transfer",
        publication_owner=OwnershipId("adopted-owner"),
        source_changes=(
            SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), OwnershipId("adopted-owner")),
        ),
    )
    assert store.execute(transfer).proof is not None
    release = _intent(
        store,
        attempt="release",
        publication_owner=OwnershipId("adopted-owner"),
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), None),),
    )
    proof = store.execute(release).proof
    assert proof is not None
    assert proof.ownership_results[0].source_snapshot_id == source.source_snapshot_id
    assert proof.ownership_results[0].requested_next_owner is None
    assert proof.ownership_results[0].resulting_observation.is_absent
    with pytest.raises(TypeError, match="PublicationTransaction"):
        type(proof)(proof.intent, proof.resulting_head, proof.ownership_results, proof.coordination_results)
    with pytest.raises(TypeError, match="must not be copied"):
        copy.copy(proof)
    with pytest.raises(TypeError, match="must not be serialized"):
        pickle.dumps(proof)

    with pytest.raises(ValueError, match="publication_owner"):
        PublicationIntent(
            PublicationAttemptId("decorative-owner"),
            release.channel_id,
            store.resolve_head(release.channel_id),
            _sealed(store),
            (SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), OwnershipId("other")),),
            OwnershipId("decorative"),
            (),
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.DIRECT_ACCEPTED,
        )


def test_concurrent_expected_absence_allows_exactly_one_committed_attempt():
    store = InMemorySnapshotStore()
    expected = store.resolve_head(ChannelId("desired-dev"))
    intents = (
        _intent(store, attempt="race-a", expected_head=expected),
        _intent(store, attempt="race-b", expected_head=expected),
    )
    barrier = Barrier(2)

    def execute(intent: PublicationIntent):
        barrier.wait()
        try:
            return store.execute(intent).state
        except ValueError:
            return PublicationOutcomeState.NOT_COMMITTED

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(execute, intents))
    assert outcomes.count(PublicationOutcomeState.COMMITTED) == 1
    assert outcomes.count(PublicationOutcomeState.NOT_COMMITTED) == 1


def test_review_publication_produces_candidate_proof_only_and_ambiguity_verifies_to_proof():
    store = InMemorySnapshotStore()
    review = _intent(
        store,
        attempt="review",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        coordination=(CoordinationChange("review-request", CoordinationObservation.absent("memory:0"), "request-1"),),
    )
    proof = store.execute(review).proof
    assert proof is not None
    assert proof.intent.target is PublicationTarget.REVIEW_CANDIDATE
    assert store.coordination("review-request").value == "request-1"

    initial_coordination = store.coordination("review-request")
    second = _intent(
        store,
        attempt="review-second",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        coordination=(CoordinationChange("review-request", initial_coordination, "request-2"),),
    )
    assert store.execute(second).state is PublicationOutcomeState.COMMITTED
    third = _intent(
        store,
        attempt="review-third",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        coordination=(CoordinationChange("review-request", store.coordination("review-request"), "request-1"),),
    )
    assert store.execute(third).state is PublicationOutcomeState.COMMITTED
    stale_coordination = _intent(
        store,
        attempt="review-coordination-aba",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        coordination=(CoordinationChange("review-request", initial_coordination, "request-stale"),),
    )
    with pytest.raises(ValueError, match="coordination"):
        store.execute(stale_coordination)

    with pytest.raises(ValueError, match="review-required"):
        _intent(store, target=PublicationTarget.ACCEPTED_DESIRED, mode=PublicationMode.REVIEW_REQUIRED)

    ambiguous = _intent(store, attempt="ambiguous")
    store.make_next_publication_ambiguous()
    assert store.execute(ambiguous).state is PublicationOutcomeState.UNKNOWN
    verified = store.verify(ambiguous)
    assert verified.state is PublicationOutcomeState.COMMITTED
    assert verified.proof is not None
    assert store.execute(ambiguous).proof == verified.proof

    unknown = _intent(store, attempt="still-unknown")
    head_before_unknown = store.resolve_head(unknown.channel_id)
    store.make_next_publication_unknown()
    assert store.execute(unknown).state is PublicationOutcomeState.UNKNOWN
    assert store.verify(unknown).state is PublicationOutcomeState.UNKNOWN
    assert store.resolve_head(unknown.channel_id) == head_before_unknown


def _committed_review_proof():
    """Return independent complete evidence for one proof-tampering case."""

    store = InMemorySnapshotStore()
    source = _source(store)
    intent = _intent(
        store,
        attempt="tamper-proof",
        target=PublicationTarget.REVIEW_CANDIDATE,
        mode=PublicationMode.REVIEW_REQUIRED,
        source_changes=(SourceOwnershipChange(source, store.ownership(source.source_snapshot_id), _PUBLICATION_OWNER),),
        coordination=(CoordinationChange("review-request", store.coordination("review-request"), "request-1"),),
    )
    proof = store.execute(intent).proof
    assert proof is not None
    return intent, proof


def test_publication_outcome_rejects_transitively_tampered_ownership_proof_evidence():
    intent, proof = _committed_review_proof()
    ownership = proof.ownership_results[0]
    object.__setattr__(ownership, "resulting_observation", OwnershipObservation.absent("memory:tampered"))
    with pytest.raises(ValueError, match="requested next owner"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    ownership = proof.ownership_results[0]
    object.__setattr__(
        ownership,
        "resulting_observation",
        OwnershipObservation.present(OwnershipId("wrong-owner"), "memory:tampered"),
    )
    with pytest.raises(ValueError, match="requested next owner"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    ownership = proof.ownership_results[0]
    object.__setattr__(
        ownership,
        "source_snapshot_id",
        SourceSnapshotId(
            SourceId("wrong-source"), intent.source_ownership_changes[0].retained_source.source_snapshot_id.snapshot_id
        ),
    )
    with pytest.raises(ValueError, match="cover ordered source changes"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    object.__setattr__(proof, "ownership_results", (object(),))
    with pytest.raises(TypeError, match="SourceOwnershipResult"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    ownership = proof.ownership_results[0]
    expected = intent.source_ownership_changes[0].expected_ownership
    object.__setattr__(
        ownership,
        "resulting_observation",
        OwnershipObservation.present(_PUBLICATION_OWNER, expected.incarnation),
    )
    with pytest.raises(ValueError, match="advance every expected ownership incarnation"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)


def test_publication_outcome_rejects_transitively_tampered_coordination_proof_evidence():
    intent, proof = _committed_review_proof()
    coordination = proof.coordination_results[0]
    object.__setattr__(
        coordination,
        "resulting_observation",
        CoordinationObservation.present("wrong-value", "memory:tampered"),
    )
    with pytest.raises(ValueError, match="requested next value"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    coordination = proof.coordination_results[0]
    object.__setattr__(coordination, "key", "wrong-key")
    with pytest.raises(ValueError, match="cover ordered changes"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    object.__setattr__(proof, "coordination_results", (object(),))
    with pytest.raises(TypeError, match="CoordinationResult"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    coordination = proof.coordination_results[0]
    expected = intent.coordination_changes[0].expected
    object.__setattr__(
        coordination,
        "resulting_observation",
        CoordinationObservation.present("request-1", expected.incarnation),
    )
    with pytest.raises(ValueError, match="advance every expected coordination incarnation"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)


def test_publication_outcome_rejects_a_proof_whose_complete_intent_was_tampered():
    intent, proof = _committed_review_proof()
    foreign_store = InMemorySnapshotStore()
    _sealed(foreign_store, b"source")
    foreign_candidate = _sealed(foreign_store)
    assert foreign_candidate.snapshot_id == intent.candidate.snapshot_id
    assert foreign_candidate.candidate_store_id != intent.candidate.candidate_store_id
    object.__setattr__(intent, "candidate", foreign_candidate)
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    object.__setattr__(intent, "channel_id", ChannelId("review-other"))
    with pytest.raises(ValueError, match="channel"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    object.__setattr__(intent, "expected_head", HeadObservation.absent(intent.channel_id, "memory:tampered"))
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    object.__setattr__(intent, "mode", PublicationMode.DIRECT_ACCEPTED)
    with pytest.raises(ValueError, match="accepted publication mode"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    object.__setattr__(
        intent.source_ownership_changes[0], "expected_ownership", OwnershipObservation.absent("memory:tampered")
    )
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    intent, proof = _committed_review_proof()
    object.__setattr__(intent.coordination_changes[0], "next_value", "request-2")
    with pytest.raises(ValueError, match="bind requested next values"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)


def test_publication_outcome_rejects_a_proof_with_a_different_transaction_store():
    _intent, proof = _committed_review_proof()
    object.__setattr__(proof, "publication_store_id", type(proof.publication_store_id)("foreign-publication-store"))
    with pytest.raises(TypeError, match="transaction-store issuance"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)


def test_publication_proof_signature_cannot_be_recomputed_or_copied_from_public_evidence():
    intent, proof = _committed_review_proof()
    object.__setattr__(intent, "expected_head", HeadObservation.absent(intent.channel_id, "memory:tampered"))
    public_digest = hashlib.sha256(
        json.dumps(proof._authenticated_wire_data(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    object.__setattr__(proof, "_signature", public_digest)
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    _other_intent, other = _committed_review_proof()
    object.__setattr__(proof, "_signature", other._signature)
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    _other_intent, other = _committed_review_proof()
    object.__setattr__(proof, "publication_store_id", other.publication_store_id)
    object.__setattr__(proof, "proof_id", other.proof_id)
    object.__setattr__(proof, "_signature", other._signature)
    object.__setattr__(proof, "_issuance", other._issuance)
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    _intent, proof = _committed_review_proof()
    object.__setattr__(proof, "proof_id", type(proof.proof_id)("publication-proof:tampered"))
    with pytest.raises(ValueError, match="authentication"):
        PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)


def test_publication_proof_remains_authenticated_after_store_gc_and_adapter_churn():
    store = InMemorySnapshotStore()
    proof = store.execute(_intent(store, attempt="gc-proof")).proof
    assert proof is not None
    store_ref = weakref.ref(store)
    del store
    gc.collect()
    assert store_ref() is None

    churn = [InMemorySnapshotStore() for _ in range(64)]
    assert len({item._publication_store_id for item in churn}) == len(churn)
    del churn
    gc.collect()

    assert PublicationOutcome(PublicationOutcomeState.COMMITTED, proof).proof == proof
