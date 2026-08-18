"""In-memory adapter for exact logical-workspace dependency inspection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from gitopsctr.application.dependencies import DependencyCommand, DependencyResult
from gitopsctr.application.model import SnapshotId
from gitopsctr.application.workspace import ImmutableWorkspace
from gitopsctr.errors import OperationError
from gitopsctr.formats import Project, parse_document_bytes, validate_project_document
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_model import ResourcePlane, ResourceRegistry
from gitopsctr.resources import ResourceCatalog
from gitopsctr.workspace_dependencies import dependency_workspace_provider
from gitopsctr.workspace_inspection import WorkspaceSnapshot


@dataclass(frozen=True, slots=True)
class MemoryDependencySnapshot:
    """One independently supplied exact source state for the memory adapter."""

    revision: str
    snapshot_id: SnapshotId
    workspace: ImmutableWorkspace

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("memory dependency revision must be non-empty")
        if self.workspace.is_mutable:
            raise ValueError("memory dependency workspace must be immutable")


class _MemoryDependencyPlanes:
    """Minimal source-only plane provider; no Git or filesystem fallback exists."""

    def __init__(self, snapshot: MemoryDependencySnapshot) -> None:
        self._snapshot = snapshot
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def project(self) -> Project:
        candidates = tuple(key for key in ("gitopsctr.yaml", "gitopsctr.yml", "gitopsctr.json") if self._has_file(key))
        if not candidates:
            raise OperationError("source tree has no Project configuration: gitopsctr.yaml")
        if len(candidates) != 1:
            raise OperationError("multiple Project configuration files exist: " + ", ".join(candidates))
        key = candidates[0]
        path = PurePosixPath(key)
        try:
            return validate_project_document(parse_document_bytes(self._snapshot.workspace.read(key), path), path)
        except Exception as exc:
            raise OperationError(str(exc)) from exc

    def source(self) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            # Source snapshots intentionally expose no ref/revision metadata;
            # the result carries the adapter-issued exact selection.
            ResourcePlane.SOURCE,
            None,
            None,
            None,
            self._snapshot.workspace,
            self._snapshot.workspace.entry_content_ids(),
        )

    def snapshot(self, *_args: object, **_kwargs: object) -> WorkspaceSnapshot:
        raise OperationError("dependency inspection does not read deployment plane snapshots")

    def _has_file(self, key: str) -> bool:
        try:
            self._snapshot.workspace.read(key)
        except Exception:
            return False
        return True


@dataclass(frozen=True, slots=True)
class MemoryDependencyInspector:
    """Resolve opaque source selectors against explicitly installed memory views."""

    sources: Mapping[str, MemoryDependencySnapshot]
    registry: ResourceRegistry

    def close(self) -> None:
        """In-memory snapshots are caller-owned immutable values."""

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        snapshot = self.sources.get(command.source_selector)
        if snapshot is None:
            raise OperationError(f"source revision {command.source_selector!r} does not exist")
        return dependency_workspace_provider(
            _MemoryDependencyPlanes(snapshot),
            self.registry,
            ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS),
            command,
            source_revision=snapshot.revision,
            source_snapshot=snapshot.snapshot_id,
        )
