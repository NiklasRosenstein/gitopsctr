"""Durable local-Git candidate and publication transaction adapter.

Git refs, objects, and the private metadata journal are implementation
details here.  The application sees only candidate, source, ownership, and
publication vocabulary.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, cast

from dulwich.index import commit_tree
from dulwich.objects import Blob, Commit, ObjectID
from dulwich.refs import Ref
from dulwich.repo import Repo

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.application.model import (
    CandidateStoreId,
    ChannelId,
    ContentId,
    CoordinationChange,
    CoordinationObservation,
    CoordinationResult,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationProof,
    PublicationProofId,
    PublicationRecovery,
    PublicationRecoveryLocator,
    PublicationStoreId,
    PublicationTarget,
    RetainedSourceHandle,
    RetentionStoreId,
    ReviewAcceptanceObservation,
    SealedCandidate,
    SealedCandidateHandle,
    SnapshotId,
    SourceId,
    SourceOwnershipChange,
    SourceOwnershipResult,
    SourceSnapshotId,
    _issue_historical_retained_source_evidence,
    _issue_publication_proof,
    _issue_review_acceptance_observation,
    _issue_sealed_candidate,
    _open_publication_proof_issuer,
)
from gitopsctr.application.ports import PublicationExecutionUnknownError, PublicationRecoveryNotFoundError
from gitopsctr.application.sources import RetainedSourceLocator, SourceRepository
from gitopsctr.application.workspace import (
    ImmutableWorkspace,
    InMemoryWorkspace,
    MutableWorkspace,
    WorkspaceCapabilities,
    WorkspaceEntry,
    WorkspaceEntryKind,
)
from gitopsctr.git_local import DulwichLocalRepository

_STATE_VERSION = 2
_STATE_DIRECTORY = "gitopsctr-publication-v1"
_STATE_FILENAME = "state.json"
_LOCK_FILENAME = "state.lock"
_KEY_SUFFIX = ".gitopsctr-publication-key"
_SNAPSHOT_PREFIX = "git-commit:"
_REF_PREFIX = "refs/gitopsctr/publication/v1"


class GitPublicationError(ValueError):
    """The local Git publication boundary cannot complete safely."""


class GitPublicationExecutionUnknownError(GitPublicationError, PublicationExecutionUnknownError):
    """Git crossed a durable publication boundary before execution returned."""


class GitCandidateWorkspace(MutableWorkspace):
    """A mutable logical candidate that only its issuing store may seal."""

    def __init__(self, token: object, workspace: InMemoryWorkspace, parent_snapshot_id: SnapshotId | None) -> None:
        self._token = token
        self._workspace = workspace
        self._parent_snapshot_id = parent_snapshot_id

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
        raise GitPublicationError("Git candidate workspaces do not support explicit directories")

    def symlink(self, key: str, target: str) -> None:
        raise GitPublicationError("Git candidate workspaces do not support symbolic links")

    def copy_from(self, source: ImmutableWorkspace, source_key: str, destination_key: str) -> None:
        self._workspace.copy_from(source, source_key, destination_key)

    def delete(self, key: str, *, recursive: bool = False) -> None:
        self._workspace.delete(key, recursive=recursive)


@dataclass(frozen=True, slots=True)
class CandidateLocator:
    """Untrusted persistable evidence used to reissue one sealed candidate."""

    handle: SealedCandidateHandle
    candidate_store_id: CandidateStoreId
    snapshot_id: SnapshotId
    content_id: ContentId

    def __post_init__(self) -> None:
        for value, expected in (
            (self.handle, SealedCandidateHandle),
            (self.candidate_store_id, CandidateStoreId),
            (self.snapshot_id, SnapshotId),
            (self.content_id, ContentId),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"candidate locator field must be a {expected.__name__}")

    def to_wire(self) -> str:
        return _wire(
            {
                "content": self.content_id.value,
                "handle": self.handle.value,
                "snapshot": self.snapshot_id.value,
                "store": self.candidate_store_id.value,
            }
        )

    @classmethod
    def from_wire(cls, value: str) -> CandidateLocator:
        try:
            data = json.loads(value)
            if not isinstance(data, dict) or set(data) != {"content", "handle", "snapshot", "store"}:
                raise ValueError
            return cls(
                SealedCandidateHandle(data["handle"]),
                CandidateStoreId(data["store"]),
                SnapshotId(data["snapshot"]),
                ContentId(data["content"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("candidate locator is invalid") from exc


@dataclass(frozen=True, slots=True)
class PublicationAttemptLocator:
    """Untrusted store/attempt selector; it confers no publication authority."""

    publication_store_id: PublicationStoreId
    attempt_id: PublicationAttemptId

    def __post_init__(self) -> None:
        if not isinstance(self.publication_store_id, PublicationStoreId):
            raise TypeError("publication_store_id must be a PublicationStoreId")
        if not isinstance(self.attempt_id, PublicationAttemptId):
            raise TypeError("attempt_id must be a PublicationAttemptId")

    def to_wire(self) -> str:
        return _wire({"attempt": self.attempt_id.value, "store": self.publication_store_id.value})

    @classmethod
    def from_wire(cls, value: str) -> PublicationAttemptLocator:
        try:
            data = json.loads(value)
            if not isinstance(data, dict) or set(data) != {"attempt", "store"}:
                raise ValueError
            return cls(PublicationStoreId(data["store"]), PublicationAttemptId(data["attempt"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("publication attempt locator is invalid") from exc


@dataclass(slots=True)
class GitPublicationStore:
    """Store-owned candidates plus crash-recoverable publication over local Git.

    The adapter serializes transactions with a private advisory lock. It first
    durably records a preparing attempt, performs the ref compare-and-swap,
    then atomically records all ownership and coordination adoption. A fresh
    adapter completes a preparing record only when the exact candidate ref is
    visible; otherwise it reports ambiguity rather than guessing.

    Raw Git refs below ``refs/gitopsctr/publication`` are never authority for
    callers.  :meth:`resolve_head` is the authoritative composite observation:
    it combines the ref with authenticated transaction metadata, hides an
    unfinished preparing ref, and advances an incarnation fence when an
    unmanaged raw ref write is detected.
    """

    repository: Path
    source_repository: SourceRepository
    _thread_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _candidate_tokens: set[object] = field(default_factory=set, init=False, repr=False)
    _issuer: object = field(init=False, repr=False)
    _store_id: PublicationStoreId = field(init=False, repr=False)
    _state_secret: bytes = field(init=False, repr=False)
    _ambiguous_next: bool = field(default=False, init=False, repr=False)
    _unknown_next: bool = field(default=False, init=False, repr=False)
    _crash_after_ref_next: bool = field(default=False, init=False, repr=False)
    _crash_after_authority_next: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path):
            raise TypeError("repository must be a Path")
        if self.source_repository is None:
            raise TypeError("source_repository is a required publication dependency")
        with self._locked() as state:
            self._store_id = PublicationStoreId(state["publication_store_id"])
            self._state_secret = self._key_for_store(self._store_id, provision=False)
            self._write_state(state)
            self._issuer = _open_publication_proof_issuer(self._store_id, self._state_secret)

    def close(self) -> None:
        """Close no proof state: issuer metadata deliberately outlives adapters."""

    def begin_candidate(
        self, base: ImmutableWorkspace | None = None, parent_snapshot_id: SnapshotId | None = None
    ) -> GitCandidateWorkspace:
        if base is not None and not isinstance(base, ImmutableWorkspace):
            raise TypeError("candidate base must implement ImmutableWorkspace")
        if base is not None and any(entry.kind is not WorkspaceEntryKind.FILE for entry in base.list_entries()):
            raise GitPublicationError(
                "Git candidate workspaces cannot represent explicit directories or symbolic links"
            )
        if parent_snapshot_id is not None:
            _revision(parent_snapshot_id)
            self._validate_commit_tree(_revision(parent_snapshot_id))
        token = object()
        self._candidate_tokens.add(token)
        return GitCandidateWorkspace(
            token,
            InMemoryWorkspace(
                () if base is None else base.list_entries(),
                capabilities=WorkspaceCapabilities(executable_mode=True),
                mutable=True,
            ),
            parent_snapshot_id,
        )

    def seal_candidate(self, workspace: MutableWorkspace) -> SealedCandidate:
        if not isinstance(workspace, GitCandidateWorkspace) or workspace._token not in self._candidate_tokens:
            raise ValueError("candidate workspace was not issued by this store or is already sealed")
        self._candidate_tokens.remove(workspace._token)
        with self._locked() as state:
            handle = SealedCandidateHandle(f"git-candidate:{secrets.token_urlsafe(32)}")
            while handle.value in state["candidates"]:
                handle = SealedCandidateHandle(f"git-candidate:{secrets.token_urlsafe(32)}")
            revision = self._write_candidate_commit(workspace, handle, workspace._parent_snapshot_id)
            candidate = _issue_sealed_candidate(
                handle,
                CandidateStoreId(state["candidate_store_id"]),
                SnapshotId(f"{_SNAPSHOT_PREFIX}{revision}"),
                workspace.content_id,
            )
            if not self._set_ref_if_equals(_candidate_ref(handle), None, revision):
                raise GitPublicationError("candidate handle unexpectedly already exists")
            state["candidates"][handle.value] = {
                "content": candidate.content_id.value,
                "snapshot": candidate.snapshot_id.value,
            }
            self._write_state(state)
            return candidate

    def candidate_locator(self, candidate: SealedCandidate) -> CandidateLocator:
        candidate._validate()
        return CandidateLocator(
            candidate.handle, candidate.candidate_store_id, candidate.snapshot_id, candidate.content_id
        )

    def reissue_candidate(self, locator: CandidateLocator) -> SealedCandidate:
        """Validate untrusted persisted evidence and issue a fresh capability."""

        if not isinstance(locator, CandidateLocator):
            raise TypeError("locator must be a CandidateLocator")
        with self._locked() as state:
            if locator.candidate_store_id.value != state["candidate_store_id"]:
                raise ValueError("candidate locator belongs to another candidate store")
            record = state["candidates"].get(locator.handle.value)
            if record != {"content": locator.content_id.value, "snapshot": locator.snapshot_id.value}:
                raise ValueError("candidate locator does not match a sealed candidate")
            candidate = _issue_sealed_candidate(
                locator.handle, locator.candidate_store_id, locator.snapshot_id, locator.content_id
            )
            self._validate_candidate_content(candidate)
            return candidate

    def attempt_locator(self, intent: PublicationIntent) -> PublicationAttemptLocator:
        return PublicationAttemptLocator(self._store_id, intent.attempt_id)

    def recovery_locator(self, intent: PublicationIntent) -> PublicationRecoveryLocator:
        intent._validate()
        return PublicationRecoveryLocator(self._store_id, intent.attempt_id)

    def recover_publication(self, locator: PublicationRecoveryLocator) -> PublicationRecovery:
        if not isinstance(locator, PublicationRecoveryLocator):
            raise TypeError("locator must be a PublicationRecoveryLocator")
        adapter_locator = PublicationAttemptLocator(locator.publication_store_id, locator.attempt_id)
        intent = self.reissue_intent(adapter_locator)
        return PublicationRecovery(intent, self.verify(intent))

    def observe_review_acceptance(self, locator: PublicationRecoveryLocator) -> ReviewAcceptanceObservation:
        """Issue evidence only while external desired equals a proven review candidate."""

        if not isinstance(locator, PublicationRecoveryLocator) or locator.publication_store_id != self._store_id:
            raise ValueError("review publication locator belongs to another transaction store")
        with self._locked() as state:
            record = state["attempts"].get(locator.attempt_id.value)
            if not isinstance(record, dict) or record.get("status") != "committed":
                raise ValueError("review publication has no committed proof")
            intent = self._intent_from_wire(json.loads(record["intent"]), state=state)
            proof = self._proof_from_record(intent, record["proof"])
            if (
                intent.mode is not PublicationMode.REVIEW_REQUIRED
                or intent.target is not PublicationTarget.REVIEW_CANDIDATE
                or intent.review_base_head is None
            ):
                raise ValueError("publication proof does not identify a review candidate")
            assert intent.environment_id is not None
            if self._current_head(state, intent.channel_id) != proof.resulting_head:
                raise GitPublicationError("review candidate head drifted from its committed proof")
            self._validate_review_adoption_refs(
                state,
                intent.review_base_head,
                intent.candidate.snapshot_id,
                pending_attempt=None,
            )
            return _issue_review_acceptance_observation(
                self._store_id,
                locator,
                proof.proof_id,
                intent.review_base_head.channel_id,
                intent.review_base_head,
                intent.candidate.snapshot_id,
                intent.candidate.content_id,
                intent.environment_id,
                f"git-review:{intent.review_base_head.incarnation}:{_revision(intent.candidate.snapshot_id)}",
                self._issuer,
            )

    def reissue_intent(self, locator: PublicationAttemptLocator) -> PublicationIntent:
        """Reconstruct an exact intent only from authenticated durable evidence."""

        if not isinstance(locator, PublicationAttemptLocator):
            raise TypeError("locator must be a PublicationAttemptLocator")
        if locator.publication_store_id != self._store_id:
            raise ValueError("publication attempt locator belongs to another transaction store")
        with self._locked() as state:
            record = state["attempts"].get(locator.attempt_id.value)
            if not isinstance(record, dict) or not isinstance(record.get("intent"), str):
                raise PublicationRecoveryNotFoundError("publication attempt locator is unknown")
            try:
                wire = json.loads(record["intent"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise GitPublicationError("authenticated publication intent record is invalid") from exc
        return self._intent_from_wire(wire)

    def reissue_committed_proof(self, locator: PublicationAttemptLocator) -> PublicationProof:
        """Reconstruct historical committed evidence without reviving a source.

        Unlike :meth:`reissue_intent`, this never calls a source retention
        adapter.  Released retention remains unavailable for new work; the
        immutable locator evidence is only enough to verify a prior commit.
        """

        if not isinstance(locator, PublicationAttemptLocator):
            raise TypeError("locator must be a PublicationAttemptLocator")
        if locator.publication_store_id != self._store_id:
            raise ValueError("publication attempt locator belongs to another transaction store")
        with self._locked() as state:
            record = state["attempts"].get(locator.attempt_id.value)
            if not isinstance(record, dict) or record.get("status") != "committed":
                raise ValueError("publication attempt has no committed proof")
            wire = json.loads(record["intent"])
            proof_record = record["proof"]
        return self._proof_from_record(self._intent_from_wire(wire, historical=True), proof_record)

    def resolve_head(self, channel_id: ChannelId) -> HeadObservation:
        if not isinstance(channel_id, ChannelId):
            raise TypeError("channel_id must be a ChannelId")
        with self._locked() as state:
            return self._current_head(state, channel_id)

    def prepare_head(self, channel_id: ChannelId) -> HeadObservation:
        return self.bootstrap_channel(channel_id)

    def bootstrap_channel(self, channel_id: ChannelId) -> HeadObservation:
        """Explicitly adopt one pre-transaction public ref as initial authority.

        This migration-only operation verifies the public object ID while it
        atomically creates the private mirror and authenticated authority
        marker.  Once bootstrapped, any external public-ref drift fails closed
        and is never silently adopted.
        """

        if not isinstance(channel_id, ChannelId):
            raise TypeError("channel_id must be a ChannelId")
        with self._locked() as state:
            if channel_id.value in state["heads"]:
                return self._current_head(state, channel_id)
            public_revision = self._ref_revision(_public_channel_ref(channel_id))
            if public_revision is None:
                return self._current_head(state, channel_id)
            if (
                self._ref_revision(_channel_ref(channel_id)) is not None
                or self._ref_revision(_authority_ref(channel_id)) is not None
            ):
                raise GitPublicationError("managed publication refs exist without bootstrapped authority")
            snapshot = SnapshotId(f"{_SNAPSHOT_PREFIX}{public_revision}")
            self._validate_commit_tree(public_revision)
            expected = HeadObservation.absent(channel_id, "git:0")
            marker = self._write_marker_commit(f"bootstrap:{channel_id.value}", snapshot, expected, "bootstrap")
            if not self._atomic_bootstrap_refs(channel_id, public_revision, marker):
                raise GitPublicationError("public channel changed during authority bootstrap")
            head = self._advance_head(state, channel_id, snapshot, marker)
            self._write_state(state)
            return head

    def set_head(self, channel_id: ChannelId, snapshot_id: SnapshotId) -> HeadObservation:
        """Conformance hook that advances a channel incarnation intentionally."""

        with self._locked() as state:
            current = self._current_head(state, channel_id)
            marker = self._write_marker_commit(
                f"test-head:{secrets.token_urlsafe(12)}", snapshot_id, current, "test-hook"
            )
            previous = state["heads"].get(channel_id.value)
            previous_marker = previous.get("marker") if isinstance(previous, dict) else None
            if not self._atomic_publish_refs(
                channel_id, current.snapshot_id, snapshot_id, marker, test_hook=True, previous_marker=previous_marker
            ):
                raise GitPublicationError("channel changed while setting test head")
            head = self._advance_head(state, channel_id, snapshot_id, marker)
            self._write_state(state)
            return head

    def clear_head(self, channel_id: ChannelId) -> HeadObservation:
        with self._locked() as state:
            current = self._current_head(state, channel_id)
            # Explicit clears are test-only and remove both mirrors/authority.
            if not self._atomic_clear_refs(channel_id, current.snapshot_id, state["heads"].get(channel_id.value)):
                raise GitPublicationError("channel changed while clearing test head")
            head = self._advance_head(state, channel_id, None, None)
            self._write_state(state)
            return head

    def ownership(self, source: SourceSnapshotId) -> OwnershipObservation:
        with self._locked() as state:
            return _ownership_observation(state["ownership"].get(source.to_wire()))

    def set_ownership(self, source: SourceSnapshotId, owner: OwnershipId | None) -> OwnershipObservation:
        with self._locked() as state:
            observation = self._advance_ownership(state, source, owner)
            self._write_state(state)
            return observation

    def coordination(self, key: str) -> CoordinationObservation:
        with self._locked() as state:
            return _coordination_observation(state["coordination"].get(key))

    def make_next_publication_ambiguous(self) -> None:
        self._ambiguous_next = True

    def make_next_publication_unknown(self) -> None:
        self._unknown_next = True

    def make_next_publication_crash_after_ref(self) -> None:
        """Conformance hook for the durable preparing/ref recovery window."""

        self._crash_after_ref_next = True

    def make_next_publication_crash_after_authority(self) -> None:
        """Fault-inject after authority promotion but before journal commit."""

        self._crash_after_authority_next = True

    def make_candidate_unavailable(self, candidate: SealedCandidate) -> None:
        candidate._validate()
        self._set_ref_if_equals(_candidate_ref(candidate.handle), _revision(candidate.snapshot_id), None)

    def execute(self, intent: PublicationIntent) -> PublicationOutcome:
        intent._validate()
        with self._locked() as state:
            if any(
                attempt.get("status") == "preparing"
                and attempt.get("channel") == intent.channel_id.value
                and attempt_id != intent.attempt_id.value
                for attempt_id, attempt in state["attempts"].items()
                if isinstance(attempt, dict)
            ):
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            stored = state["attempts"].get(intent.attempt_id.value)
            if stored is not None:
                return self._outcome_for_existing(state, intent, stored, retry=True)
            self._validate_transaction(state, intent)
            state["attempts"][intent.attempt_id.value] = {
                "candidate_snapshot": intent.candidate.snapshot_id.value,
                "channel": intent.channel_id.value,
                "expected_head": _head_record(intent.expected_head),
                "intent": _intent_wire(intent),
                "marker": self._write_marker_commit(
                    intent.attempt_id.value, intent.candidate.snapshot_id, intent.expected_head, _intent_wire(intent)
                ),
                "status": "preparing",
            }
            self._write_state(state)
            if self._unknown_next:
                self._unknown_next = False
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            marker = state["attempts"][intent.attempt_id.value]["marker"]
            published = (
                self._atomic_adopt_review_refs(intent, marker)
                if intent.mode is PublicationMode.REVIEW_ADOPTION
                else self._atomic_publish_refs(
                    intent.channel_id, intent.expected_head.snapshot_id, intent.candidate.snapshot_id, marker
                )
            )
            if not published:
                state["attempts"][intent.attempt_id.value]["status"] = "not-committed"
                self._write_state(state)
                return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
            if self._crash_after_ref_next:
                self._crash_after_ref_next = False
                raise GitPublicationExecutionUnknownError("simulated interruption after durable ref publication")
            proof = self._commit_prepared(state, intent)
            if self._ambiguous_next:
                self._ambiguous_next = False
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            return PublicationOutcome(PublicationOutcomeState.COMMITTED, proof)

    def verify(self, intent: PublicationIntent) -> PublicationOutcome:
        intent._validate()
        with self._locked() as state:
            stored = state["attempts"].get(intent.attempt_id.value)
            if stored is None:
                return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
            return self._outcome_for_existing(state, intent, stored, retry=False)

    def _outcome_for_existing(
        self, state: dict[str, Any], intent: PublicationIntent, stored: dict[str, Any], *, retry: bool
    ) -> PublicationOutcome:
        if stored.get("intent") != _intent_wire(intent):
            raise ValueError("publication attempt ID is already bound to a different intent")
        status = stored.get("status")
        if status == "committed":
            return PublicationOutcome(
                PublicationOutcomeState.COMMITTED, self._proof_from_record(intent, stored["proof"])
            )
        if status == "not-committed":
            return PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
        if status != "preparing":
            raise GitPublicationError("publication attempt record is invalid")
        if self._preparing_marker_matches(stored, intent):
            try:
                self._validate_transaction(state, intent, pending_attempt=intent.attempt_id.value)
            except (GitPublicationError, ValueError):
                return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
            return PublicationOutcome(PublicationOutcomeState.COMMITTED, self._commit_prepared(state, intent))
        # A persisted prepare without its exact marker cannot be retried as if
        # it were a new CAS: raw candidate equality is not transaction proof.
        return PublicationOutcome(PublicationOutcomeState.UNKNOWN)

    def _validate_transaction(
        self, state: dict[str, Any], intent: PublicationIntent, *, pending_attempt: str | None = None
    ) -> None:
        candidate = state["candidates"].get(intent.candidate.handle.value)
        if intent.candidate.candidate_store_id.value != state["candidate_store_id"]:
            raise ValueError("publication candidate was issued by another candidate store")
        if candidate != {"content": intent.candidate.content_id.value, "snapshot": intent.candidate.snapshot_id.value}:
            raise ValueError("publication candidate was not sealed by this store")
        if self._ref_revision(_candidate_ref(intent.candidate.handle)) != _revision(intent.candidate.snapshot_id):
            raise ValueError("publication candidate is unavailable")
        self._validate_candidate_content(intent.candidate)
        if intent.mode is PublicationMode.REVIEW_ADOPTION:
            self._validate_review_adoption(state, intent, pending_attempt=pending_attempt)
        elif self._current_head(state, intent.channel_id, pending_attempt=pending_attempt) != intent.expected_head:
            raise ValueError("publication expected head is stale")
        if (
            intent.review_base_head is not None
            and self._current_head(state, intent.review_base_head.channel_id, pending_attempt=pending_attempt)
            != intent.review_base_head
        ):
            raise ValueError("review publication accepted desired base head is stale")
        for change in intent.source_ownership_changes:
            change._validate()
            self.source_repository.recover(change.retained_source)
            if (
                _ownership_observation(state["ownership"].get(change.retained_source.source_snapshot_id.to_wire()))
                != change.expected_ownership
            ):
                raise ValueError("publication source ownership is stale")
        for change in intent.coordination_changes:
            if _coordination_observation(state["coordination"].get(change.key)) != change.expected:
                raise ValueError("publication coordination fence is stale")

    def _validate_review_adoption(
        self, state: dict[str, Any], intent: PublicationIntent, *, pending_attempt: str | None
    ) -> None:
        acceptance = intent.review_acceptance
        if acceptance is None:
            raise ValueError("review adoption lacks external acceptance evidence")
        acceptance._validate()
        if acceptance.publication_store_id != self._store_id:
            raise ValueError("review acceptance was issued by another publication store")
        review_record = state["attempts"].get(acceptance.review_publication.attempt_id.value)
        if not isinstance(review_record, dict) or review_record.get("status") != "committed":
            raise ValueError("review acceptance does not identify a committed publication")
        review_intent = self._intent_from_wire(json.loads(review_record["intent"]), historical=True, state=state)
        review_proof = self._proof_from_record(review_intent, review_record["proof"])
        if (
            review_proof.proof_id != acceptance.review_proof_id
            or review_intent.mode is not PublicationMode.REVIEW_REQUIRED
            or review_intent.target is not PublicationTarget.REVIEW_CANDIDATE
            or review_intent.review_base_head != intent.expected_head
            or review_intent.candidate.snapshot_id != intent.candidate.snapshot_id
            or review_intent.candidate.content_id != intent.candidate.content_id
            or review_intent.environment_id != intent.environment_id
            or acceptance.environment_id != intent.environment_id
            or review_proof.resulting_head.snapshot_id != intent.candidate.snapshot_id
        ):
            raise ValueError("review acceptance does not match its authenticated review proof")
        if self._current_head(state, review_intent.channel_id) != review_proof.resulting_head:
            raise ValueError("review candidate head is stale")
        self._validate_review_adoption_refs(
            state,
            intent.expected_head,
            intent.candidate.snapshot_id,
            pending_attempt=pending_attempt,
        )

    def _validate_review_adoption_refs(
        self,
        state: dict[str, Any],
        accepted_base: HeadObservation,
        candidate: SnapshotId,
        *,
        pending_attempt: str | None,
    ) -> None:
        """Validate accepted authority plus the external review decision.

        The public ref is deliberately allowed to be ahead of authenticated
        authority only in this specialized path.  ``_atomic_adopt_review_refs``
        repeats the public comparison in the same Git ref transaction that
        advances the private mirror/attempt marker, closing the read/CAS gap.
        """

        record = state["heads"].get(accepted_base.channel_id.value)
        authority_marker = self._ref_revision(_authority_ref(accepted_base.channel_id))
        pending_marker = (
            state["attempts"].get(pending_attempt, {}).get("marker") if pending_attempt is not None else None
        )
        if record is None:
            if accepted_base.snapshot_id is not None or accepted_base.incarnation != "git:0":
                raise ValueError("review adoption accepted base is stale")
            if authority_marker is None:
                pass
            elif authority_marker == pending_marker and isinstance(pending_marker, str):
                self._validate_marker_object(pending_marker, candidate, pending_attempt)
            else:
                raise GitPublicationError("managed accepted authority exists without journal state")
        else:
            stored = HeadObservation(
                accepted_base.channel_id,
                SnapshotId(record["snapshot"]) if record["snapshot"] is not None else None,
                f"git:{record['version']}",
            )
            if stored != accepted_base:
                raise ValueError("review adoption accepted base is stale")
            marker = record.get("marker")
            if (
                not isinstance(marker, str)
                or authority_marker not in {marker, pending_marker}
                or (authority_marker == pending_marker and not isinstance(pending_marker, str))
            ):
                raise GitPublicationError("managed accepted authority marker is missing or drifted")
            if authority_marker == marker:
                self._validate_marker_object(marker, accepted_base.snapshot_id, record.get("attempt"))
            else:
                self._validate_marker_object(cast(str, pending_marker), candidate, pending_attempt)
        expected_private = candidate if pending_attempt is not None else accepted_base.snapshot_id
        if self._ref_revision(_channel_ref(accepted_base.channel_id)) != _revision_or_none(expected_private):
            raise GitPublicationError("private accepted channel does not match review adoption state")
        if self._ref_revision(_public_channel_ref(accepted_base.channel_id)) != _revision(candidate):
            raise ValueError("external accepted channel does not equal the reviewed candidate")
        self._validate_commit_tree(_revision(candidate))

    def _validate_candidate_content(self, candidate: SealedCandidate) -> None:
        if self._ref_revision(_candidate_ref(candidate.handle)) != _revision(candidate.snapshot_id):
            raise ValueError("publication candidate is unavailable")
        try:
            reader = GitSnapshotReader.from_path(self.repository)
            try:
                candidate_view = reader.open_snapshot(candidate.snapshot_id)
            finally:
                reader.close()
        except Exception as exc:
            raise GitPublicationError("publication candidate object is missing or unreadable") from exc
        if candidate_view.content_id != candidate.content_id:
            raise GitPublicationError("publication candidate content does not match its sealed content identity")

    def _intent_from_wire(
        self, wire: object, *, historical: bool = False, state: dict[str, Any] | None = None
    ) -> PublicationIntent:
        if not isinstance(wire, dict) or wire.get("effect_authorization") is not None:
            raise GitPublicationError("publication intent evidence cannot be reissued safely")
        try:
            candidate_wire = wire["candidate"]
            if not isinstance(candidate_wire, dict):
                raise ValueError
            candidate_locator = CandidateLocator(
                SealedCandidateHandle(candidate_wire["handle"]),
                CandidateStoreId(candidate_wire["store"]),
                SnapshotId(candidate_wire["snapshot"]),
                ContentId(candidate_wire["content"]),
            )
            if state is None:
                candidate = self.reissue_candidate(candidate_locator)
            else:
                if candidate_locator.candidate_store_id.value != state["candidate_store_id"] or state["candidates"].get(
                    candidate_locator.handle.value
                ) != {
                    "content": candidate_locator.content_id.value,
                    "snapshot": candidate_locator.snapshot_id.value,
                }:
                    raise ValueError
                candidate = _issue_sealed_candidate(
                    candidate_locator.handle,
                    candidate_locator.candidate_store_id,
                    candidate_locator.snapshot_id,
                    candidate_locator.content_id,
                )
                self._validate_candidate_content(candidate)
            expected = wire["expected_head"]
            if not isinstance(expected, dict):
                raise ValueError
            expected_head = HeadObservation(
                ChannelId(expected["channel"]),
                SnapshotId(expected["snapshot"]) if expected["snapshot"] is not None else None,
                expected["incarnation"],
            )
            changes: list[SourceOwnershipChange] = []
            ownership = wire["ownership"]
            if not isinstance(ownership, list):
                raise ValueError
            for change in ownership:
                if not isinstance(change, dict) or not isinstance(change.get("retained"), dict):
                    raise ValueError
                retained = change["retained"]
                locator = RetainedSourceLocator(
                    RetainedSourceHandle(retained["handle"]),
                    RetentionStoreId(retained["store"]),
                    SourceSnapshotId(SourceId(retained["source"]), SnapshotId(retained["snapshot"])),
                    ContentId(retained["content"]),
                )
                if historical:
                    source = _issue_historical_retained_source_evidence(
                        locator.handle, locator.retention_store_id, locator.source_snapshot_id, locator.content_id
                    )
                else:
                    source = self.source_repository.reissue(locator)
                expected_owner = change["expected"]
                if not isinstance(expected_owner, dict):
                    raise ValueError
                changes.append(
                    SourceOwnershipChange(
                        source,
                        OwnershipObservation(
                            OwnershipId(expected_owner["owner"]) if expected_owner["owner"] is not None else None,
                            expected_owner["incarnation"],
                        ),
                        OwnershipId(change["next"]) if change["next"] is not None else None,
                    )
                )
            coordination: list[CoordinationChange] = []
            if not isinstance(wire["coordination"], list):
                raise ValueError
            for change in wire["coordination"]:
                if not isinstance(change, dict) or not isinstance(change.get("expected"), dict):
                    raise ValueError
                observed = change["expected"]
                coordination.append(
                    CoordinationChange(
                        change["key"],
                        CoordinationObservation(observed["value"], observed["incarnation"]),
                        change["next"],
                    )
                )
            review_acceptance_wire = wire.get("review_acceptance")
            review_acceptance = None
            if isinstance(review_acceptance_wire, dict):
                accepted_base_wire = review_acceptance_wire["accepted_base"]
                if not isinstance(accepted_base_wire, dict):
                    raise ValueError
                accepted_base = HeadObservation(
                    ChannelId(accepted_base_wire["channel"]),
                    SnapshotId(accepted_base_wire["snapshot"]) if accepted_base_wire["snapshot"] is not None else None,
                    accepted_base_wire["incarnation"],
                )
                store_id = PublicationStoreId(review_acceptance_wire["store"])
                review_acceptance = _issue_review_acceptance_observation(
                    store_id,
                    PublicationRecoveryLocator(
                        store_id, PublicationAttemptId(review_acceptance_wire["review_attempt"])
                    ),
                    PublicationProofId(review_acceptance_wire["proof"]),
                    ChannelId(review_acceptance_wire["desired_channel"]),
                    accepted_base,
                    SnapshotId(review_acceptance_wire["candidate_snapshot"]),
                    ContentId(review_acceptance_wire["candidate_content"]),
                    EnvironmentId(review_acceptance_wire["environment"]),
                    review_acceptance_wire["incarnation"],
                    self._issuer,
                )
            elif review_acceptance_wire is not None:
                raise ValueError
            return PublicationIntent(
                PublicationAttemptId(wire["attempt"]),
                ChannelId(wire["channel"]),
                expected_head,
                candidate,
                tuple(changes),
                OwnershipId(wire["owner"]),
                tuple(coordination),
                PublicationTarget(wire["target"]),
                PublicationMode(wire["mode"]),
                review_base_head=(
                    HeadObservation(
                        ChannelId(wire["review_base_head"]["channel"]),
                        SnapshotId(wire["review_base_head"]["snapshot"])
                        if wire["review_base_head"]["snapshot"] is not None
                        else None,
                        wire["review_base_head"]["incarnation"],
                    )
                    if isinstance(wire.get("review_base_head"), dict)
                    else None
                ),
                review_acceptance=review_acceptance,
                environment_id=(EnvironmentId(wire["environment"]) if wire.get("environment") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitPublicationError("authenticated publication intent record is malformed") from exc

    def _commit_prepared(self, state: dict[str, Any], intent: PublicationIntent) -> PublicationProof:
        record = state["attempts"][intent.attempt_id.value]
        marker = record.get("marker")
        if not isinstance(marker, str) or not self._preparing_marker_matches(record, intent):
            raise GitPublicationError("prepared publication marker is absent or does not match its intent")
        previous = state["heads"].get(intent.channel_id.value)
        previous_marker = previous.get("marker") if isinstance(previous, dict) else None
        if not self._promote_authority_marker(intent.channel_id, marker, previous_marker):
            raise GitPublicationError("managed authority marker changed during publication")
        if self._crash_after_authority_next:
            self._crash_after_authority_next = False
            raise GitPublicationExecutionUnknownError("simulated interruption after authority marker promotion")
        head = self._advance_head(state, intent.channel_id, intent.candidate.snapshot_id, marker)
        ownership = tuple(
            SourceOwnershipResult(
                change.retained_source.source_snapshot_id,
                change.next_owner,
                self._advance_ownership(state, change.retained_source.source_snapshot_id, change.next_owner),
            )
            for change in intent.source_ownership_changes
        )
        coordination = tuple(
            CoordinationResult(change.key, change.next_value, self._advance_coordination(state, change))
            for change in intent.coordination_changes
        )
        proof_id = PublicationProofId(f"git-publication-proof:{secrets.token_urlsafe(32)}")
        proof = _issue_publication_proof(
            PublicationStoreId(state["publication_store_id"]),
            self._issuer,
            intent,
            head,
            ownership,
            coordination,
            proof_id,
        )
        state["attempts"][intent.attempt_id.value] = {
            "candidate_snapshot": intent.candidate.snapshot_id.value,
            "channel": intent.channel_id.value,
            "expected_head": _head_record(intent.expected_head),
            "intent": _intent_wire(intent),
            "marker": marker,
            "proof": _proof_record(head, ownership, coordination, proof),
            "status": "committed",
        }
        self._write_state(state)
        return proof

    def _proof_from_record(self, intent: PublicationIntent, record: dict[str, Any]) -> PublicationProof:
        head = _head_observation(record["head"])
        ownership = tuple(_ownership_result(value) for value in record["ownership"])
        coordination = tuple(_coordination_result(value) for value in record["coordination"])
        return _issue_publication_proof(
            self._store_id, self._issuer, intent, head, ownership, coordination, PublicationProofId(record["id"])
        )

    def _promote_authority_marker(self, channel: ChannelId, marker: str, previous: object) -> bool:
        current = self._ref_revision(_authority_ref(channel))
        if current == marker:
            self._validate_marker_object(
                marker, SnapshotId(cast(str, self._marker_data(marker)["candidate"])), self._marker_attempt(marker)
            )
            return True
        old = previous if isinstance(previous, str) else "0" * 40
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "update-ref", _authority_ref(channel), marker, old],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if "cannot lock ref" in completed.stderr:
            return False
        raise GitPublicationError("managed authority marker cannot be promoted")

    def _current_head(
        self, state: dict[str, Any], channel_id: ChannelId, *, pending_attempt: str | None = None
    ) -> HeadObservation:
        """Read only authenticated committed authority, never a raw mirror."""

        record = state["heads"].get(channel_id.value)
        raw = self._ref_revision(_channel_ref(channel_id))
        public = self._ref_revision(_public_channel_ref(channel_id))
        # The MACed preparing journal alone preserves the pre-transaction
        # authority. Marker/mirror integrity gates recovery and adoption, not
        # reads of the last committed head.
        for attempt in state["attempts"].values():
            if (
                isinstance(attempt, dict)
                and attempt.get("status") == "preparing"
                and attempt.get("channel") == channel_id.value
            ):
                return _head_observation(attempt["expected_head"])
        if record is None:
            if raw is not None or public is not None or self._ref_revision(_authority_ref(channel_id)) is not None:
                raise GitPublicationError("unmanaged publication ref exists without committed authority")
            return HeadObservation.absent(channel_id, "git:0")
        snapshot = SnapshotId(record["snapshot"]) if record["snapshot"] is not None else None
        marker = record["marker"]
        if marker is None:
            if (
                snapshot is not None
                or raw is not None
                or public is not None
                or self._ref_revision(_authority_ref(channel_id)) is not None
            ):
                raise GitPublicationError("committed authority record is malformed")
        else:
            if self._ref_revision(_authority_ref(channel_id)) != marker:
                raise GitPublicationError("managed authority marker is missing or drifted")
            self._validate_marker_object(marker, snapshot, record.get("attempt"))
        if raw != _revision_or_none(snapshot) or public != _revision_or_none(snapshot):
            raise GitPublicationError("public or private channel mirror drift requires repair")
        if snapshot is not None:
            self._validate_commit_tree(_revision(snapshot))
        return HeadObservation(channel_id, snapshot, f"git:{record['version']}")

    def _advance_head(
        self, state: dict[str, Any], channel_id: ChannelId, snapshot: SnapshotId | None, marker: str | None
    ) -> HeadObservation:
        current = state["heads"].get(channel_id.value)
        version = 1 if current is None else current["version"] + 1
        state["heads"][channel_id.value] = {
            "snapshot": snapshot.value if snapshot is not None else None,
            "marker": marker,
            "attempt": None if marker is None else self._marker_attempt(marker),
            "version": version,
        }
        return HeadObservation(channel_id, snapshot, f"git:{version}")

    def _advance_ownership(
        self, state: dict[str, Any], source: SourceSnapshotId, owner: OwnershipId | None
    ) -> OwnershipObservation:
        key = source.to_wire()
        prior = state["ownership"].get(key)
        version = 1 if prior is None else prior["version"] + 1
        state["ownership"][key] = {"owner": owner.value if owner is not None else None, "version": version}
        return OwnershipObservation(owner, f"git:{version}")

    def _advance_coordination(self, state: dict[str, Any], change: CoordinationChange) -> CoordinationObservation:
        prior = state["coordination"].get(change.key)
        version = 1 if prior is None else prior["version"] + 1
        state["coordination"][change.key] = {"value": change.next_value, "version": version}
        return CoordinationObservation(change.next_value, f"git:{version}")

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        root = self._state_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        with self._thread_lock, os.fdopen(os.open(root / _LOCK_FILENAME, flags, 0o600), "a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._load_state()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _state_root(self) -> Path:
        repository = Repo(self.repository)
        try:
            control = Path(repository.controldir())
        finally:
            repository.close()
        root = control / _STATE_DIRECTORY
        root.mkdir(mode=0o700, exist_ok=True)
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise GitPublicationError("publication metadata is not a private directory")
        os.chmod(root, 0o700)
        return root

    def _load_state(self) -> dict[str, Any]:
        path = self._state_root() / _STATE_FILENAME
        if not path.exists():
            if self._key_path().exists() or self._managed_namespace_exists():
                raise GitPublicationError("publication journal/key is missing while managed Git authority exists")
            store_id = PublicationStoreId(f"git-publication-store:{secrets.token_urlsafe(24)}")
            self._key_for_store(store_id, provision=True)
            return {
                "attempts": {},
                "candidate_store_id": f"git-candidate-store:{secrets.token_urlsafe(24)}",
                "candidates": {},
                "coordination": {},
                "heads": {},
                "ownership": {},
                "publication_store_id": store_id.value,
                "version": _STATE_VERSION,
            }
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
            state = json.loads(path.read_text())
            if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
                raise ValueError
            for key in ("attempts", "candidates", "coordination", "heads", "ownership"):
                if not isinstance(state.get(key), dict):
                    raise ValueError
            CandidateStoreId(state["candidate_store_id"])
            store_id = PublicationStoreId(state["publication_store_id"])
            secret = self._key_for_store(store_id, provision=False)
            mac = state.pop("mac", None)
            if not isinstance(mac, str) or not hmac.compare_digest(mac, _state_mac(secret, state)):
                raise ValueError
            state["mac"] = mac
            _validate_state_shape(state)
            return state
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise GitPublicationError("publication metadata is corrupted or unreadable") from exc

    def _write_state(self, state: dict[str, Any]) -> None:
        root = self._state_root()
        temporary_path: Path | None = None
        try:
            unsigned = {key: value for key, value in state.items() if key != "mac"}
            state = {**unsigned, "mac": _state_mac(self._state_secret, unsigned)}
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, delete=False) as temporary:
                os.fchmod(temporary.fileno(), 0o600)
                json.dump(state, temporary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, root / _STATE_FILENAME)
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise GitPublicationError("publication metadata cannot be written") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _key_path(self) -> Path:
        return self.repository.parent / f".{self.repository.name}{_KEY_SUFFIX}"

    def _managed_namespace_exists(self) -> bool:
        repository = Repo(self.repository)
        try:
            return any(ref.decode().startswith(f"{_REF_PREFIX}/") for ref in repository.refs.keys())
        finally:
            repository.close()

    def _key_for_store(self, store_id: PublicationStoreId, *, provision: bool) -> bytes:
        """Load or provision the private state-MAC/proof key outside Git metadata."""

        path = self._key_path()
        if not path.exists():
            if not provision:
                raise GitPublicationError("publication key anchor is missing")
            record = _key_record(store_id, secrets.token_bytes(32))
            payload = _wire(record).encode()
            descriptor: int | None = None
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.write(descriptor, payload)
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                pass
            except OSError as exc:
                raise GitPublicationError("publication key anchor cannot be created") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            # Durably link the newly-created independent anchor before a
            # state journal is allowed to refer to its store identity.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or set(record) != {"secret", "store"} or record["store"] != store_id.value:
                raise ValueError
            secret = base64.b64decode(record["secret"].encode(), validate=True)
            if len(secret) != 32:
                raise ValueError
            return secret
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise GitPublicationError("publication key anchor is corrupted or does not match this store") from exc

    def _ref_revision(self, ref: str) -> str | None:
        repository = Repo(self.repository)
        try:
            try:
                return repository.refs[Ref(ref.encode())].decode()
            except (KeyError, ValueError):
                return None
        finally:
            repository.close()

    def _set_ref_if_equals(self, ref: str, old: str | None, new: str | None) -> bool:
        repository = Repo(self.repository)
        try:
            old_value = cast(ObjectID, old.encode()) if old is not None else None
            new_value = cast(ObjectID, new.encode()) if new is not None else None
            if new_value is None:
                return repository.refs.remove_if_equals(Ref(ref.encode()), old_value)
            return repository.refs.set_if_equals(Ref(ref.encode()), old_value, new_value)
        finally:
            repository.close()

    def _atomic_publish_refs(
        self,
        channel: ChannelId,
        expected: SnapshotId | None,
        candidate: SnapshotId,
        marker: str,
        *,
        test_hook: bool = False,
        previous_marker: str | None = None,
    ) -> bool:
        """Atomically update the non-authoritative mirror and managed marker."""

        if not isinstance(marker, str) or not _is_object_id(marker):
            raise GitPublicationError("publication marker object ID is invalid")
        zero = "0" * 40
        marker_ref = _authority_ref(channel) if test_hook else _attempt_marker_ref(marker)
        # The authority ref is advanced only after adoption is journaled.  The
        # attempt marker is the durable proof that this exact CAS happened.
        lines = [
            "start",
            f"update {_channel_ref(channel)} {_revision(candidate)} {_revision_or_none(expected) or zero}",
            f"update {_public_channel_ref(channel)} {_revision(candidate)} {_revision_or_none(expected) or zero}",
            (
                f"update {marker_ref} {marker} {previous_marker or zero}"
                if test_hook
                else f"create {marker_ref} {marker}"
            ),
            "prepare",
            "commit",
            "",
        ]
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "update-ref", "--stdin"],
            input="\n".join(lines),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if "cannot lock ref" in completed.stderr or "reference already exists" in completed.stderr:
            return False
        raise GitPublicationError("atomic publication ref transaction failed")

    def _atomic_adopt_review_refs(self, intent: PublicationIntent, marker: str) -> bool:
        """Adopt an externally accepted review without rewriting its public ref."""

        candidate = _revision(intent.candidate.snapshot_id)
        expected = _revision_or_none(intent.expected_head.snapshot_id) or "0" * 40
        lines = [
            "start",
            f"verify {_public_channel_ref(intent.channel_id)} {candidate}",
            f"update {_channel_ref(intent.channel_id)} {candidate} {expected}",
            f"create {_attempt_marker_ref(marker)} {marker}",
            "prepare",
            "commit",
            "",
        ]
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "update-ref", "--stdin"],
            input="\n".join(lines),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if "cannot lock ref" in completed.stderr or "reference already exists" in completed.stderr:
            return False
        raise GitPublicationError("atomic review adoption ref transaction failed")

    def _atomic_bootstrap_refs(self, channel: ChannelId, public_revision: str, marker: str) -> bool:
        lines = [
            "start",
            f"verify {_public_channel_ref(channel)} {public_revision}",
            f"create {_channel_ref(channel)} {public_revision}",
            f"create {_authority_ref(channel)} {marker}",
            "prepare",
            "commit",
            "",
        ]
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "update-ref", "--stdin"],
            input="\n".join(lines),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if "cannot lock ref" in completed.stderr or "reference already exists" in completed.stderr:
            return False
        raise GitPublicationError("atomic authority bootstrap failed")

    def _atomic_clear_refs(self, channel: ChannelId, expected: SnapshotId | None, record: object) -> bool:
        marker = record.get("marker") if isinstance(record, dict) else None
        zero = "0" * 40
        lines = [
            "start",
            f"delete {_channel_ref(channel)} {_revision_or_none(expected) or zero}",
            f"delete {_public_channel_ref(channel)} {_revision_or_none(expected) or zero}",
        ]
        if isinstance(marker, str):
            lines.append(f"delete {_authority_ref(channel)} {marker}")
        lines.extend(("prepare", "commit", ""))
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "update-ref", "--stdin"],
            input="\n".join(lines),
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0

    def _write_marker_commit(
        self, attempt: str, candidate: SnapshotId, expected: HeadObservation, intent_wire: str
    ) -> str:
        """Persist an authenticated, attempt-bound marker object before CAS."""

        payload = {
            "attempt": attempt,
            "candidate": candidate.value,
            "expected": _head_record(expected),
            "intent": hashlib.sha256(intent_wire.encode()).hexdigest(),
        }
        evidence = {**payload, "mac": _state_mac(self._state_secret, payload)}
        repository = Repo(self.repository)
        try:
            candidate_commit = repository[cast(ObjectID, _revision(candidate).encode())]
            if not isinstance(candidate_commit, Commit):
                raise GitPublicationError("publication candidate is not a commit")
            self._validate_commit_tree(_revision(candidate))
            marker = Commit()
            marker.tree = candidate_commit.tree
            marker.parents = [candidate_commit.id]
            identity = b"gitopsctr publication <gitopsctr@invalid>"
            marker.author = identity
            marker.committer = identity
            marker.message = _wire(evidence).encode()
            moment = int(time.time())
            marker.author_time = moment
            marker.commit_time = moment
            marker.author_timezone = 0
            marker.commit_timezone = 0
            repository.object_store.add_object(marker)
            return marker.id.decode()
        finally:
            repository.close()

    def _marker_data(self, marker: str) -> dict[str, object]:
        if not _is_object_id(marker):
            raise GitPublicationError("managed publication marker ID is invalid")
        repository = Repo(self.repository)
        try:
            object_value = repository[cast(ObjectID, marker.encode())]
            if not isinstance(object_value, Commit):
                raise GitPublicationError("managed publication marker is not a commit")
            if not _is_object_id(object_value.tree.decode()):
                raise GitPublicationError("managed publication marker tree is invalid")
            repository[object_value.tree]
            value = json.loads(object_value.message.decode())
            if not isinstance(value, dict) or set(value) != {"attempt", "candidate", "expected", "intent", "mac"}:
                raise ValueError
            unsigned = {key: value[key] for key in ("attempt", "candidate", "expected", "intent")}
            if not isinstance(value["mac"], str) or not hmac.compare_digest(
                value["mac"], _state_mac(self._state_secret, unsigned)
            ):
                raise ValueError
            if (
                not isinstance(value["attempt"], str)
                or not isinstance(value["candidate"], str)
                or not isinstance(value["intent"], str)
            ):
                raise ValueError
            _revision(SnapshotId(value["candidate"]))
            _head_observation(value["expected"])
            return value
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise GitPublicationError("managed publication marker is malformed or unauthenticated") from exc
        finally:
            repository.close()

    def _marker_attempt(self, marker: str) -> str:
        return cast(str, self._marker_data(marker)["attempt"])

    def _validate_marker_object(self, marker: str, snapshot: SnapshotId | None, attempt: object) -> None:
        data = self._marker_data(marker)
        if snapshot is None or data["candidate"] != snapshot.value or data["attempt"] != attempt:
            raise GitPublicationError("managed authority marker does not match committed head")
        self._validate_commit_tree(_revision(snapshot))

    def _preparing_marker_matches(self, record: dict[str, Any], intent: PublicationIntent | None) -> bool:
        marker = record.get("marker")
        if not isinstance(marker, str) or self._ref_revision(_attempt_marker_ref(marker)) != marker:
            return False
        try:
            data = self._marker_data(marker)
        except GitPublicationError:
            return False
        if data["attempt"] != (
            intent.attempt_id.value if intent is not None else record.get("attempt", data["attempt"])
        ):
            return False
        if data["candidate"] != record.get("candidate_snapshot"):
            return False
        if data["expected"] != record.get("expected_head"):
            return False
        if intent is not None and data["intent"] != hashlib.sha256(_intent_wire(intent).encode()).hexdigest():
            return False
        channel = ChannelId(record["channel"])
        candidate = _revision(SnapshotId(record["candidate_snapshot"]))
        private = self._ref_revision(_channel_ref(channel))
        public = self._ref_revision(_public_channel_ref(channel))
        return private == candidate and public == candidate

    def _validate_commit_tree(self, revision: str) -> None:
        if not _is_object_id(revision):
            raise GitPublicationError("managed publication object ID is invalid")
        repository = Repo(self.repository)
        try:
            commit = repository[cast(ObjectID, revision.encode())]
            if not isinstance(commit, Commit):
                raise GitPublicationError("managed publication ref does not point to a commit")
            repository[commit.tree]
        except KeyError as exc:
            raise GitPublicationError("managed publication commit or tree is missing") from exc
        finally:
            repository.close()
        try:
            reader = GitSnapshotReader.from_path(self.repository)
            try:
                reader.open_snapshot(SnapshotId(f"{_SNAPSHOT_PREFIX}{revision}"))
            finally:
                reader.close()
        except Exception as exc:
            raise GitPublicationError("managed publication snapshot tree is missing or invalid") from exc

    def _write_candidate_commit(
        self, workspace: ImmutableWorkspace, handle: SealedCandidateHandle, parent_snapshot_id: SnapshotId | None
    ) -> str:
        repository = Repo(self.repository)
        try:
            entries: list[tuple[bytes, ObjectID, int]] = []
            for entry in workspace.list_entries():
                if entry.kind is WorkspaceEntryKind.DIRECTORY:
                    continue
                if entry.kind is not WorkspaceEntryKind.FILE or entry.content is None:
                    raise GitPublicationError("Git candidates support only regular logical workspace files")
                blob = Blob.from_string(entry.content)
                repository.object_store.add_object(blob)
                entries.append((entry.key.encode(), blob.id, stat.S_IFREG | (0o755 if entry.executable else 0o644)))
            tree = repository.object_store.add_object  # preserve the empty-tree path without leaking Dulwich upstream
            if entries:
                tree_id = commit_tree(repository.object_store, entries)
            else:
                from dulwich.objects import Tree

                empty = Tree()
                tree(empty)
                tree_id = empty.id
            commit = Commit()
            commit.tree = tree_id
            commit.parents = (
                [] if parent_snapshot_id is None else [cast(ObjectID, _revision(parent_snapshot_id).encode())]
            )
            identity = b"gitopsctr publication <gitopsctr@invalid>"
            commit.author = identity
            commit.committer = identity
            commit.message = f"Seal publication candidate {handle.value}".encode()
            moment = int(time.time())
            commit.author_time = moment
            commit.commit_time = moment
            commit.author_timezone = 0
            commit.commit_timezone = 0
            repository.object_store.add_object(commit)
            return commit.id.decode()
        finally:
            repository.close()


def _key_record(store_id: PublicationStoreId, secret: bytes) -> dict[str, str]:
    return {"secret": base64.b64encode(secret).decode(), "store": store_id.value}


def _state_mac(secret: bytes, state: dict[str, Any]) -> str:
    return hmac.new(secret, _wire(state).encode(), hashlib.sha256).hexdigest()


def _validate_state_shape(state: dict[str, Any]) -> None:
    """Reject malformed durable data before it can reach ref/object operations."""

    required = {
        "attempts",
        "candidate_store_id",
        "candidates",
        "coordination",
        "heads",
        "ownership",
        "publication_store_id",
        "version",
        "mac",
    }
    if set(state) != required:
        raise ValueError("publication state has an unsupported shape")
    for handle, candidate in state["candidates"].items():
        SealedCandidateHandle(handle)
        if not isinstance(candidate, dict) or set(candidate) != {"content", "snapshot"}:
            raise ValueError("publication candidate record is invalid")
        ContentId(candidate["content"])
        _revision(SnapshotId(candidate["snapshot"]))
    for channel, head in state["heads"].items():
        ChannelId(channel)
        if not isinstance(head, dict) or set(head) != {"attempt", "marker", "snapshot", "version"}:
            raise ValueError("publication head record is invalid")
        if head["snapshot"] is not None:
            _revision(SnapshotId(head["snapshot"]))
        if head["marker"] is not None and not _is_object_id(head["marker"]):
            raise ValueError("publication head marker is invalid")
        if head["attempt"] is not None and not isinstance(head["attempt"], str):
            raise ValueError("publication head attempt is invalid")
        if not isinstance(head["version"], int) or head["version"] < 0:
            raise ValueError("publication head record is invalid")
    for source, observation in state["ownership"].items():
        source_data = json.loads(source)
        if not isinstance(source_data, dict) or set(source_data) != {"snapshot", "source"}:
            raise ValueError("publication ownership source key is invalid")
        SourceSnapshotId(SourceId(source_data["source"]), SnapshotId(source_data["snapshot"]))
        if not isinstance(observation, dict) or set(observation) != {"owner", "version"}:
            raise ValueError("publication ownership record is invalid")
        if observation["owner"] is not None:
            OwnershipId(observation["owner"])
        if not isinstance(observation["version"], int) or observation["version"] < 0:
            raise ValueError("publication ownership record is invalid")
    for key, observation in state["coordination"].items():
        if not isinstance(key, str) or not isinstance(observation, dict) or set(observation) != {"value", "version"}:
            raise ValueError("publication coordination record is invalid")
        CoordinationObservation(observation["value"], f"git:{observation['version']}")
    for attempt, record in state["attempts"].items():
        PublicationAttemptId(attempt)
        if not isinstance(record, dict) or not isinstance(record.get("intent"), str):
            raise ValueError("publication attempt record is invalid")
        status = record.get("status")
        if status not in {"preparing", "committed", "not-committed"}:
            raise ValueError("publication attempt record is invalid")
        required_attempt = {"candidate_snapshot", "channel", "expected_head", "intent", "marker", "status"}
        if status == "committed":
            required_attempt.add("proof")
        if set(record) != required_attempt:
            raise ValueError("publication attempt record has an unsupported shape")
        _revision(SnapshotId(record["candidate_snapshot"]))
        ChannelId(record["channel"])
        if not _is_object_id(record["marker"]):
            raise ValueError("publication attempt marker is invalid")
        _head_observation(record["expected_head"])
        try:
            intent = json.loads(record["intent"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("publication attempt intent evidence is invalid") from exc
        if not isinstance(intent, dict):
            raise ValueError("publication attempt intent evidence is invalid")
        if status == "committed":
            proof = record["proof"]
            if not isinstance(proof, dict) or set(proof) != {"coordination", "head", "id", "ownership"}:
                raise ValueError("publication attempt proof evidence is invalid")
            PublicationProofId(proof["id"])
            _head_observation(proof["head"])
            if not isinstance(proof["ownership"], list) or not isinstance(proof["coordination"], list):
                raise ValueError("publication attempt proof evidence is invalid")
            for value in proof["ownership"]:
                _ownership_result(value)
            for value in proof["coordination"]:
                _coordination_result(value)


def _wire(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _intent_wire(intent: PublicationIntent) -> str:
    return _wire(intent._wire_data())


def _candidate_ref(handle: SealedCandidateHandle) -> str:
    return f"{_REF_PREFIX}/candidates/{handle.value.removeprefix('git-candidate:')}"


def _channel_ref(channel: ChannelId) -> str:
    encoded = base64.urlsafe_b64encode(channel.value.encode()).decode().rstrip("=")
    return f"{_REF_PREFIX}/channels/{encoded}"


def _public_channel_ref(channel: ChannelId) -> str:
    value = channel.value.removeprefix("refs/heads/")
    ref = f"refs/heads/{value}"
    if channel.value.startswith("refs/") and not channel.value.startswith("refs/heads/"):
        raise GitPublicationError("Git publication channel must name a branch")
    if not DulwichLocalRepository.valid_ref(ref):
        raise GitPublicationError("Git publication channel is not a valid branch")
    return ref


def _authority_ref(channel: ChannelId) -> str:
    encoded = base64.urlsafe_b64encode(channel.value.encode()).decode().rstrip("=")
    return f"{_REF_PREFIX}/authority/{encoded}"


def _attempt_marker_ref(marker: str) -> str:
    if not _is_object_id(marker):
        raise ValueError("publication marker object ID is invalid")
    return f"{_REF_PREFIX}/attempts/{marker}"


def _is_object_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _revision(snapshot: SnapshotId) -> str:
    if not snapshot.value.startswith(_SNAPSHOT_PREFIX):
        raise ValueError("Git publication snapshot was not issued by this adapter")
    revision = snapshot.value.removeprefix(_SNAPSHOT_PREFIX)
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("Git publication snapshot is invalid")
    return revision


def _revision_or_none(snapshot: SnapshotId | None) -> str | None:
    return _revision(snapshot) if snapshot is not None else None


def _snapshot_value(revision: str | None) -> str | None:
    return f"{_SNAPSHOT_PREFIX}{revision}" if revision is not None else None


def _ownership_observation(value: object) -> OwnershipObservation:
    if value is None:
        return OwnershipObservation.absent("git:0")
    if not isinstance(value, dict) or not isinstance(value.get("version"), int):
        raise GitPublicationError("publication ownership metadata is invalid")
    owner = value.get("owner")
    return OwnershipObservation(OwnershipId(owner) if owner is not None else None, f"git:{value['version']}")


def _coordination_observation(value: object) -> CoordinationObservation:
    if value is None:
        return CoordinationObservation.absent("git:0")
    if not isinstance(value, dict) or not isinstance(value.get("version"), int):
        raise GitPublicationError("publication coordination metadata is invalid")
    return CoordinationObservation(value.get("value"), f"git:{value['version']}")


def _proof_record(
    head: HeadObservation,
    ownership: tuple[SourceOwnershipResult, ...],
    coordination: tuple[CoordinationResult, ...],
    proof: PublicationProof,
) -> dict[str, object]:
    return {
        "coordination": [
            {
                "key": value.key,
                "next": value.requested_next_value,
                "result": {
                    "incarnation": value.resulting_observation.incarnation,
                    "value": value.resulting_observation.value,
                },
            }
            for value in coordination
        ],
        "head": {
            "channel": head.channel_id.value,
            "incarnation": head.incarnation,
            "snapshot": head.snapshot_id.value if head.snapshot_id is not None else None,
        },
        "id": proof.proof_id.value,
        "ownership": [
            {
                "next": value.requested_next_owner.value if value.requested_next_owner is not None else None,
                "result": {
                    "incarnation": value.resulting_observation.incarnation,
                    "owner": value.resulting_observation.owner.value if value.resulting_observation.owner else None,
                },
                "snapshot": value.source_snapshot_id.snapshot_id.value,
                "source": value.source_snapshot_id.source_id.value,
            }
            for value in ownership
        ],
    }


def _head_record(head: HeadObservation) -> dict[str, str | None]:
    return {
        "channel": head.channel_id.value,
        "incarnation": head.incarnation,
        "snapshot": head.snapshot_id.value if head.snapshot_id is not None else None,
    }


def _head_observation(value: object) -> HeadObservation:
    if not isinstance(value, dict):
        raise GitPublicationError("publication proof record is invalid")
    return HeadObservation(
        ChannelId(value["channel"]),
        SnapshotId(value["snapshot"]) if value["snapshot"] is not None else None,
        value["incarnation"],
    )


def _ownership_result(value: object) -> SourceOwnershipResult:
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise GitPublicationError("publication ownership proof record is invalid")
    result = value["result"]
    return SourceOwnershipResult(
        SourceSnapshotId(SourceId(value["source"]), SnapshotId(value["snapshot"])),
        OwnershipId(value["next"]) if value["next"] is not None else None,
        OwnershipObservation(OwnershipId(result["owner"]) if result["owner"] else None, result["incarnation"]),
    )


def _coordination_result(value: object) -> CoordinationResult:
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise GitPublicationError("publication coordination proof record is invalid")
    result = value["result"]
    return CoordinationResult(
        value["key"], value["next"], CoordinationObservation(result["value"], result["incarnation"])
    )
