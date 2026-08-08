"""Driver plugin contracts and entry-point discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type DriverResult = Mapping[str, object]


class DriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriverContext:
    source_root: Path
    source_revision: str
    source_path: str
    unit: JsonObject
    inputs: JsonObject
    dry: bool = False
    report: Path | None = None


class Driver(ABC):
    """A versioned reconciliation driver discovered through a package entry point."""

    version: ClassVar[int] = 0

    @abstractmethod
    def reconcile(self, context: DriverContext) -> DriverResult:
        """Converge one fully materialized unit and return receipt fields."""

    @abstractmethod
    def semantic_result(self, result: object) -> DriverResult:
        """Select the receipt fields that define the deployment result."""


class VerificationStatus(StrEnum):
    CLEAN = "CLEAN"
    DRIFT = "DRIFT"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus


SemanticResultSelector = Callable[[object], DriverResult]


class VerificationCapability(ABC):
    """Optional capability for read-only external-state verification."""

    @abstractmethod
    def verify(self, context: DriverContext) -> VerificationResult:
        """Compare external state with desired state without writing a receipt."""


def load_driver_plugins() -> dict[str, Driver]:
    plugins: dict[str, Driver] = {}
    for entry_point in entry_points(group="gitopsctr.drivers"):
        if entry_point.name in plugins:
            raise DriverError(f"duplicate driver entry point: {entry_point.name}")
        plugin = entry_point.load()
        if not isinstance(plugin, Driver):
            raise DriverError(f"driver entry point {entry_point.name!r} did not load a Driver")
        if plugin.version < 1:
            raise DriverError(f"driver entry point {entry_point.name!r} has an invalid version")
        plugins[entry_point.name] = plugin
    return plugins


DRIVER_PLUGINS = load_driver_plugins()
DRIVER_VERSIONS = {name: plugin.version for name, plugin in DRIVER_PLUGINS.items()}
RECONCILIATION_DRIVERS = {name: plugin.reconcile for name, plugin in DRIVER_PLUGINS.items()}
VERIFICATION_DRIVERS = {
    name: plugin.verify for name, plugin in DRIVER_PLUGINS.items() if isinstance(plugin, VerificationCapability)
}


def semantic_driver_result(driver: str, result: object) -> DriverResult:
    try:
        plugin = DRIVER_PLUGINS[driver]
    except KeyError as exc:
        raise DriverError(f"unsupported driver: {driver}") from exc
    return plugin.semantic_result(result)
