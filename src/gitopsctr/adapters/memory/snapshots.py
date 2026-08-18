"""In-memory exact snapshots and incarnation-fenced channel observations."""

from __future__ import annotations

from dataclasses import dataclass, field

from gitopsctr.application.model import ChannelId, HeadObservation, SnapshotId
from gitopsctr.application.snapshots import SnapshotNotFoundError, SnapshotView
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace


@dataclass
class InMemorySnapshotStore:
    """A deterministic read adapter with genuine mutable-channel incarnations.

    Snapshot installation takes a logical copy, so subsequently mutating the
    caller's workspace cannot alter an already installed immutable snapshot.
    Channel updates have a process-local monotonic sequence and therefore fence
    presence, absence, and an ``A -> B -> A`` sequence alike.
    """

    _snapshots: dict[SnapshotId, SnapshotView] = field(default_factory=dict, init=False, repr=False)
    _heads: dict[ChannelId, SnapshotId | None] = field(default_factory=dict, init=False, repr=False)
    _head_versions: dict[ChannelId, int] = field(default_factory=dict, init=False, repr=False)

    def close(self) -> None:
        """Satisfy the snapshot-reader lifecycle; no resources are owned."""

    def install(self, snapshot_id: SnapshotId, workspace: ImmutableWorkspace) -> SnapshotView:
        """Install one exact immutable logical snapshot, rejecting ID reuse."""

        if not isinstance(snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        if not isinstance(workspace, ImmutableWorkspace):
            raise TypeError("workspace must implement ImmutableWorkspace")
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
        """Open a fresh immutable logical view of the requested exact snapshot."""

        if not isinstance(snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        try:
            stored = self._snapshots[snapshot_id]
        except KeyError as exc:
            raise SnapshotNotFoundError(f"snapshot does not exist: {snapshot_id}") from exc
        workspace = InMemoryWorkspace(
            stored.workspace.list_entries(), capabilities=stored.workspace.capabilities, mutable=False
        )
        return SnapshotView(stored.snapshot_id, stored.content_id, workspace)

    def set_head(self, channel_id: ChannelId, snapshot_id: SnapshotId) -> HeadObservation:
        """Move a channel to an installed snapshot and issue a fresh fence."""

        if not isinstance(channel_id, ChannelId):
            raise TypeError("channel_id must be a ChannelId")
        self.open_snapshot(snapshot_id)
        self._heads[channel_id] = snapshot_id
        return self._advance_head(channel_id)

    def clear_head(self, channel_id: ChannelId) -> HeadObservation:
        """Make a channel absent and issue a fresh absence fence."""

        if not isinstance(channel_id, ChannelId):
            raise TypeError("channel_id must be a ChannelId")
        self._heads[channel_id] = None
        return self._advance_head(channel_id)

    def resolve_head(self, channel_id: ChannelId) -> HeadObservation:
        """Observe the current present or absent channel head without updating it."""

        if not isinstance(channel_id, ChannelId):
            raise TypeError("channel_id must be a ChannelId")
        version = self._head_versions.get(channel_id, 0)
        snapshot_id = self._heads.get(channel_id)
        incarnation = f"memory:{version}"
        if snapshot_id is None:
            return HeadObservation.absent(channel_id, incarnation)
        return HeadObservation.present(channel_id, snapshot_id, incarnation)

    def _advance_head(self, channel_id: ChannelId) -> HeadObservation:
        self._head_versions[channel_id] = self._head_versions.get(channel_id, 0) + 1
        return self.resolve_head(channel_id)
