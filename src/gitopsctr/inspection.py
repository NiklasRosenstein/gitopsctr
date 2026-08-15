"""Registry-driven read-only resource inspection and presentation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from gitopsctr.document import JsonObject
from gitopsctr.errors import OperationError
from gitopsctr.inventory import (
    InventoryRecord,
    InventorySession,
    evaluate_relationships,
)
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import (
    ResourceFamilyDefinition,
    ResourceModelError,
    ResourcePlane,
    ResourceScope,
)


@dataclass(frozen=True)
class InspectionResult:
    """A persisted resource and any relationship state used only for its table row."""

    record: InventoryRecord
    relationship: object | None = None


ALL_SELECTOR = "all"


def inspectable_selectors() -> tuple[str, ...]:
    """Return selectors from the semantic registry, not a CLI-owned kind list."""

    selectors = {
        selector for family in RESOURCE_REGISTRY.families if family.inspection for selector in family.selectors
    }
    selectors.add(ALL_SELECTOR)
    return tuple(sorted(selectors))


def _aggregate_families() -> tuple[ResourceFamilyDefinition, ...]:
    """Return registry-defined inspection families inside an Environment namespace."""

    return tuple(
        family
        for family in RESOURCE_REGISTRY.families
        if family.inspection is not None
        and next(item for item in family.placements if item.default_for_inspection).scope is ResourceScope.ENVIRONMENT
    )


def _environments(inventory: InventorySession) -> tuple[str, ...]:
    return tuple(record.name for record in inventory.resources(inventory.registry.namespace_family.name))


def _validate_options(args: argparse.Namespace, family: ResourceFamilyDefinition) -> None:
    all_environments = cast(bool, args.all_environments)
    environment = cast(str | None, args.environment)
    desired_override = args.desired_ref is not None or args.desired_revision is not None
    observed_override = args.observed_ref is not None or args.observed_revision is not None
    default_placement = next(item for item in family.placements if item.default_for_inspection)
    if default_placement.scope is ResourceScope.PROJECT:
        if environment is not None or all_environments:
            raise OperationError(f"{family.singular.title()} queries do not accept --environment or --all-environments")
        if desired_override or observed_override:
            raise OperationError(f"{family.singular.title()} queries do not accept deployment-ref overrides")
    else:
        if environment is None and not all_environments:
            raise OperationError(f"get {family.plural} requires --environment or --all-environments")
        if all_environments and (desired_override or observed_override):
            raise OperationError("deployment-ref overrides cannot be combined with --all-environments")
    plane = family.inspection.default_plane if family.inspection is not None else None
    if desired_override and plane is not ResourcePlane.DESIRED:
        raise OperationError(f"get {family.plural} does not accept desired-ref overrides")
    if observed_override and plane is not ResourcePlane.OBSERVED and family.name not in {"unit", "stack"}:
        raise OperationError(f"get {family.plural} does not accept observed-ref overrides")
    if args.artifact is not None or args.artifacts:
        view = family.inspection
        definition = (
            RESOURCE_REGISTRY.artifact_description(view.artifact_description)
            if view is not None and view.artifact_description is not None
            else None
        )
        if definition is None or definition.describer_family != family.name:
            raise OperationError("--artifact and --artifacts are available only for Receipt queries")
        if args.name is None:
            raise OperationError("Receipt artifact inspection requires a receipt name")


def _validate_all_options(args: argparse.Namespace) -> None:
    """Validate aggregate inspection without imposing one family's plane."""

    if args.name is not None:
        raise OperationError("get all does not accept a resource name")
    if args.artifact is not None or args.artifacts:
        raise OperationError("get all does not accept --artifact or --artifacts")
    if args.environment is None and not args.all_environments:
        raise OperationError("get all requires --environment or --all-environments")
    desired_override = args.desired_ref is not None or args.desired_revision is not None
    observed_override = args.observed_ref is not None or args.observed_revision is not None
    if args.all_environments and (desired_override or observed_override):
        raise OperationError("deployment-ref overrides cannot be combined with --all-environments")


def _records_for_environment(
    inventory: InventorySession,
    family: ResourceFamilyDefinition,
    environment: str,
    args: argparse.Namespace,
    *,
    evaluate: bool,
) -> tuple[InspectionResult, ...]:
    desired_ref, observed_ref = inventory.deployment_refs(environment)
    plane = family.inspection.default_plane if family.inspection is not None else None
    if plane is ResourcePlane.DESIRED:
        desired_ref = args.desired_ref or desired_ref
        records = inventory.resources(
            family.name,
            environment=environment,
            ref=desired_ref,
            revision=args.desired_revision,
            allow_missing_ref=args.desired_ref is None and args.desired_revision is None,
            names=frozenset((args.name,)) if args.name is not None else None,
        )
    elif plane is ResourcePlane.OBSERVED:
        observed_ref = args.observed_ref or observed_ref
        records = inventory.resources(
            family.name,
            environment=environment,
            ref=observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            names=frozenset((args.name,)) if args.name is not None else None,
        )
    else:
        records = inventory.resources(
            family.name,
            environment=environment,
            names=frozenset((args.name,)) if args.name is not None else None,
        )

    if evaluate and family.name in {"stack", "stacktemplate"}:
        allow_missing_observed_ref = args.observed_ref is None and args.observed_revision is None
        inventory.prepare_stack_inspection(
            records,
            observed_ref=args.observed_ref or observed_ref,
            observed_revision=args.observed_revision,
            allow_missing_observed_ref=allow_missing_observed_ref,
        )
    relationship_by_path = _relationship_states(inventory, family, records, environment, args) if evaluate else {}
    return tuple(InspectionResult(record, relationship_by_path.get(record.path)) for record in records)


def _relationship_states(
    inventory: InventorySession,
    family: ResourceFamilyDefinition,
    records: tuple[InventoryRecord, ...],
    environment: str,
    args: argparse.Namespace,
    *,
    resolve_artifacts: bool = False,
) -> dict[object, object]:
    view = family.inspection
    if view is None or view.observation is None:
        return {}
    observation = inventory.registry.observation(view.observation)
    desired_ref, observed_ref = inventory.deployment_refs(environment)
    desired_ref = args.desired_ref or desired_ref
    observed_ref = args.observed_ref or observed_ref
    names = frozenset(record.name for record in records)
    if family.name == observation.subject_family:
        units = records
        receipts = inventory.resources(
            observation.observer_family,
            environment=environment,
            plane=observation.observer_plane,
            ref=observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            names=names,
        )
    elif family.name == observation.observer_family:
        receipts = records
        subject_names = set()
        for receipt in receipts:
            try:
                subject_names.add(observation.binding.subject_identity(receipt.relationship_resource()).name)
            except ResourceModelError as exc:
                raise OperationError(f"environment {environment!r}, observed {receipt.path}: {exc}") from exc
        units = inventory.resources(
            observation.subject_family,
            environment=environment,
            plane=observation.subject_plane,
            ref=desired_ref,
            revision=args.desired_revision,
            allow_missing_ref=args.desired_ref is None and args.desired_revision is None,
            names=frozenset(subject_names),
        )
    else:
        raise OperationError(
            f"inspection relationship {observation.name!r} does not include resource family {family.name!r}"
        )

    artifacts: tuple[InventoryRecord, ...] = ()
    if resolve_artifacts and view.artifact_description is not None:
        description = inventory.registry.artifact_description(view.artifact_description)
        producer_names = frozenset(unit.name for unit in units)
        artifacts = inventory.resources(
            description.artifact_family,
            environment=environment,
            plane=description.artifact_plane,
            ref=observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            producer_names=producer_names,
        )
    evaluation = evaluate_relationships(
        inventory.registry,
        units,
        receipts,
        artifacts,
        resolve_artifacts=resolve_artifacts,
    )
    if family.name == observation.subject_family:
        return {value.unit.path: value for value in evaluation.units}
    return {value.receipt.path: value for value in evaluation.receipts}


def _select(
    inventory: InventorySession,
    family: ResourceFamilyDefinition,
    args: argparse.Namespace,
    *,
    evaluate: bool,
) -> tuple[InspectionResult, ...]:
    default_placement = next(item for item in family.placements if item.default_for_inspection)
    if default_placement.scope is ResourceScope.PROJECT:
        results = tuple(
            InspectionResult(record)
            for record in inventory.resources(
                family.name,
                names=frozenset((args.name,)) if args.name is not None else None,
            )
        )
    else:
        authored_environments = _environments(inventory)
        if args.all_environments:
            environments = authored_environments
        else:
            selected_environment = cast(str, args.environment)
            if selected_environment not in authored_environments:
                raise OperationError(f"no environment named {selected_environment!r}")
            environments = (selected_environment,)
        results = tuple(
            result
            for environment in environments
            for result in _records_for_environment(inventory, family, environment, args, evaluate=evaluate)
        )
    if args.name is not None:
        results = tuple(result for result in results if result.record.name == args.name)
        if not results:
            location = (
                " in any environment"
                if args.all_environments
                else (
                    "" if default_placement.scope is ResourceScope.PROJECT else f" in environment {args.environment!r}"
                )
            )
            raise OperationError(f"no {family.singular} named {args.name!r}{location}")
    return tuple(
        sorted(results, key=lambda item: (item.record.environment or "", item.record.name, str(item.record.gvk)))
    )


def _table_rows(
    family: ResourceFamilyDefinition,
    results: Sequence[InspectionResult],
    *,
    include_environment: bool,
    inventory: InventorySession,
) -> list[list[str]]:
    rows: list[list[str]] = []
    for result in results:
        assert family.inspection is not None
        try:
            values = list(family.inspection.presenter.row(result.record, result.relationship, inventory))
        except ResourceModelError as exc:
            raise OperationError(
                f"could not present {family.singular} {result.record.name!r} at {result.record.path}: {exc}"
            ) from exc
        if len(values) != len(family.inspection.columns):
            raise OperationError(
                f"resource family {family.name!r} presenter returned {len(values)} values for "
                f"{len(family.inspection.columns)} columns"
            )
        if include_environment:
            values.insert(0, result.record.environment or "-")
        rows.append(values)
    return rows


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        print("No resources found.")
        return
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())


def _envelope(results: Sequence[InspectionResult]) -> JsonObject:
    return {
        "apiVersion": "inspection.gitopsctr.io/v1",
        "kind": "ResourceList",
        "metadata": {},
        "items": [
            {
                "provenance": {
                    "environment": result.record.environment,
                    "plane": result.record.plane.value,
                    "ref": result.record.ref,
                    "revision": result.record.revision,
                    "path": result.record.path.as_posix(),
                },
                "document": result.record.document,
            }
            for result in results
        ],
    }


def _print_documents(results: Sequence[InspectionResult], output: str, *, force_list: bool = False) -> None:
    document: JsonObject = _envelope(results) if force_list or len(results) != 1 else results[0].record.document
    if output == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        print(yaml.safe_dump(document, sort_keys=False, default_flow_style=False), end="")


def _artifact_results(
    inventory: InventorySession,
    receipt_results: Sequence[InspectionResult],
    args: argparse.Namespace,
) -> tuple[InspectionResult, ...]:
    selected: list[InspectionResult] = []
    for receipt_result in receipt_results:
        environment = cast(str, receipt_result.record.environment)
        observation = getattr(receipt_result.relationship, "observation", None)
        if getattr(observation, "value", None) != "CURRENT":
            state = getattr(observation, "value", "UNKNOWN")
            raise OperationError(
                f"Receipt {receipt_result.record.name!r} is {state}; its Artifacts cannot be authenticated without "
                "the exact historical desired producer"
            )
        relationships = _relationship_states(
            inventory,
            receipt_result.record.family,
            (receipt_result.record,),
            environment,
            args,
            resolve_artifacts=True,
        )
        current = relationships.get(receipt_result.record.path)
        _desired_ref, observed_ref = inventory.deployment_refs(environment)
        assert receipt_result.record.family.inspection is not None
        artifacts = inventory.resources(
            inventory.registry.artifact_description(
                cast(str, receipt_result.record.family.inspection.artifact_description)
            ).artifact_family,
            environment=environment,
            plane=ResourcePlane.OBSERVED,
            ref=args.observed_ref or observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            producer_names=frozenset((receipt_result.record.name,)),
        )
        by_path = {artifact.path: artifact for artifact in artifacts}
        links = getattr(current, "artifacts", ())
        for link in links:
            if args.artifact is None or link.name == args.artifact:
                selected.append(InspectionResult(by_path[link.artifact.path]))
    if args.artifact is not None and not selected:
        raise OperationError(f"receipt {args.name!r} has no artifact named {args.artifact!r}")
    return tuple(selected)


def command_get(repository_root: Path, args: argparse.Namespace) -> None:
    """Inspect persisted resources through registry-defined collections and relationships."""

    if args.selector == ALL_SELECTOR:
        _validate_all_options(args)
        with InventorySession(repository_root, RESOURCE_REGISTRY) as inventory:
            selected: list[tuple[ResourceFamilyDefinition, tuple[InspectionResult, ...]]] = []
            for family in _aggregate_families():
                evaluate = args.output == "table" and (
                    bool(family.inspection and family.inspection.observation)
                    or family.name in {"stack", "stacktemplate"}
                )
                selected.append((family, _select(inventory, family, args, evaluate=evaluate)))
            if args.output == "table":
                populated = tuple((family, results) for family, results in selected if results)
                if not populated:
                    print("No resources found.")
                    return
                for index, (family, results) in enumerate(populated):
                    if index:
                        print()
                    print(family.plural.upper())
                    assert family.inspection is not None
                    headers = list(family.inspection.columns)
                    if args.all_environments:
                        headers.insert(0, "ENVIRONMENT")
                    rows = _table_rows(
                        family,
                        results,
                        include_environment=args.all_environments,
                        inventory=inventory,
                    )
                    _print_table(headers, rows)
            else:
                results = tuple(result for _family, family_results in selected for result in family_results)
                _print_documents(results, args.output, force_list=True)
        return

    try:
        family = RESOURCE_REGISTRY.family(args.selector)
    except KeyError as exc:
        raise OperationError(str(exc)) from exc
    if family.inspection is None:
        raise OperationError(f"resource family {family.name!r} has no inspection view")
    _validate_options(args, family)
    with InventorySession(repository_root, RESOURCE_REGISTRY) as inventory:
        wants_artifacts = args.artifact is not None or args.artifacts
        table = args.output == "table" and not wants_artifacts
        evaluate = (table or wants_artifacts) and (
            bool(family.inspection.observation) or family.name in {"stack", "stacktemplate"}
        )
        results = _select(inventory, family, args, evaluate=evaluate)
        if wants_artifacts:
            results = _artifact_results(inventory, results, args)
        if table:
            rows = _table_rows(
                family,
                results,
                include_environment=args.all_environments,
                inventory=inventory,
            )
            headers = list(family.inspection.columns)
            if args.all_environments and family is not inventory.registry.namespace_family:
                headers.insert(0, "ENVIRONMENT")
            _print_table(headers, rows)
        else:
            _print_documents(results, "yaml" if args.output == "table" else args.output)
