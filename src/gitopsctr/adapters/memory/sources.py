"""Deterministic in-memory source acquisition and retention conformance adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_urlsafe
from threading import RLock

from gitopsctr.application.model import (
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
    _issue_retained_source,
)
from gitopsctr.application.sources import (
    RetainedSourceLocator,
    SourceNotFoundError,
    SourceRepository,
    SourceRequest,
    SourceRetentionError,
    SourceSnapshot,
    copied_source_snapshot,
    same_source_payload,
)
from gitopsctr.application.workspace import ImmutableWorkspace


@dataclass(slots=True)
class MemorySourceRetentionStore:
    """Injectable durable retention state shared by memory adapter instances.

    The store has its own random identity and synchronized records, which
    makes an otherwise identical source identity from another test/store
    incapable of recovering this store's retained payload.
    """

    retention_store_id: RetentionStoreId = field(
        default_factory=lambda: RetentionStoreId(f"memory-retention-store:{token_urlsafe(24)}")
    )
    _records: dict[RetainedSourceHandle, tuple[RetainedSource, SourceSnapshot]] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.retention_store_id, RetentionStoreId):
            raise TypeError("retention_store_id must be a RetentionStoreId")


@dataclass(slots=True)
class MemorySourceRepository(SourceRepository):
    """An exact source adapter with explicit mutable selectors for conformance.

    Tests may move or remove selectors and source snapshots.  Retained payloads
    are copied at ``retain`` time, so they remain recoverable independently of
    those source-side changes.
    """

    source_id: SourceId
    retention_store: MemorySourceRetentionStore = field(default_factory=MemorySourceRetentionStore)
    _snapshots: dict[SourceSnapshotId, SourceSnapshot] = field(default_factory=dict, init=False, repr=False)
    _selectors: dict[str, SourceSnapshotId] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, SourceId):
            raise TypeError("source_id must be a SourceId")
        if not isinstance(self.retention_store, MemorySourceRetentionStore):
            raise TypeError("retention_store must be a MemorySourceRetentionStore")

    def close(self) -> None:
        """Satisfy the source-repository lifecycle; no external resources exist."""

    def install(self, snapshot_id: SnapshotId, workspace: ImmutableWorkspace) -> SourceSnapshot:
        """Install an immutable exact source snapshot, rejecting changed ID reuse."""

        if not isinstance(snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        if not isinstance(workspace, ImmutableWorkspace):
            raise TypeError("workspace must implement ImmutableWorkspace")
        source_snapshot_id = SourceSnapshotId(self.source_id, snapshot_id)
        source = SourceSnapshot(source_snapshot_id, workspace.content_id, workspace)
        immutable = copied_source_snapshot(source)
        with self._lock:
            existing = self._snapshots.get(source_snapshot_id)
            if existing is not None:
                if not same_source_payload(existing, immutable):
                    raise ValueError("source snapshot ID is already installed with different content")
                return copied_source_snapshot(existing)
            self._snapshots[source_snapshot_id] = immutable
            return copied_source_snapshot(immutable)

    def set_selector(self, selector: str, snapshot_id: SnapshotId) -> None:
        """Move a test selector to an installed exact source snapshot."""

        source_snapshot_id = SourceSnapshotId(self.source_id, snapshot_id)
        with self._lock:
            if source_snapshot_id not in self._snapshots:
                raise SourceNotFoundError("source selector cannot target an unavailable snapshot")
            self._selectors[_selector(selector)] = source_snapshot_id

    def remove_selector(self, selector: str) -> None:
        """Remove a source-side selector without affecting retained copies."""

        with self._lock:
            self._selectors.pop(_selector(selector), None)

    def remove_snapshot(self, snapshot_id: SnapshotId) -> None:
        """Remove original source availability without affecting retained copies."""

        source_snapshot_id = SourceSnapshotId(self.source_id, snapshot_id)
        with self._lock:
            self._snapshots.pop(source_snapshot_id, None)
            for selector, selected in tuple(self._selectors.items()):
                if selected == source_snapshot_id:
                    del self._selectors[selector]

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        """Resolve one current selector to its exact installed source snapshot."""

        if not isinstance(request, SourceRequest):
            raise TypeError("request must be a SourceRequest")
        if request.source_id != self.source_id:
            raise SourceNotFoundError("source request was not issued for this memory source")
        with self._lock:
            try:
                source_snapshot_id = self._selectors[request.selector]
                source = self._snapshots[source_snapshot_id]
            except KeyError as exc:
                raise SourceNotFoundError("memory source selector cannot be resolved") from exc
            return copied_source_snapshot(source)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        """Copy one adapter-issued source payload under a fresh opaque handle."""

        canonical = self._canonical_resolved(source)
        with self.retention_store._lock:
            handle = RetainedSourceHandle(f"memory-retained:{token_urlsafe(32)}")
            while handle in self.retention_store._records:
                handle = RetainedSourceHandle(f"memory-retained:{token_urlsafe(32)}")
            retained = _issue_retained_source(
                handle,
                self.retention_store.retention_store_id,
                canonical.source_snapshot_id,
                canonical.content_id,
            )
            self.retention_store._records[handle] = (retained, copied_source_snapshot(canonical))
            return retained

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        """Recover a retained source payload, or fail closed for invalid handles."""

        with self.retention_store._lock:
            return copied_source_snapshot(self._retained_source(retained))

    def release(self, retained: RetainedSource) -> None:
        """Release only an exact, live retention record."""

        with self.retention_store._lock:
            self._retained_source(retained)
            del self.retention_store._records[retained.handle]

    def reissue(self, locator: RetainedSourceLocator) -> RetainedSource:
        """Reissue a process-local retention capability from durable evidence."""

        with self.retention_store._lock:
            source = self._locator_source(locator)
            return _issue_retained_source(
                locator.handle,
                locator.retention_store_id,
                source.source_snapshot_id,
                source.content_id,
            )

    def _canonical_resolved(self, source: SourceSnapshot) -> SourceSnapshot:
        if not isinstance(source, SourceSnapshot):
            raise TypeError("source must be a SourceSnapshot")
        if source.source_snapshot_id.source_id != self.source_id:
            raise SourceRetentionError("source snapshot belongs to a different source repository")
        with self._lock:
            canonical = self._snapshots.get(source.source_snapshot_id)
        if canonical is None or not same_source_payload(canonical, source):
            raise SourceRetentionError("source snapshot was not issued by this memory source repository")
        return canonical

    def _retained_source(self, retained: RetainedSource) -> SourceSnapshot:
        if not isinstance(retained, RetainedSource):
            raise TypeError("retained must be a RetainedSource")
        try:
            retained._validate()
        except TypeError as exc:
            raise SourceRetentionError("retained source is not an issued retention value") from exc
        return self._locator_source(RetainedSourceLocator.from_retained(retained), expected=retained)

    def _locator_source(
        self, locator: RetainedSourceLocator, *, expected: RetainedSource | None = None
    ) -> SourceSnapshot:
        if not isinstance(locator, RetainedSourceLocator):
            raise TypeError("locator must be a RetainedSourceLocator")
        try:
            issued, source = self.retention_store._records[locator.handle]
        except KeyError as exc:
            raise SourceRetentionError("retained source handle is unknown or has been released") from exc
        if (
            (expected is not None and expected != issued)
            or locator.retention_store_id != self.retention_store.retention_store_id
            or source.source_snapshot_id.source_id != self.source_id
            or source.source_snapshot_id != locator.source_snapshot_id
            or source.content_id != locator.content_id
        ):
            raise SourceRetentionError("retained source does not match its issued store, snapshot, and content")
        return source


def _selector(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("source selector must be a non-empty, trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("source selector must not contain control characters")
    return value
