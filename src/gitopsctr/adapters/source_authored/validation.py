"""Source-authored implementation of the application validation port.

This is a phase-2 compatibility adapter.  It delegates to the existing
authored-resource decoder until that decoder has moved completely behind the
application boundary.  The repository root is an explicit construction-time
dependency; the adapter never consults controller process-global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gitopsctr.application.model import ValidateCommand, ValidationResult


@dataclass(frozen=True, slots=True)
class SourceAuthoredSpecificationValidator:
    """Validate source-authored resources rooted at one fixed repository."""

    repository_root: Path

    def close(self) -> None:
        """Satisfy the validator lifecycle; no resources are held open."""

    def validate(self, command: ValidateCommand) -> ValidationResult:
        """Delegate lazily while authored decoding remains in the controller."""

        from gitopsctr.controller import validate_authored_resources

        return validate_authored_resources(self.repository_root, command)
