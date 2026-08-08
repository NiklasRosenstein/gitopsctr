"""Unit driver contracts and entry-point discovery.

A unit is a resource instance; a :class:`UnitDriver` implements the resource
kind and advertises the capabilities it supports.
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


@dataclass(frozen=True)
class ArtifactDocumentContract:
    """A logical artifact output and its independently versioned resource contract."""

    api_version: str
    kind: str
    contract: DocumentContract
    media_type: str


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
    artifact_contracts: ClassVar[Mapping[str, ArtifactDocumentContract]] = {}

    def scaffold_unit_spec(self, name: str, source_path: str) -> JsonObject | None:
        """Return an authored unit spec body, or ``None`` when scaffolding is unsupported.

        The CLI owns the resource identity and envelope. Drivers only provide
        fields below ``spec`` so they cannot accidentally select a different
        API kind, driver name, or unit name.
        """

        return None


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


def _register_artifact_gvk(
    contracts: dict[str, ArtifactDocumentContract],
    artifact: ArtifactDocumentContract,
) -> None:
    artifact_gvk = f"{artifact.api_version}/{artifact.kind}"
    registered = contracts.get(artifact_gvk)
    if registered is not None and (
        registered.contract.json_schema() != artifact.contract.json_schema()
        or registered.media_type != artifact.media_type
    ):
        raise DriverError(f"artifact GVK {artifact_gvk!r} has conflicting installed contracts")
    contracts[artifact_gvk] = artifact


def load_unit_drivers() -> dict[str, UnitDriver]:
    drivers: dict[str, UnitDriver] = {}
    artifact_gvks: dict[str, ArtifactDocumentContract] = {}
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
        for artifact_name, artifact in driver.artifact_contracts.items():
            if not artifact_name or not artifact_name.replace("-", "").isalnum() or not artifact_name.islower():
                raise DriverError(
                    f"unit driver entry point {entry_point.name!r} has invalid artifact name {artifact_name!r}"
                )
            if not isinstance(artifact, ArtifactDocumentContract) or not isinstance(
                artifact.contract, DocumentContract
            ):
                raise DriverError(
                    f"unit driver entry point {entry_point.name!r} has an invalid artifact contract"
                )
            if not all(
                isinstance(value, str) and value
                for value in (artifact.api_version, artifact.kind, artifact.media_type)
            ):
                raise DriverError(
                    f"unit driver entry point {entry_point.name!r} has incomplete artifact metadata"
                )
            properties = artifact.contract.json_schema().get("properties", {})
            if (
                not isinstance(properties, dict)
                or properties.get("apiVersion", {}).get("const") != artifact.api_version
                or properties.get("kind", {}).get("const") != artifact.kind
            ):
                raise DriverError(
                    f"unit driver entry point {entry_point.name!r} artifact {artifact_name!r} "
                    "metadata does not match its resource contract"
                )
            _register_artifact_gvk(artifact_gvks, artifact)
        if driver.artifact_contracts and not isinstance(driver, ReconciliationCapability):
            raise DriverError(
                f"unit driver entry point {entry_point.name!r} advertises artifacts without reconciliation"
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
    artifacts: object = None
    candidate = result
    if isinstance(result, Mapping) and "desired" in result and "driver" in result:
        reserved = {
            "$schema",
            "schema",
            "unit",
            "driver",
            "desired",
            "resolvedInputs",
            "controller",
            "artifacts",
        }
        candidate = {key: value for key, value in result.items() if key not in reserved}
        artifacts = result.get("artifacts")
    semantic = dict(driver.semantic_result(candidate))
    if isinstance(artifacts, Mapping) and artifacts:
        semantic["artifacts"] = {
            name: {
                key: descriptor[key]
                for key in ("apiVersion", "kind", "digest")
                if isinstance(descriptor, Mapping) and key in descriptor
            }
            for name, descriptor in artifacts.items()
        }
    return semantic
