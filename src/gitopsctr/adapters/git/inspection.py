"""Git-backed compatibility adapter for read-only resource inspection.

The adapter is the only layer in this vertical that translates the default
source/Git composition's snapshot-reference hints to the legacy inventory
reader.  The application receives a typed command and returns structured
tables/documents; it neither receives a path nor renders output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.inspection import build_resource_inspection


@dataclass(frozen=True, slots=True)
class GitResourceInspector:
    """Inspect one explicitly configured local source/Git repository."""

    repository_root: Path

    def close(self) -> None:
        """Satisfy the read-port lifecycle; each inspection owns its session."""

    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        """Translate the typed request to the compatibility inventory adapter."""

        values: dict[str, object] = {
            "selector": command.selector,
            "name": command.name,
            "environment": command.environment,
            "all_environments": command.all_environments,
            "desired_ref": command.desired_reference,
            "desired_revision": command.desired_snapshot,
            "observed_ref": command.observed_reference,
            "observed_revision": command.observed_snapshot,
            "output": command.output.value,
            "artifact": command.artifact,
            "artifacts": command.artifacts,
            "as_list": command.as_list,
        }
        values.update({item.name: item.value for item in command.filters})
        return build_resource_inspection(self.repository_root, argparse.Namespace(**values))
