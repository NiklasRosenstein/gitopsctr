"""Unit plugin contracts and entry-point discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type ReconciliationResult = Mapping[str, object]


class DriverError(RuntimeError):
    pass


class UnitPlugin:
    """A versioned deployment-unit plugin discovered through an entry point."""

    version: ClassVar[int] = 0


@dataclass(frozen=True)
class MaterializationContext:
    environment: str
    source_root: Path
    source_revision: str
    source_path: str
    unit: JsonObject
    output_root: Path


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


def load_unit_plugins() -> dict[str, UnitPlugin]:
    plugins: dict[str, UnitPlugin] = {}
    for entry_point in entry_points(group="gitopsctr.drivers"):
        if entry_point.name in plugins:
            raise DriverError(f"duplicate unit plugin entry point: {entry_point.name}")
        plugin = entry_point.load()
        if not isinstance(plugin, UnitPlugin):
            raise DriverError(f"unit plugin entry point {entry_point.name!r} did not load a UnitPlugin")
        if isinstance(plugin.version, bool) or not isinstance(plugin.version, int) or plugin.version < 1:
            raise DriverError(f"unit plugin entry point {entry_point.name!r} has an invalid version")
        if not isinstance(plugin, (MaterializationCapability, ReconciliationCapability)):
            raise DriverError(
                f"unit plugin entry point {entry_point.name!r} has no materialization or reconciliation capability"
            )
        plugins[entry_point.name] = plugin
    return plugins


UNIT_PLUGINS = load_unit_plugins()
PLUGIN_VERSIONS = {name: plugin.version for name, plugin in UNIT_PLUGINS.items()}
MATERIALIZATION_PLUGINS = {
    name: plugin for name, plugin in UNIT_PLUGINS.items() if isinstance(plugin, MaterializationCapability)
}
RECONCILIATION_PLUGINS = {
    name: plugin for name, plugin in UNIT_PLUGINS.items() if isinstance(plugin, ReconciliationCapability)
}
PLANNING_PLUGINS = {name: plugin for name, plugin in UNIT_PLUGINS.items() if isinstance(plugin, PlanningCapability)}
VERIFICATION_PLUGINS = {
    name: plugin for name, plugin in UNIT_PLUGINS.items() if isinstance(plugin, VerificationCapability)
}


def semantic_reconciliation_result(plugin_name: str, result: object) -> ReconciliationResult:
    try:
        plugin = RECONCILIATION_PLUGINS[plugin_name]
    except KeyError as exc:
        raise DriverError(f"unit plugin does not support reconciliation: {plugin_name}") from exc
    return plugin.semantic_result(result)
