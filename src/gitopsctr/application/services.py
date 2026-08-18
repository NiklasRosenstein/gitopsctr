"""Small injected application services for the first read-only vertical."""

from __future__ import annotations

from dataclasses import dataclass, field

from gitopsctr.application.model import (
    SnapshotInspectionCommand,
    SnapshotInspectionResult,
    ValidateCommand,
    ValidationResult,
)
from gitopsctr.application.ports import SnapshotReader, SpecificationValidator


@dataclass(slots=True)
class ApplicationServices:
    """Application facade with explicit dependencies and no ambient backend."""

    snapshot_reader: SnapshotReader
    specification_validator: SpecificationValidator
    _closed: bool = field(default=False, init=False, repr=False)

    def validate(self, command: ValidateCommand) -> ValidationResult:
        """Validate authored specifications through the injected validator."""

        return self.specification_validator.validate(command)

    def inspect_snapshot(self, command: SnapshotInspectionCommand) -> SnapshotInspectionResult:
        """Inspect an exact immutable snapshot through the configured reader."""

        view = self.snapshot_reader.open_snapshot(command.snapshot_id)
        return SnapshotInspectionResult(view.snapshot_id, view.content_id)

    def close(self) -> None:
        """Close both explicit dependencies exactly once."""

        if self._closed:
            return
        self._closed = True
        try:
            self.specification_validator.close()
        finally:
            self.snapshot_reader.close()

    def __enter__(self) -> ApplicationServices:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
