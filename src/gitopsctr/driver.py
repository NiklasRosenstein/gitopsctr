"""Unit driver contracts and entry-point discovery.

A unit is a resource instance; a :class:`UnitDriver` implements the resource
kind and advertises the capabilities it supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from gitopsctr.api import GVK, ApiKind, api_kinds
from gitopsctr.artifacts import ArtifactApi, require_artifact_api
from gitopsctr.document import DocumentContract, JsonObject
from gitopsctr.execution import DriverExecution, default_driver_execution

type ReconciliationResult = Mapping[str, object]


class DriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationOutput:
    """Validated observation facts and artifact resources returned by reconciliation."""

    result: ReconciliationResult = field(default_factory=dict)
    artifacts: Mapping[str, JsonObject] = field(default_factory=dict)


class UnitDriver:
    """A versioned implementation of one unit API kind."""

    api_version: ClassVar[str] = "unit.gitopsctr.io/v1"
    kind: ClassVar[str]
    driver_name: ClassVar[str]
    version: ClassVar[int] = 0
    schema_base_uri: ClassVar[str | None] = None
    unit_contract: ClassVar[DocumentContract]
    desired_unit_contract: ClassVar[DocumentContract]
    result_contract: ClassVar[DocumentContract]
    artifact_outputs: ClassVar[Mapping[str, ApiKind[ArtifactApi[Any]]]] = {}

    def scaffold_unit_spec(self, name: str, source_path: str) -> JsonObject | None:
        """Return an authored unit spec body, or ``None`` when scaffolding is unsupported.

        The CLI owns the resource identity and envelope. Drivers only provide
        fields below ``spec`` so they cannot accidentally select a different
        API kind, driver name, or unit name.
        """

        return None


def unit_driver_api(driver: UnitDriver) -> ApiKind[UnitDriver]:
    """Expose a unit driver through the generic API-kind entry-point interface."""

    return ApiKind(GVK(driver.api_version, driver.kind), driver)


@dataclass(frozen=True)
class MaterializationContext:
    environment: str
    source_root: Path
    source_revision: str
    source_path: str
    unit: JsonObject
    output_root: Path
    execution: DriverExecution = field(default_factory=default_driver_execution)


@dataclass(frozen=True)
class MaterializationResult:
    media_type: str
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class UnitExecutionContext:
    environment: str
    desired_root: Path
    desired_revision: str
    source_root: Path
    source_revision: str
    source_path: str
    unit: JsonObject
    inputs: JsonObject
    report: Path | None = None
    execution: DriverExecution = field(default_factory=default_driver_execution)


@dataclass(frozen=True)
class PlanningContext(UnitExecutionContext):
    pass


@dataclass(frozen=True)
class ReconciliationContext(UnitExecutionContext):
    previous_receipt: JsonObject | None = None


@dataclass(frozen=True)
class VerificationContext:
    environment: str
    desired_root: Path
    desired_revision: str
    source_root: Path
    source_revision: str
    source_path: str
    unit: JsonObject
    inputs: JsonObject
    report: Path | None = None
    execution: DriverExecution = field(default_factory=default_driver_execution)


class MaterializationCapability(ABC):
    """Produce immutable files while desired state is advanced."""

    @abstractmethod
    def materialize(self, context: MaterializationContext) -> MaterializationResult:
        """Write materialized files below ``output_root`` and describe them."""


class PlanningCapability(ABC):
    """Perform speculative, non-publishing work for a deployment unit."""

    @abstractmethod
    def plan(self, context: PlanningContext) -> None:
        """Validate and plan the unit without changing remote deployment state."""


class ReconciliationCapability(ABC):
    """Converge external state for a fully materialized unit."""

    def reconciliation_required(self, unit: JsonObject) -> bool:
        return True

    @abstractmethod
    def reconcile(self, context: ReconciliationContext) -> ReconciliationOutput:
        """Converge one unit and return its observation facts and artifact resources."""

    @abstractmethod
    def semantic_result(self, result: object) -> ReconciliationResult:
        """Select receipt fields that define the external deployment result."""


class VerificationStatus(StrEnum):
    CLEAN = "CLEAN"
    DRIFT = "DRIFT"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus


class VerificationCapability(ABC):
    """Compare external state with desired state without writing a receipt."""

    def verification_supported(self, unit: JsonObject) -> bool:
        return True

    @abstractmethod
    def verify(self, context: VerificationContext) -> VerificationResult:
        """Return whether external state matches the fully materialized unit."""


SemanticResultSelector = Callable[[object], ReconciliationResult]


def load_unit_drivers(
    installed_api_kinds: Mapping[GVK, ApiKind[object]] | None = None,
) -> dict[str, UnitDriver]:
    drivers: dict[str, UnitDriver] = {}
    installed = api_kinds() if installed_api_kinds is None else installed_api_kinds
    for api_kind in installed.values():
        if isinstance(api_kind.spec, ArtifactApi):
            require_artifact_api(api_kind)
    for api_kind in installed.values():
        driver = api_kind.spec
        if not isinstance(driver, UnitDriver):
            continue
        if isinstance(driver.version, bool) or not isinstance(driver.version, int) or driver.version < 1:
            raise DriverError(f"unit driver API {api_kind.gvk!s} has an invalid version")
        if not isinstance(driver, (MaterializationCapability, ReconciliationCapability)):
            raise DriverError(f"unit driver API {api_kind.gvk!s} has no materialization or reconciliation capability")
        expected_gvk = GVK(driver.api_version, driver.kind)
        if api_kind.gvk != expected_gvk:
            raise DriverError(f"unit driver API {api_kind.gvk!s} does not match driver kind {expected_gvk!s}")
        if not driver.driver_name:
            raise DriverError(f"unit driver API {api_kind.gvk!s} has no driver_name")
        if driver.driver_name in drivers:
            raise DriverError(f"duplicate unit driver entry point: {driver.driver_name}")
        for kind in ("unit", "desired_unit", "result"):
            contract = getattr(driver, f"{kind}_contract", None)
            if not isinstance(contract, DocumentContract):
                raise DriverError(f"unit driver API {api_kind.gvk!s} has no {kind.replace('_', '-')} contract")
        for artifact_name, artifact_kind in driver.artifact_outputs.items():
            if not artifact_name or not artifact_name.replace("-", "").isalnum() or not artifact_name.islower():
                raise DriverError(f"unit driver API {api_kind.gvk!s} has invalid artifact name {artifact_name!r}")
            if not isinstance(artifact_kind, ApiKind) or not isinstance(artifact_kind.spec, ArtifactApi):
                raise DriverError(f"unit driver API {api_kind.gvk!s} has an invalid artifact API reference")
            if installed.get(artifact_kind.gvk) is not artifact_kind:
                raise DriverError(
                    f"unit driver API {api_kind.gvk!s} artifact {artifact_name!r} does not reference "
                    f"the authoritative registration for {artifact_kind.gvk}"
                )
        if driver.artifact_outputs and not isinstance(driver, ReconciliationCapability):
            raise DriverError(f"unit driver API {api_kind.gvk!s} advertises artifacts without reconciliation")
        drivers[driver.driver_name] = driver
    return drivers
