"""Git-only source lineage bridge for controller-free projection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gitopsctr.application.apply_compilers import SourceLineage
from gitopsctr.application.apply_projection import RetainedSourceDescriptor, RetainedSourcePlane
from gitopsctr.application.model import SourceId

_GIT_SOURCE_SNAPSHOT = re.compile(r"git-source:([0-9a-f]{40})$")


class GitSourceLineageError(ValueError):
    """An issued generic source cannot be represented by Git schema fields."""


@dataclass(frozen=True, slots=True)
class GitSourceLineageEncoder:
    """Explicit SourceId-to-repository policy for the temporary Git seam.

    A retained source must be an exact ``git-source:<sha>`` source snapshot.
    The exact retained plane carries that source snapshot identity; its SHA is
    the commit used by the current contract.  Promotion lineage validates its
    separate ``git-commit:<sha>`` state snapshots in its own adapter.
    """

    repositories: Mapping[SourceId, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repositories", MappingProxyType(dict(self.repositories)))
        for source_id, repository in self.repositories.items():
            if not isinstance(source_id, SourceId) or not isinstance(repository, str) or not repository:
                raise GitSourceLineageError("Git source repository policy contains an invalid SourceId mapping")

    def encode(self, descriptor: RetainedSourceDescriptor, plane: RetainedSourcePlane) -> SourceLineage:
        descriptor._validate()
        plane._validate()
        if descriptor not in plane.descriptors:
            raise GitSourceLineageError("Git source descriptor is not bound to the recovered plane")
        if descriptor.source_id != plane.retained.source_snapshot_id.source_id:
            raise GitSourceLineageError("Git source descriptor SourceId does not match retained source")
        source_snapshot = plane.retained.source_snapshot_id.snapshot_id.value
        if _GIT_SOURCE_SNAPSHOT.fullmatch(source_snapshot) is None:
            raise GitSourceLineageError("Git source retained snapshot is not an exact git-source ID")
        match = _GIT_SOURCE_SNAPSHOT.fullmatch(source_snapshot)
        assert match is not None
        try:
            repository = self.repositories[descriptor.source_id]
        except KeyError as exc:
            raise GitSourceLineageError("Git source has no configured SourceId-to-repository mapping") from exc
        return SourceLineage(repository=repository, revision=match.group(1))
