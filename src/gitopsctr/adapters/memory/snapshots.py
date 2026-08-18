"""In-memory snapshot, candidate, retention, and publication conformance adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_hex
from threading import RLock

from gitopsctr.application.model import (
    CandidateStoreId,
    ChannelId,
    ContentId,
    CoordinationChange,
    CoordinationObservation,
    CoordinationResult,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationIntent,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationProof,
    PublicationStoreId,
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SealedCandidate,
    SealedCandidateHandle,
    SnapshotId,
    SourceOwnershipResult,
    SourceSnapshotId,
    _issue_publication_proof,
    _issue_retained_source,
    _issue_sealed_candidate,
    _new_publication_proof_issuer,
)
from gitopsctr.application.snapshots import SnapshotNotFoundError, SnapshotView
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace, MutableWorkspace, WorkspaceEntry


class CandidateWorkspace(MutableWorkspace):
    """Store-issued mutable candidate capability; callers cannot seal it themselves."""

    def __init__(self, _token: object, workspace: InMemoryWorkspace) -> None:
        self._token = _token
        self._workspace = workspace

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._workspace.capabilities

    @property
    def is_mutable(self) -> bool:
        return self._workspace.is_mutable

    @property
    def content_id(self):  # type: ignore[no-untyped-def]
        return self._workspace.content_id

    def list_entries(self, prefix: str | None = None) -> tuple[WorkspaceEntry, ...]:
        return self._workspace.list_entries(prefix)

    def list(self, prefix: str | None = None) -> tuple[WorkspaceEntry, ...]:
        return self._workspace.list(prefix)

    def get_entry(self, key: str) -> WorkspaceEntry:
        return self._workspace.get_entry(key)

    def inspect(self, key: str) -> WorkspaceEntry:
        return self._workspace.inspect(key)

    def read(self, key: str) -> bytes:
        return self._workspace.read(key)

    def entry_content_ids(self):  # type: ignore[no-untyped-def]
        return self._workspace.entry_content_ids()

    def write(self, key: str, content: bytes, *, executable: bool = False) -> None:
        self._workspace.write(key, content, executable=executable)

    def mkdir(self, key: str) -> None:
        self._workspace.mkdir(key)

    def symlink(self, key: str, target: str) -> None:
        self._workspace.symlink(key, target)

    def copy_from(self, source: ImmutableWorkspace, source_key: str, destination_key: str) -> None:
        self._workspace.copy_from(source, source_key, destination_key)

    def delete(self, key: str, *, recursive: bool = False) -> None:
        self._workspace.delete(key, recursive=recursive)


@dataclass
class InMemorySnapshotStore:
    """A deterministic complete transaction adapter with incarnation fencing."""

    _snapshots: dict[SnapshotId, SnapshotView] = field(default_factory=dict, init=False, repr=False)
    _candidate_store_id: CandidateStoreId = field(
        default_factory=lambda: CandidateStoreId(f"memory-candidate-store:{token_hex(16)}"), init=False, repr=False
    )
    _publication_store_id: PublicationStoreId = field(
        default_factory=lambda: PublicationStoreId(f"memory-publication-store:{token_hex(16)}"), init=False, repr=False
    )
    _publication_issuer: object = field(init=False, repr=False)
    _heads: dict[ChannelId, SnapshotId | None] = field(default_factory=dict, init=False, repr=False)
    _head_versions: dict[ChannelId, int] = field(default_factory=dict, init=False, repr=False)
    _candidates: dict[SealedCandidateHandle, tuple[object, SealedCandidate]] = field(
        default_factory=dict, init=False, repr=False
    )
    _candidate_tokens: set[object] = field(default_factory=set, init=False, repr=False)
    _candidate_sequence: int = field(default=0, init=False, repr=False)
    _retention_store_id: RetentionStoreId = field(
        default_factory=lambda: RetentionStoreId(f"memory-retention-store:{token_hex(16)}"), init=False, repr=False
    )
    _retained: dict[RetainedSourceHandle, tuple[SourceSnapshotId, ContentId]] = field(
        default_factory=dict, init=False, repr=False
    )
    _retained_available: set[RetainedSourceHandle] = field(default_factory=set, init=False, repr=False)
    _retention_sequence: int = field(default=0, init=False, repr=False)
    _ownership: dict[SourceSnapshotId, OwnershipObservation] = field(default_factory=dict, init=False, repr=False)
    _ownership_versions: dict[SourceSnapshotId, int] = field(default_factory=dict, init=False, repr=False)
    _coordination: dict[str, CoordinationObservation] = field(default_factory=dict, init=False, repr=False)
    _coordination_versions: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _intent_by_attempt: dict[object, PublicationIntent] = field(default_factory=dict, init=False, repr=False)
    _proof_by_attempt: dict[object, PublicationProof] = field(default_factory=dict, init=False, repr=False)
    _noncommitted_attempts: set[object] = field(default_factory=set, init=False, repr=False)
    _ambiguous_next: bool = field(default=False, init=False, repr=False)
    _unknown_next: bool = field(default=False, init=False, repr=False)
    _transaction_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._publication_issuer = _new_publication_proof_issuer(self._publication_store_id)

    def close(self) -> None:
        """Satisfy the application lifecycle; no external resources are owned."""

    def install(self, snapshot_id: SnapshotId, workspace: ImmutableWorkspace) -> SnapshotView:
        if not isinstance(snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        immutable = InMemoryWorkspace(workspace.list_entries(), capabilities=workspace.capabilities, mutable=False)
        view = SnapshotView(snapshot_id, immutable.content_id, immutable)
        existing = self._snapshots.get(snapshot_id)
        if existing is not None:
            if existing.content_id != view.content_id:
                raise ValueError(f"snapshot ID is already installed with different content: {snapshot_id}")
            return self.open_snapshot(snapshot_id)
        self._snapshots[snapshot_id] = view
        return self.open_snapshot(snapshot_id)

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        try:
            stored = self._snapshots[snapshot_id]
        except KeyError as exc:
            raise SnapshotNotFoundError(f"snapshot does not exist: {snapshot_id}") from exc
        workspace = InMemoryWorkspace(
            stored.workspace.list_entries(), capabilities=stored.workspace.capabilities, mutable=False
        )
        return SnapshotView(stored.snapshot_id, stored.content_id, workspace)

    def set_head(self, channel_id: ChannelId, snapshot_id: SnapshotId) -> HeadObservation:
        self.open_snapshot(snapshot_id)
        self._heads[channel_id] = snapshot_id
        return self._advance_head(channel_id)

    def clear_head(self, channel_id: ChannelId) -> HeadObservation:
        self._heads[channel_id] = None
        return self._advance_head(channel_id)

    def resolve_head(self, channel_id: ChannelId) -> HeadObservation:
        version = self._head_versions.get(channel_id, 0)
        snapshot_id = self._heads.get(channel_id)
        return (
            HeadObservation.absent(channel_id, f"memory:{version}")
            if snapshot_id is None
            else HeadObservation.present(channel_id, snapshot_id, f"memory:{version}")
        )

    def begin_candidate(self, base: ImmutableWorkspace | None = None) -> CandidateWorkspace:
        """Create an adapter-owned mutable candidate from an optional immutable base."""

        if base is not None and not isinstance(base, ImmutableWorkspace):
            raise TypeError("candidate base must implement ImmutableWorkspace")
        entries = () if base is None else base.list_entries()
        capabilities = None if base is None else base.capabilities
        token = object()
        self._candidate_tokens.add(token)
        return CandidateWorkspace(token, InMemoryWorkspace(entries, capabilities=capabilities, mutable=True))

    def seal_candidate(self, workspace: CandidateWorkspace) -> SealedCandidate:
        """Seal only a workspace this store issued, with fresh opaque identities."""

        if not isinstance(workspace, CandidateWorkspace):
            raise TypeError("candidate workspace was not issued by this store")
        if workspace._token not in self._candidate_tokens:
            raise ValueError("candidate workspace was not issued by this store or is already sealed")
        self._candidate_tokens.remove(workspace._token)
        self._candidate_sequence += 1
        handle = SealedCandidateHandle(f"memory-candidate:{token_hex(16)}")
        snapshot_id = SnapshotId(f"memory-candidate-snapshot:{self._candidate_sequence}")
        immutable = InMemoryWorkspace(workspace.list_entries(), capabilities=workspace.capabilities, mutable=False)
        view = self.install(snapshot_id, immutable)
        candidate = _issue_sealed_candidate(handle, self._candidate_store_id, snapshot_id, view.content_id)
        self._candidates[handle] = (workspace._token, candidate)
        return candidate

    def retain_source(self, source: SourceSnapshotId, content_id: ContentId) -> RetainedSource:
        """Issue a durable retention handle for an exact source snapshot."""

        self._retention_sequence += 1
        handle = RetainedSourceHandle(f"memory-retention:{token_hex(16)}")
        self._retained[handle] = (source, content_id)
        self._retained_available.add(handle)
        return _issue_retained_source(handle, self._retention_store_id, source, content_id)

    def make_source_unavailable(self, retained: RetainedSource) -> None:
        """Conformance hook simulating disappearance before publication."""

        retained._validate()
        self._retained_available.discard(retained.handle)

    def ownership(self, source: SourceSnapshotId) -> OwnershipObservation:
        return self._ownership.get(source, OwnershipObservation.absent("memory:0"))

    def set_ownership(self, source: SourceSnapshotId, owner: OwnershipId | None) -> OwnershipObservation:
        version = self._ownership_versions.get(source, 0) + 1
        self._ownership_versions[source] = version
        observation = (
            OwnershipObservation.absent(f"memory:{version}")
            if owner is None
            else OwnershipObservation.present(owner, f"memory:{version}")
        )
        self._ownership[source] = observation
        return observation

    def coordination(self, key: str) -> CoordinationObservation:
        return self._coordination.get(key, CoordinationObservation.absent("memory:0"))

    def make_next_publication_ambiguous(self) -> None:
        self._ambiguous_next = True

    def make_next_publication_unknown(self) -> None:
        """Cause the next attempt to remain unknown without committing state."""

        self._unknown_next = True

    def execute(self, intent: PublicationIntent) -> PublicationOutcome:
        """Atomically commit every fenced change or leave all state untouched."""

        with self._transaction_lock:
            existing = self._intent_by_attempt.get(intent.attempt_id)
            if existing is not None:
                if existing != intent:
                    raise ValueError("publication attempt ID is already bound to a different intent")
                proof = self._proof_by_attempt.get(intent.attempt_id)
                if proof is not None:
                    return PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)
                if intent.attempt_id in self._noncommitted_attempts:
                    return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            self._intent_by_attempt[intent.attempt_id] = intent
            if self._unknown_next:
                self._unknown_next = False
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            try:
                self._validate_transaction(intent)
            except BaseException:
                self._noncommitted_attempts.add(intent.attempt_id)
                raise
            head = self.set_head(intent.channel_id, intent.candidate.snapshot_id)
            ownership = tuple(
                SourceOwnershipResult(
                    change.retained_source.source_snapshot_id,
                    change.next_owner,
                    self.set_ownership(change.retained_source.source_snapshot_id, change.next_owner),
                )
                for change in intent.source_ownership_changes
            )
            coordination = tuple(
                CoordinationResult(change.key, change.next_value, self._apply_coordination(change))
                for change in intent.coordination_changes
            )
            proof = _issue_publication_proof(
                self._publication_store_id,
                self._publication_issuer,
                intent,
                head,
                ownership,
                coordination,
            )
            self._proof_by_attempt[intent.attempt_id] = proof
            if self._ambiguous_next:
                self._ambiguous_next = False
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            return PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    def verify(self, intent: PublicationIntent) -> PublicationOutcome:
        """Resolve an ambiguous attempt to proof, noncommit, or remaining unknown."""

        with self._transaction_lock:
            existing = self._intent_by_attempt.get(intent.attempt_id)
            if existing is None:
                return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
            if existing != intent:
                raise ValueError("publication attempt ID is bound to a different intent")
            if intent.attempt_id in self._noncommitted_attempts:
                return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
            proof = self._proof_by_attempt.get(intent.attempt_id)
            return (
                PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)
                if proof is not None
                else PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            )

    def _validate_transaction(self, intent: PublicationIntent) -> None:
        candidate = self._candidates.get(intent.candidate.handle)
        if intent.candidate.candidate_store_id != self._candidate_store_id:
            raise ValueError("publication candidate was issued by another candidate store")
        if candidate is None or candidate[1] != intent.candidate:
            raise ValueError("publication candidate was not sealed by this store")
        if self.resolve_head(intent.channel_id) != intent.expected_head:
            raise ValueError("publication expected head is stale")
        for change in intent.source_ownership_changes:
            change.retained_source._validate()
            if change.retained_source.retention_store_id != self._retention_store_id:
                raise ValueError("publication retained source was issued by another retention store")
            if self._retained.get(change.retained_source.handle) != (
                change.retained_source.source_snapshot_id,
                change.retained_source.content_id,
            ):
                raise ValueError("publication retained source handle is unknown")
            if change.retained_source.handle not in self._retained_available:
                raise ValueError("publication retained source is unavailable")
            if self.ownership(change.retained_source.source_snapshot_id) != change.expected_ownership:
                raise ValueError("publication source ownership is stale")
        for change in intent.coordination_changes:
            if self.coordination(change.key) != change.expected:
                raise ValueError("publication coordination fence is stale")

    def _apply_coordination(self, change: CoordinationChange) -> CoordinationObservation:
        version = self._coordination_versions.get(change.key, 0) + 1
        self._coordination_versions[change.key] = version
        observation = (
            CoordinationObservation.absent(f"memory:{version}")
            if change.next_value is None
            else CoordinationObservation.present(change.next_value, f"memory:{version}")
        )
        self._coordination[change.key] = observation
        return observation

    def _advance_head(self, channel_id: ChannelId) -> HeadObservation:
        self._head_versions[channel_id] = self._head_versions.get(channel_id, 0) + 1
        return self.resolve_head(channel_id)
