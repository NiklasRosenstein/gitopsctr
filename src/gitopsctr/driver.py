"""Driver plugin contracts and entry-point discovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any


class DriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriverContext:
    source_root: Path
    source_revision: str
    source_path: str
    unit: dict[str, Any]
    inputs: dict[str, Any]
    dry: bool = False
    report: Path | None = None


Driver = Callable[[DriverContext], dict[str, Any]]


class VerificationStatus(StrEnum):
    CLEAN = "CLEAN"
    DRIFT = "DRIFT"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus


VerificationDriver = Callable[[DriverContext], VerificationResult]
SemanticResultSelector = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class DriverPlugin:
    """A versioned reconciliation driver discovered through package entry points."""

    version: int
    reconcile: Driver
    semantic_result: SemanticResultSelector
    verify: VerificationDriver | None = None


def load_driver_plugins() -> dict[str, DriverPlugin]:
    plugins: dict[str, DriverPlugin] = {}
    for entry_point in entry_points(group="gitopsctr.drivers"):
        if entry_point.name in plugins:
            raise DriverError(f"duplicate driver entry point: {entry_point.name}")
        plugin = entry_point.load()
        if not isinstance(plugin, DriverPlugin):
            raise DriverError(f"driver entry point {entry_point.name!r} did not load a DriverPlugin")
        if plugin.version < 1:
            raise DriverError(f"driver entry point {entry_point.name!r} has an invalid version")
        plugins[entry_point.name] = plugin
    return plugins


DRIVER_PLUGINS = load_driver_plugins()
DRIVER_VERSIONS = {name: plugin.version for name, plugin in DRIVER_PLUGINS.items()}
RECONCILIATION_DRIVERS = {name: plugin.reconcile for name, plugin in DRIVER_PLUGINS.items()}
VERIFICATION_DRIVERS = {name: plugin.verify for name, plugin in DRIVER_PLUGINS.items() if plugin.verify is not None}


def semantic_driver_result(driver: str, result: dict[str, Any]) -> dict[str, Any]:
    try:
        plugin = DRIVER_PLUGINS[driver]
    except KeyError as exc:
        raise DriverError(f"unsupported driver: {driver}") from exc
    return plugin.semantic_result(result)
