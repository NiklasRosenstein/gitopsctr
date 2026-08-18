"""Phase-3 read adapters over the controlled authority plane provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitopsctr.adapters.git.dependencies import GitDependencyInspector
from gitopsctr.adapters.git.remote_authority import ControlledGitPublicationAuthority
from gitopsctr.adapters.git.remote_workspace_planes import ControlledGitWorkspacePlaneProvider
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.status import GitStatusInspector
from gitopsctr.adapters.git.workspace_inspection import inspect_workspace_provider
from gitopsctr.application.dependencies import DependencyCommand, DependencyResult
from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.application.status import StatusCommand, StatusResult
from gitopsctr.resource_model import ResourceRegistry


@dataclass(frozen=True, slots=True)
class ControlledGitResourceInspector:
    repository_root: Path
    authority: ControlledGitPublicationAuthority
    registry: ResourceRegistry

    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        return inspect_workspace_provider(
            ControlledGitWorkspacePlaneProvider(self.repository_root, self.authority),
            self.registry,
            command,
        )

    def close(self) -> None:
        """The application facade owns the authority session."""


@dataclass(frozen=True, slots=True)
class ControlledGitStatusInspector:
    repository_root: Path
    authority: ControlledGitPublicationAuthority
    registry: ResourceRegistry

    def status(self, command: StatusCommand) -> StatusResult:
        compatibility = GitStatusInspector(
            self.repository_root,
            self.authority,
            self.registry,
        )
        return compatibility.status_with_provider(
            ControlledGitWorkspacePlaneProvider(self.repository_root, self.authority),
            command,
        )

    def close(self) -> None:
        """The application facade owns the authority session."""


@dataclass(slots=True)
class ControlledGitDependencyInspector:
    repository_root: Path
    registry: ResourceRegistry
    _reader: GitSnapshotReader = field(init=False, repr=False)
    _delegate: GitDependencyInspector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._reader = GitSnapshotReader.from_path(self.repository_root)
        self._delegate = GitDependencyInspector(self.repository_root, self._reader, self.registry)

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        return self._delegate.dependencies(command)

    def close(self) -> None:
        self._reader.close()
