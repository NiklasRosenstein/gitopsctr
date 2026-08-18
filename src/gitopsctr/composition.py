"""The one default composition root for the local source-authored CLI."""

from __future__ import annotations

from pathlib import Path

from gitopsctr.adapters.git import (
    GitApplyService,
    GitAuthoredChangeDecoder,
    GitDependencyInspector,
    GitResourceInspector,
    GitSnapshotReader,
    GitStatusInspector,
)
from gitopsctr.adapters.source_authored import SourceAuthoredSpecificationValidator
from gitopsctr.application.services import ApplicationServices
from gitopsctr.registry import RESOURCE_REGISTRY


def create_default_application(repository: Path) -> ApplicationServices:
    """Compose the local Git snapshot and source-authored validation adapters."""

    # Preserve a root symlink for the authored-path policy to reject.  Resolving
    # here would erase the security-relevant fact before validation observes it.
    repository_root = repository.absolute()
    snapshot_reader = GitSnapshotReader.from_path(repository_root)
    return ApplicationServices(
        snapshot_reader,
        SourceAuthoredSpecificationValidator(repository_root),
        GitResourceInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitStatusInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitDependencyInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitApplyService(repository_root),
        GitAuthoredChangeDecoder(repository_root),
    )
