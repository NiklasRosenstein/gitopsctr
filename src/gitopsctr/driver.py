"""Unit driver contracts and entry-point discovery.

A unit is a resource instance; a :class:`UnitDriver` implements the resource
kind and advertises the capabilities it supports.  ``Plugin`` remains a
packaging/discovery term, not the runtime domain model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

from gitopsctr.document import DocumentContract, JsonObject
from gitopsctr.execution import DriverExecution, default_driver_execution

type ReconciliationResult = Mapping[str, object]


class DriverError(RuntimeError):
    pass


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
    def reconcile(self, context: ReconciliationContext) -> ReconciliationResult:
        """Converge one unit and return fields for its observation receipt."""

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


def load_unit_drivers() -> dict[str, UnitDriver]:
    drivers: dict[str, UnitDriver] = {}
    for entry_point in entry_points(group="gitopsctr.drivers"):
        driver = entry_point.load()
        if not isinstance(driver, UnitDriver):
            raise DriverError(f"unit driver entry point {entry_point.name!r} did not load a UnitDriver")
        if isinstance(driver.version, bool) or not isinstance(driver.version, int) or driver.version < 1:
            raise DriverError(f"unit driver entry point {entry_point.name!r} has an invalid version")
        if not isinstance(driver, (MaterializationCapability, ReconciliationCapability)):
            raise DriverError(
                f"unit driver entry point {entry_point.name!r} has no materialization or reconciliation capability"
            )
        expected_gvk = f"{driver.api_version}/{driver.kind}"
        if entry_point.name != expected_gvk:
            raise DriverError(
                f"unit driver entry point {entry_point.name!r} does not match declared API kind {expected_gvk!r}"
            )
        if not driver.driver_name:
            raise DriverError(f"unit driver entry point {entry_point.name!r} has no driver_name")
        if driver.driver_name in drivers:
            raise DriverError(f"duplicate unit driver entry point: {driver.driver_name}")
        for kind in ("unit", "desired_unit", "result"):
            contract = getattr(driver, f"{kind}_contract", None)
            if not isinstance(contract, DocumentContract):
                raise DriverError(
                    f"unit driver entry point {entry_point.name!r} has no {kind.replace('_', '-')} contract"
                )
        drivers[driver.driver_name] = driver
    return drivers


UNIT_DRIVERS = load_unit_drivers()
DRIVER_GVKS = {name: f"{driver.api_version}/{driver.kind}" for name, driver in UNIT_DRIVERS.items()}
DRIVER_NAMES_BY_GVK = {gvk: name for name, gvk in DRIVER_GVKS.items()}
DRIVER_VERSIONS = {name: driver.version for name, driver in UNIT_DRIVERS.items()}
MATERIALIZATION_DRIVERS = {
    name: driver for name, driver in UNIT_DRIVERS.items() if isinstance(driver, MaterializationCapability)
}
RECONCILIATION_DRIVERS = {
    name: driver for name, driver in UNIT_DRIVERS.items() if isinstance(driver, ReconciliationCapability)
}
PLANNING_DRIVERS = {name: driver for name, driver in UNIT_DRIVERS.items() if isinstance(driver, PlanningCapability)}
VERIFICATION_DRIVERS = {
    name: driver for name, driver in UNIT_DRIVERS.items() if isinstance(driver, VerificationCapability)
}


def semantic_reconciliation_result(driver_name: str, result: object) -> ReconciliationResult:
    try:
        driver = RECONCILIATION_DRIVERS[driver_name]
    except KeyError as exc:
        raise DriverError(f"unit driver does not support reconciliation: {driver_name}") from exc
    return driver.semantic_result(result)


# Transitional aliases keep the existing CLI/test surface usable while the
# repository documents and entry points move to the UnitDriver vocabulary.
UnitPlugin = UnitDriver
load_unit_plugins = load_unit_drivers
UNIT_PLUGINS = UNIT_DRIVERS
PLUGIN_VERSIONS = DRIVER_VERSIONS
MATERIALIZATION_PLUGINS = MATERIALIZATION_DRIVERS
RECONCILIATION_PLUGINS = RECONCILIATION_DRIVERS
PLANNING_PLUGINS = PLANNING_DRIVERS
VERIFICATION_PLUGINS = VERIFICATION_DRIVERS
