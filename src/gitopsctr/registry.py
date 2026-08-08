"""Installed API kinds and the capability registries derived from them."""

from __future__ import annotations

from collections.abc import Mapping

from gitopsctr.api import api_kinds
from gitopsctr.driver import (
    DriverError,
    MaterializationCapability,
    PlanningCapability,
    ReconciliationCapability,
    ReconciliationResult,
    VerificationCapability,
    load_unit_drivers,
)

API_KINDS = api_kinds()
UNIT_DRIVERS = load_unit_drivers(API_KINDS)
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
