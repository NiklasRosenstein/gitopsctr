"""Logical-workspace inspection contracts shared by read-only adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from gitopsctr.application.model import ContentId
from gitopsctr.application.snapshots import SnapshotId
from gitopsctr.application.workspace import ImmutableWorkspace, WorkspaceEntryKind
from gitopsctr.formats import Project
from gitopsctr.resource_model import ResourcePlane


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Exact logical content selected for one plane.

    ``snapshot_id`` is an opaque read identity, not a compare-and-swap fence.
    ``content_ids`` retain the per-entry logical identities of the workspace.
    """

    plane: ResourcePlane
    reference: str | None
    revision: str | None
    snapshot_id: SnapshotId | None
    workspace: ImmutableWorkspace
    content_ids: Mapping[str, ContentId]

    def __post_init__(self) -> None:
        if not isinstance(self.plane, ResourcePlane):
            raise TypeError("workspace snapshot plane must be a ResourcePlane")
        if not isinstance(self.workspace, ImmutableWorkspace):
            raise TypeError("workspace snapshot must contain an ImmutableWorkspace")
        if self.workspace.is_mutable is not False:
            raise ValueError("workspace snapshot must contain an immutable workspace")
        if not isinstance(self.content_ids, Mapping):
            raise TypeError("workspace snapshot content_ids must be a mapping")

        entries = self.workspace.list_entries()
        files = {entry.key for entry in entries if entry.kind is WorkspaceEntryKind.FILE}
        expected_content_ids = self.workspace.entry_content_ids()
        copied: dict[str, ContentId] = {}
        for key, value in self.content_ids.items():
            if not isinstance(key, str) or not key:
                raise TypeError("workspace snapshot content provenance keys must be non-empty strings")
            if not isinstance(value, ContentId):
                raise TypeError("workspace snapshot content provenance values must be ContentId instances")
            if key not in files:
                raise ValueError("workspace snapshot provenance must name a logical file")
            copied[key] = value
        if copied != dict(expected_content_ids):
            raise ValueError("workspace snapshot provenance must equal its logical entry identities")

        if self.plane is ResourcePlane.SOURCE:
            if self.reference is not None or self.revision is not None or self.snapshot_id is not None:
                raise ValueError("source workspace snapshots cannot have selection provenance")
        elif self.plane in {ResourcePlane.DESIRED, ResourcePlane.OBSERVED}:
            if not isinstance(self.reference, str) or not self.reference:
                raise ValueError("selected workspace snapshots require a non-empty reference")
            missing = self.revision is None and self.snapshot_id is None
            present = self.revision is not None and self.snapshot_id is not None
            if not missing and not present:
                raise ValueError("selected workspace snapshots require both revision and snapshot_id")
            if missing:
                if entries or copied:
                    raise ValueError("missing workspace snapshots must have empty content and provenance")
            else:
                if not isinstance(self.revision, str) or not self.revision:
                    raise ValueError("selected workspace snapshots require a non-empty revision")
                if not isinstance(self.snapshot_id, SnapshotId):
                    raise TypeError("selected workspace snapshots require an exact SnapshotId")
        else:
            raise ValueError("workspace snapshots support source, desired, and observed planes only")
        object.__setattr__(self, "content_ids", MappingProxyType(copied))


class WorkspacePlaneProvider(Protocol):
    """Read-only source and plane snapshots expressed as logical workspaces."""

    def close(self) -> None: ...

    def project(self) -> Project: ...

    def source(self) -> WorkspaceSnapshot: ...

    def snapshot(
        self,
        plane: ResourcePlane,
        reference: str,
        revision: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> WorkspaceSnapshot: ...
