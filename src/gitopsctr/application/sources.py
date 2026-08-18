"""Typed acquisition and retention of immutable authored sources.

Selectors remain unresolved requests at this boundary.  A source adapter alone
may resolve one into a :class:`SourceSnapshot`, whose opaque snapshot identity
and logical content are then stable even if the source's selector moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from gitopsctr.application.model import (
    ContentId,
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace


class SourceError(ValueError):
    """Base error for source selection and retention failures."""


class SourceNotFoundError(SourceError):
    """Raised when an unresolved source selector has no available snapshot."""


class SourceRetentionError(SourceError):
    """Raised when a retention handle is unknown, tampered, or released."""


def _selector(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("source selector must be a non-empty, trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("source selector must not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """An unresolved source selector supplied by an incoming adapter.

    ``selector`` is intentionally only text: a local path, remote transport,
    ref spelling, and any source-specific access mechanism remain adapter
    concerns.  Callers receive an exact ``SourceSnapshot`` before they may
    inspect content or ask for retention.
    """

    source_id: SourceId
    selector: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, SourceId):
            raise TypeError("source_id must be a SourceId")
        _selector(self.selector)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One resolved immutable source and its safe logical workspace payload."""

    source_snapshot_id: SourceSnapshotId
    content_id: ContentId
    workspace: ImmutableWorkspace

    def __post_init__(self) -> None:
        if not isinstance(self.source_snapshot_id, SourceSnapshotId):
            raise TypeError("source_snapshot_id must be a SourceSnapshotId")
        if not isinstance(self.content_id, ContentId):
            raise TypeError("content_id must be a ContentId")
        if not isinstance(self.workspace, ImmutableWorkspace):
            raise TypeError("workspace must implement ImmutableWorkspace")
        if self.workspace.is_mutable:
            raise ValueError("a source snapshot must contain an immutable workspace")
        if self.workspace.content_id != self.content_id:
            raise ValueError("source snapshot content_id must match its workspace canonicalization")


@dataclass(frozen=True, slots=True)
class RetainedSourceLocator:
    """Untrusted, persistable evidence used to reissue a retained capability.

    This deliberately carries no issuance proof and confers no authority by
    itself.  A retention adapter validates every field against its durable
    record before it can issue a fresh :class:`RetainedSource` in another
    process.
    """

    handle: RetainedSourceHandle
    retention_store_id: RetentionStoreId
    source_snapshot_id: SourceSnapshotId
    content_id: ContentId

    def __post_init__(self) -> None:
        if not isinstance(self.handle, RetainedSourceHandle):
            raise TypeError("handle must be a RetainedSourceHandle")
        if not isinstance(self.retention_store_id, RetentionStoreId):
            raise TypeError("retention_store_id must be a RetentionStoreId")
        if not isinstance(self.source_snapshot_id, SourceSnapshotId):
            raise TypeError("source_snapshot_id must be a SourceSnapshotId")
        if not isinstance(self.content_id, ContentId):
            raise TypeError("content_id must be a ContentId")

    @classmethod
    def from_retained(cls, retained: RetainedSource) -> RetainedSourceLocator:
        """Copy public evidence from an already-issued retention capability."""

        if not isinstance(retained, RetainedSource):
            raise TypeError("retained must be a RetainedSource")
        retained._validate()
        return cls(retained.handle, retained.retention_store_id, retained.source_snapshot_id, retained.content_id)

    def to_wire(self) -> str:
        """Encode only untrusted evidence suitable for an operation record."""

        return json.dumps(
            {
                "content": self.content_id.value,
                "handle": self.handle.value,
                "source": self.source_snapshot_id.source_id.value,
                "snapshot": self.source_snapshot_id.snapshot_id.value,
                "store": self.retention_store_id.value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_wire(cls, value: str) -> RetainedSourceLocator:
        """Decode untrusted persisted evidence without issuing authority."""

        try:
            data = json.loads(value)
            if not isinstance(data, dict) or set(data) != {"content", "handle", "source", "snapshot", "store"}:
                raise ValueError("retained source locator has an invalid shape")
            return cls(
                RetainedSourceHandle(data["handle"]),
                RetentionStoreId(data["store"]),
                SourceSnapshotId(SourceId(data["source"]), SnapshotId(data["snapshot"])),
                ContentId(data["content"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("retained source locator is invalid") from exc


class SourceRepository(Protocol):
    """Resolve, retain, recover, and release exact logical source snapshots."""

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        """Resolve a selector exactly once into an immutable source snapshot."""

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        """Issue a durable handle for one adapter-issued exact source snapshot."""

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        """Recover the retained immutable payload, or fail closed."""

    def release(self, retained: RetainedSource) -> None:
        """Release one exact retained handle, or fail closed."""

    def reissue(self, locator: RetainedSourceLocator) -> RetainedSource:
        """Validate persisted evidence and issue a fresh retention capability."""

    def close(self) -> None:
        """Release adapter-owned resources; repeated calls must be safe."""


def copied_source_snapshot(source: SourceSnapshot) -> SourceSnapshot:
    """Return a fresh immutable logical copy of an exact source payload.

    Adapters use this at their durability boundary so recovered content does
    not depend on a still-live repository, selector, or caller-owned view.
    """

    if not isinstance(source, SourceSnapshot):
        raise TypeError("source must be a SourceSnapshot")
    workspace = InMemoryWorkspace(
        source.workspace.list_entries(), capabilities=source.workspace.capabilities, mutable=False
    )
    return SourceSnapshot(source.source_snapshot_id, workspace.content_id, workspace)


def same_source_payload(left: SourceSnapshot, right: SourceSnapshot) -> bool:
    """Compare the identity-bearing source payload without trusting handles."""

    return (
        left.source_snapshot_id == right.source_snapshot_id
        and left.content_id == right.content_id
        and left.workspace.capabilities == right.workspace.capabilities
        and left.workspace.list_entries() == right.workspace.list_entries()
    )
