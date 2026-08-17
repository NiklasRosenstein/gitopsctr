"""Installed API kinds and the capability registries derived from them."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points

from gitopsctr.api import api_kinds
from gitopsctr.contracts import ArtifactDescriptor
from gitopsctr.driver import (
    DriverError,
    MaterializationCapability,
    PlanningCapability,
    ReconciliationCapability,
    ReconciliationResult,
    VerificationCapability,
    load_unit_drivers,
)
from gitopsctr.inspection_api import InspectionOutputApi
from gitopsctr.resource_model import ResourceModelContribution, ResourceModelError, build_resource_registry


def load_resource_model_contributions() -> tuple[ResourceModelContribution, ...]:
    """Load independently installable resource families and their supporting model definitions."""

    contributions: list[ResourceModelContribution] = []
    for entry_point in entry_points(group="gitopsctr.resource-models"):
        contribution = entry_point.load()
        if not isinstance(contribution, ResourceModelContribution):
            raise ResourceModelError(
                f"resource-model entry point {entry_point.name!r} did not load a ResourceModelContribution"
            )
        contributions.append(contribution)
    return tuple(contributions)


API_KINDS = api_kinds()
RESOURCE_REGISTRY = build_resource_registry(
    {gvk: api_kind for gvk, api_kind in API_KINDS.items() if not isinstance(api_kind.spec, InspectionOutputApi)},
    load_resource_model_contributions(),
)
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


def semantic_reconciliation_result(
    driver_name: str,
    result: object,
    artifacts: Mapping[str, ArtifactDescriptor] | None = None,
) -> ReconciliationResult:
    try:
        driver = RECONCILIATION_DRIVERS[driver_name]
    except KeyError as exc:
        raise DriverError(f"unit driver does not support reconciliation: {driver_name}") from exc
    semantic = dict(driver.semantic_result(result))
    if artifacts:
        semantic["artifacts"] = {
            name: {
                "apiVersion": descriptor.apiVersion,
                "kind": descriptor.kind,
                "digest": descriptor.digest,
            }
            for name, descriptor in artifacts.items()
        }
    return semantic
