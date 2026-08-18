"""Application-owned protocols for the first backend-neutral slice."""

from __future__ import annotations

from typing import Protocol

from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.application.model import (
    AcceptedDesiredSnapshot,
    ChannelId,
    EffectAuthorization,
    EffectIntent,
    EnvironmentId,
    ExecutionIdentity,
    HeadObservation,
    SnapshotId,
    SnapshotInspectionCommand,
    SnapshotInspectionResult,
    ValidateCommand,
    ValidationResult,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.status import StatusCommand, StatusResult


class SnapshotReader(Protocol):
    """Open an exact immutable snapshot as a logical content view."""

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        """Return the immutable view identified by ``snapshot_id`` exactly."""

    def close(self) -> None:
        """Release owned resources; repeated calls must be safe."""


class ChannelReader(Protocol):
    """Observe a mutable channel with an adapter-provided incarnation fence.

    This is intentionally distinct from :class:`SnapshotReader`: a backend
    must not claim the compare-and-swap semantics of ``HeadObservation`` until
    it has a real present-and-absent incarnation mechanism.
    """

    def resolve_head(self, channel_id: ChannelId) -> HeadObservation:
        """Return the current presence-or-absence observation for a channel."""


class DeploymentAuthority(Protocol):
    """Independently resolve the currently accepted desired state for an environment."""

    def accepted_desired_snapshot(self, environment_id: EnvironmentId) -> AcceptedDesiredSnapshot:
        """Issue a value bound to the exact authority and channel observations."""

    def revalidate_accepted_desired_snapshot(self, accepted: AcceptedDesiredSnapshot) -> None:
        """Fail closed unless the original authority and head bindings still hold.

        Orchestration calls this immediately before effect acquisition and again
        before it accepts post-effect evidence.
        """


class EffectFencing(Protocol):
    """Issue and revalidate exact authorization for an external resource effect."""

    def authorize_effect(
        self,
        accepted: AcceptedDesiredSnapshot,
        intent: EffectIntent,
    ) -> EffectAuthorization:
        """Acquire an adapter-issued lease/token/generation authorization."""

    def revalidate_effect(
        self,
        authorization: EffectAuthorization,
        accepted: AcceptedDesiredSnapshot,
    ) -> EffectAuthorization:
        """Fail closed or return the exact current authorization for completion."""


class RuntimeIdentityProvider(Protocol):
    """Supply explicit runner identity without leaking ambient process state."""

    def execution_identity(self) -> ExecutionIdentity:
        """Return the identity used for one application operation."""


class SpecificationValidator(Protocol):
    """Validate authored specifications selected by logical identities."""

    def validate(self, command: ValidateCommand) -> ValidationResult:
        """Return typed issues, or raise ValidationFailFastError when requested."""

    def close(self) -> None:
        """Release owned resources; repeated calls must be safe."""


class ResourceInspector(Protocol):
    """Read persisted resources through adapter-owned snapshot/workspace access."""

    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        """Return structured data for an incoming adapter to render."""

    def close(self) -> None:
        """Release owned resources; repeated calls must be safe."""


class StatusInspector(Protocol):
    """Read one coherent environment status through adapter-owned snapshots."""

    def status(self, command: StatusCommand) -> StatusResult:
        """Return closed status data for the requested environment."""

    def close(self) -> None:
        """Release owned resources; repeated calls must be safe."""


class Orchestrator(Protocol):
    """The incoming application port for the first typed use cases."""

    def validate(self, command: ValidateCommand) -> ValidationResult:
        """Validate one backend-neutral authored-input command."""

    def inspect_snapshot(self, command: SnapshotInspectionCommand) -> SnapshotInspectionResult:
        """Inspect one immutable snapshot through the application boundary."""

    def inspect_resources(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        """Inspect persisted resources through the configured read adapter."""

    def status(self, command: StatusCommand) -> StatusResult:
        """Read one coherent environment reconciliation status."""
