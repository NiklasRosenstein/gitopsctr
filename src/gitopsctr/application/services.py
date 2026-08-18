"""Small injected application services for the first read-only vertical."""

from __future__ import annotations

from dataclasses import dataclass, field

from gitopsctr.application.apply import ApplyCommand, ApplyResult, AuthoredChangeSet
from gitopsctr.application.dependencies import DependencyCommand, DependencyResult
from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.application.model import (
    SnapshotInspectionCommand,
    SnapshotInspectionResult,
    ValidateCommand,
    ValidationResult,
)
from gitopsctr.application.ports import (
    ApplyService,
    AuthoredChangeDecoder,
    DependencyInspector,
    ResourceInspector,
    SnapshotReader,
    SpecificationValidator,
    StatusInspector,
)
from gitopsctr.application.status import StatusCommand, StatusResult


@dataclass(slots=True)
class ApplicationServices:
    """Application facade with explicit dependencies and no ambient backend."""

    snapshot_reader: SnapshotReader
    specification_validator: SpecificationValidator
    resource_inspector: ResourceInspector
    status_inspector: StatusInspector
    dependency_inspector: DependencyInspector
    apply_service: ApplyService | None = None
    authored_change_decoder: AuthoredChangeDecoder | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def validate(self, command: ValidateCommand) -> ValidationResult:
        """Validate authored specifications through the injected validator."""

        return self.specification_validator.validate(command)

    def inspect_snapshot(self, command: SnapshotInspectionCommand) -> SnapshotInspectionResult:
        """Inspect an exact immutable snapshot through the configured reader."""

        view = self.snapshot_reader.open_snapshot(command.snapshot_id)
        return SnapshotInspectionResult(view.snapshot_id, view.content_id)

    def inspect_resources(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        """Inspect resources through the injected read adapter."""

        return self.resource_inspector.inspect(command)

    def status(self, command: StatusCommand) -> StatusResult:
        """Read environment status through the configured typed status adapter."""

        return self.status_inspector.status(command)

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        """Read an exact source dependency graph through the configured adapter."""

        return self.dependency_inspector.dependencies(command)

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet | None = None) -> ApplyResult:
        """Coordinate apply through the explicitly composed service."""

        if self.apply_service is None:
            raise RuntimeError("the configured application does not provide apply")
        if changes is None:
            if self.authored_change_decoder is None:
                raise RuntimeError("the configured application does not provide authored input decoding")
            changes = self.authored_change_decoder.decode(command)
        return self.apply_service.apply(command, changes)

    def close(self) -> None:
        """Close both explicit dependencies exactly once."""

        if self._closed:
            return
        self._closed = True
        try:
            if self.authored_change_decoder is not None:
                self.authored_change_decoder.close()
        finally:
            try:
                if self.apply_service is not None:
                    self.apply_service.close()
            finally:
                try:
                    self.dependency_inspector.close()
                finally:
                    try:
                        self.status_inspector.close()
                    finally:
                        try:
                            self.resource_inspector.close()
                        finally:
                            try:
                                self.specification_validator.close()
                            finally:
                                self.snapshot_reader.close()

    def __enter__(self) -> ApplicationServices:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
