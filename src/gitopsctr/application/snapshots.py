"""Immutable snapshot views shared by application services and adapters."""

from __future__ import annotations

from dataclasses import dataclass

from gitopsctr.application.model import ContentId, SnapshotId
from gitopsctr.application.workspace import ImmutableWorkspace


class SnapshotReadError(ValueError):
    """Raised when an adapter cannot safely open a requested snapshot."""


class SnapshotNotFoundError(SnapshotReadError):
    """Raised when an exact immutable snapshot is unavailable."""


@dataclass(frozen=True, slots=True)
class SnapshotView:
    """An exact immutable snapshot and its canonical logical content view."""

    snapshot_id: SnapshotId
    content_id: ContentId
    workspace: ImmutableWorkspace

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        if not isinstance(self.content_id, ContentId):
            raise TypeError("content_id must be a ContentId")
        if not isinstance(self.workspace, ImmutableWorkspace):
            raise TypeError("workspace must implement ImmutableWorkspace")
        if self.workspace.is_mutable:
            raise ValueError("a snapshot view must contain an immutable workspace")
        if self.workspace.content_id != self.content_id:
            raise ValueError("snapshot content_id must match its workspace canonicalization")
