"""Git adapter for immutable workspace-backed dependency inspection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.application.dependencies import DependencyCommand, DependencyResult
from gitopsctr.application.snapshots import SnapshotReadError
from gitopsctr.errors import OperationError
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_model import ResourceRegistry
from gitopsctr.resources import ResourceCatalog
from gitopsctr.workspace_dependencies import dependency_workspace_provider


@dataclass(frozen=True, slots=True)
class GitDependencyInspector:
    """Resolve a Git-shaped source selector before evaluating a logical graph."""

    repository_root: Path
    snapshot_reader: GitSnapshotReader
    registry: ResourceRegistry

    def close(self) -> None:
        """The shared snapshot reader is owned by the application facade."""

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        try:
            snapshot_id = self.snapshot_reader.snapshot_id_for_revision(command.source_selector)
            revision = self.snapshot_reader.revision_for_snapshot(snapshot_id)
        except SnapshotReadError as exc:
            raise OperationError(f"source revision {command.source_selector!r} does not exist") from exc
        return dependency_workspace_provider(
            GitWorkspacePlaneProvider(self.repository_root, self.snapshot_reader, snapshot_id),
            self.registry,
            ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS),
            command,
            source_revision=revision,
            source_snapshot=snapshot_id,
        )
