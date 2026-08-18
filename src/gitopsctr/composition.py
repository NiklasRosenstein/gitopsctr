"""The one default composition root for the local source-authored CLI."""

from __future__ import annotations

from pathlib import Path

from gitopsctr.adapters.git import GitSnapshotReader
from gitopsctr.adapters.source_authored import SourceAuthoredSpecificationValidator
from gitopsctr.application.services import ApplicationServices


def create_default_application(repository: Path) -> ApplicationServices:
    """Compose the local Git snapshot and source-authored validation adapters."""

    # Preserve a root symlink for the authored-path policy to reject.  Resolving
    # here would erase the security-relevant fact before validation observes it.
    repository_root = repository.absolute()
    return ApplicationServices(
        GitSnapshotReader.from_path(repository_root),
        SourceAuthoredSpecificationValidator(repository_root),
    )
