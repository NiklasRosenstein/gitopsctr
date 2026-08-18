"""Git-backed logical-workspace adapter for read-only resource inspection.

The adapter translates default source/Git selection hints to exact immutable
snapshots, while application callers receive typed tables/documents only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_inspection import inspect_git_workspaces
from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.resource_model import ResourceRegistry


@dataclass(frozen=True, slots=True)
class GitResourceInspector:
    """Inspect one explicitly configured local source/Git repository."""

    repository_root: Path
    snapshot_reader: GitSnapshotReader
    registry: ResourceRegistry

    def close(self) -> None:
        """Satisfy the read-port lifecycle; each inspection owns its session."""

    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        """Inspect through explicit catalog and exact workspace dependencies."""

        return inspect_git_workspaces(self.repository_root, self.snapshot_reader, self.registry, command)
