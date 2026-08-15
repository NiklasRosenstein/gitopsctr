"""Registry-driven read-only resource inspection and presentation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from gitopsctr.contracts import (
    INSPECTION_RESOURCE_LIST_CONTRACT,
    InspectionAddress,
    InspectionAuthentication,
    InspectionDetails,
    InspectionProvenance,
    InspectionResourceItem,
    InspectionResourceListDocument,
    InspectionResourceListMetadata,
)
from gitopsctr.document import JsonObject, JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.inventory import (
    InventoryRecord,
    InventorySession,
    evaluate_observation_relationship,
    evaluate_relationships,
)
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import (
    IdentityConstraint,
    IdentitySegmentDefinition,
    InspectionRelationshipRole,
    ResourceFamilyDefinition,
    ResourceModelError,
    ResourcePlane,
    ResourceScope,
    ResourceSelection,
    WideInspectionPresenter,
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


def identity_filter_options() -> tuple[IdentitySegmentDefinition, ...]:
    """Return the registry-declared family identity filters exposed by ``get``."""

    options: dict[str, IdentitySegmentDefinition] = {}
    for family in RESOURCE_REGISTRY.families:
        if family.inspection is None:
            continue
        for segment in family.identity.segments:
            if segment.filter_option is None:
                continue
            previous = options.get(segment.filter_option)
            if previous is not None and previous.name != segment.name:
                raise ResourceModelError(
                    f"identity filter {segment.filter_option!r} is registered for multiple identity segments"
                )
            options[segment.filter_option] = segment
    return tuple(options[key] for key in sorted(options))


def _aggregate_families() -> tuple[ResourceFamilyDefinition, ...]:
    """Return registry-defined inspection families inside an Environment namespace."""

    return tuple(
        family
        for family in RESOURCE_REGISTRY.families
        if family.inspection is not None
        and family.inspection.include_in_all
        and next(item for item in family.placements if item.default_for_inspection).scope is ResourceScope.ENVIRONMENT
    )


def _is_authenticated_artifact_family(family: ResourceFamilyDefinition) -> bool:
    view = family.inspection
    return view is not None and view.relationship_role is InspectionRelationshipRole.DESCRIBED_RESOURCE


def _inspection_planes(family: ResourceFamilyDefinition) -> frozenset[ResourcePlane]:
    """Planes used by a registry-defined view and its relationship traversal."""

    view = family.inspection
    if view is None:
        return frozenset()
    planes = {view.default_plane}
    if view.observation is not None:
        observation = RESOURCE_REGISTRY.observation(view.observation)
        planes.update((observation.observer_plane, observation.subject_plane))
    if view.artifact_description is not None:
        description = RESOURCE_REGISTRY.artifact_description(view.artifact_description)
        planes.update((description.describer_plane, description.artifact_plane, description.producer_plane))
    return frozenset(planes)


def _environments(inventory: InventorySession) -> tuple[str, ...]:
    return tuple(record.name for record in inventory.resources(inventory.registry.namespace_family.name))


def _selection(family: ResourceFamilyDefinition, args: argparse.Namespace) -> ResourceSelection | None:
    try:
        exact = family.identity.parse(args.name) if args.name is not None else None
    except ResourceModelError as exc:
        expected = family.identity.separator.join(segment.name.upper() for segment in family.identity.segments)
        raise OperationError(f"invalid {family.singular} name {args.name!r}; expected {expected}") from exc
    constraints = tuple(
        IdentityConstraint(segment.name, frozenset((value,)))
        for segment in family.identity.segments
        if segment.option_destination is not None
        and isinstance(value := getattr(args, segment.option_destination, None), str)
    )
    return ResourceSelection(exact, constraints) if exact is not None or constraints else None


def _names_selection(names: frozenset[str]) -> ResourceSelection:
    return ResourceSelection.segment("name", names)


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
    planes = _inspection_planes(family)
    if desired_override and ResourcePlane.DESIRED not in planes:
        raise OperationError(f"get {family.plural} does not accept desired-ref overrides")
    if observed_override and ResourcePlane.OBSERVED not in planes and family.name != "stack":
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
    family_options = {segment.filter_option for segment in family.identity.segments}
    for segment in identity_filter_options():
        if (
            getattr(args, cast(str, segment.option_destination), None) is not None
            and segment.filter_option not in family_options
        ):
            raise OperationError(f"{segment.filter_option} is not available for {family.plural}")


def _validate_all_options(args: argparse.Namespace) -> None:
    """Validate aggregate inspection without imposing one family's plane."""

    if args.name is not None:
        raise OperationError("get all does not accept a resource name")
    if args.artifact is not None or args.artifacts:
        raise OperationError("get all does not accept --artifact or --artifacts")
    for segment in identity_filter_options():
        if getattr(args, cast(str, segment.option_destination), None) is not None:
            raise OperationError(f"get all does not accept {segment.filter_option}")
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
    selection = _selection(family, args)
    if plane is ResourcePlane.DESIRED:
        desired_ref = args.desired_ref or desired_ref
        records = inventory.resources(
            family.name,
            environment=environment,
            ref=desired_ref,
            revision=args.desired_revision,
            allow_missing_ref=args.desired_ref is None and args.desired_revision is None,
            selection=selection,
        )
    elif plane is ResourcePlane.OBSERVED:
        observed_ref = args.observed_ref or observed_ref
        records = inventory.resources(
            family.name,
            environment=environment,
            ref=observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            selection=selection,
        )
    else:
        records = inventory.resources(
            family.name,
            environment=environment,
            selection=selection,
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
    description = (
        inventory.registry.artifact_description(view.artifact_description)
        if view.artifact_description is not None
        else None
    )
    desired_ref, observed_ref = inventory.deployment_refs(environment)
    desired_ref = args.desired_ref or desired_ref
    observed_ref = args.observed_ref or observed_ref

    def related_resources(
        related_family: str,
        plane: ResourcePlane,
        selection: ResourceSelection | None = None,
    ) -> tuple[InventoryRecord, ...]:
        definition = inventory.registry.family(related_family)
        placement = next(item for item in definition.placements if item.plane is plane)
        related_environment = environment if placement.scope is ResourceScope.ENVIRONMENT else None
        if plane is ResourcePlane.SOURCE:
            return inventory.resources(
                related_family,
                environment=related_environment,
                plane=plane,
                selection=selection,
            )
        if plane is ResourcePlane.DESIRED:
            ref, revision = desired_ref, args.desired_revision
            allow_missing_ref = args.desired_ref is None and args.desired_revision is None
        else:
            ref, revision = observed_ref, args.observed_revision
            allow_missing_ref = args.observed_ref is None and args.observed_revision is None
        return inventory.resources(
            related_family,
            environment=related_environment,
            plane=plane,
            ref=ref,
            revision=revision,
            allow_missing_ref=allow_missing_ref,
            selection=selection,
        )

    selected_artifact_producers: frozenset[str] | None = None
    if view.relationship_role is InspectionRelationshipRole.SUBJECT:
        units = records
        subject_identities = {record.identity for record in records}
        candidate_receipts = related_resources(
            observation.observer_family,
            observation.observer_plane,
        )
        try:
            receipts = tuple(
                receipt
                for receipt in candidate_receipts
                if observation.binding.subject_identity(receipt.relationship_resource()) in subject_identities
            )
        except ResourceModelError as exc:
            raise OperationError(f"environment {environment!r}: invalid observation relationship: {exc}") from exc
    elif view.relationship_role is InspectionRelationshipRole.OBSERVER:
        receipts = records
        subject_names = set()
        for receipt in receipts:
            try:
                subject_names.add(observation.binding.subject_identity(receipt.relationship_resource()).name)
            except ResourceModelError as exc:
                raise OperationError(f"environment {environment!r}, observed {receipt.path}: {exc}") from exc
        units = related_resources(
            observation.subject_family,
            observation.subject_plane,
            _names_selection(frozenset(subject_names)),
        )
    elif view.relationship_role is InspectionRelationshipRole.DESCRIBED_RESOURCE and description is not None:
        selected_artifact_producers = frozenset(
            family.identity.value(record.local_identity, description.producer_identity_segment) for record in records
        )
        candidate_receipts = related_resources(
            description.describer_family,
            description.describer_plane,
        )
        try:
            receipts = tuple(
                receipt
                for receipt in candidate_receipts
                if observation.binding.subject_identity(receipt.relationship_resource()).name
                in selected_artifact_producers
            )
            subject_names = {
                observation.binding.subject_identity(receipt.relationship_resource()).name for receipt in receipts
            }
        except ResourceModelError as exc:
            raise OperationError(f"environment {environment!r}: invalid observation relationship: {exc}") from exc
        units = related_resources(
            description.producer_family,
            description.producer_plane,
            _names_selection(frozenset(subject_names)),
        )
    else:
        raise OperationError(
            f"inspection relationship {observation.name!r} does not include resource family {family.name!r}"
        )

    artifacts: tuple[InventoryRecord, ...] = ()
    if description is not None and (
        resolve_artifacts or view.relationship_role is InspectionRelationshipRole.DESCRIBED_RESOURCE
    ):
        producer_names = selected_artifact_producers or frozenset(unit.name for unit in units)
        artifacts = related_resources(
            description.artifact_family,
            description.artifact_plane,
            ResourceSelection.segment(description.producer_identity_segment, producer_names),
        )
    if description is None:
        evaluation = evaluate_observation_relationship(observation, units, receipts)
        if view.relationship_role is InspectionRelationshipRole.SUBJECT:
            return {value.resource.path: value for value in evaluation.subjects}
        if view.relationship_role is InspectionRelationshipRole.OBSERVER:
            return {value.resource.path: value for value in evaluation.observers}
        raise OperationError(
            f"inspection relationship {observation.name!r} has no Artifact description for "
            f"resource family {family.name!r}"
        )

    evaluation = evaluate_relationships(
        inventory.registry,
        units,
        receipts,
        artifacts,
        resolve_artifacts=(
            resolve_artifacts or view.relationship_role is InspectionRelationshipRole.DESCRIBED_RESOURCE
        ),
        strict_artifacts=view.relationship_role is not InspectionRelationshipRole.DESCRIBED_RESOURCE,
        observation=observation,
        description=description,
    )
    if view.relationship_role is InspectionRelationshipRole.SUBJECT:
        return {value.unit.path: value for value in evaluation.units}
    if view.relationship_role is InspectionRelationshipRole.OBSERVER:
        return {value.receipt.path: value for value in evaluation.receipts}
    if view.relationship_role is InspectionRelationshipRole.DESCRIBED_RESOURCE:
        selected_paths = {record.path for record in records}
        return {value.artifact.path: value for value in evaluation.artifacts if value.artifact.path in selected_paths}
    return {}


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
                selection=_selection(family, args),
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
    if args.name is not None and not results:
        location = (
            " in any environment"
            if args.all_environments
            else ("" if default_placement.scope is ResourceScope.PROJECT else f" in environment {args.environment!r}")
        )
        raise OperationError(f"no {family.singular} named {args.name!r}{location}")
    return tuple(
        sorted(
            results, key=lambda item: (item.record.environment or "", item.record.qualified_name, str(item.record.gvk))
        )
    )


def _table_rows(
    family: ResourceFamilyDefinition,
    results: Sequence[InspectionResult],
    *,
    include_environment: bool,
    inventory: InventorySession,
    wide: bool = False,
) -> list[list[str]]:
    rows: list[list[str]] = []
    for result in results:
        assert family.inspection is not None
        try:
            if wide and family.inspection.wide_columns is not None:
                presenter = family.inspection.presenter
                if not isinstance(presenter, WideInspectionPresenter):
                    raise ResourceModelError(f"{family.name} has no wide inspection presenter")
                values = list(presenter.wide_row(result.record, result.relationship, inventory))
            else:
                values = list(family.inspection.presenter.row(result.record, result.relationship, inventory))
        except ResourceModelError as exc:
            raise OperationError(
                f"could not present {family.singular} {result.record.qualified_name!r} at {result.record.path}: {exc}"
            ) from exc
        columns = family.inspection.columns_for(wide=wide)
        if len(values) != len(columns):
            raise OperationError(
                f"resource family {family.name!r} presenter returned {len(values)} values for {len(columns)} columns"
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
    items: list[InspectionResourceItem] = []
    for result in results:
        authentication = getattr(result.relationship, "authentication", None)
        authentication_value = getattr(authentication, "value", None)
        items.append(
            InspectionResourceItem(
                provenance=InspectionProvenance(
                    environment=result.record.environment,
                    plane=result.record.plane.value,
                    ref=result.record.ref,
                    revision=result.record.revision,
                    path=result.record.path.as_posix(),
                ),
                address=InspectionAddress(
                    family=result.record.family.name,
                    scope=result.record.address.scope.value,
                    namespace=result.record.address.namespace,
                    qualifiedName=result.record.qualified_name,
                ),
                document=JsonObjectValue(result.record.document),
                inspection=(
                    InspectionDetails(authentication=cast(InspectionAuthentication, authentication_value))
                    if isinstance(authentication_value, str)
                    else None
                ),
            )
        )
    return INSPECTION_RESOURCE_LIST_CONTRACT.dump(
        InspectionResourceListDocument(
            apiVersion="inspection.gitopsctr.io/v1",
            kind="ResourceList",
            metadata=InspectionResourceListMetadata(),
            items=items,
        )
    )


def _print_documents(results: Sequence[InspectionResult], output: str, *, collection_result: bool) -> None:
    document = _envelope(results) if collection_result or len(results) != 1 else results[0].record.document
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
        description = inventory.registry.artifact_description(
            cast(str, receipt_result.record.family.inspection.artifact_description)
        )
        artifacts = inventory.resources(
            description.artifact_family,
            environment=environment,
            plane=ResourcePlane.OBSERVED,
            ref=args.observed_ref or observed_ref,
            revision=args.observed_revision,
            allow_missing_ref=args.observed_ref is None and args.observed_revision is None,
            selection=ResourceSelection.segment(
                description.producer_identity_segment,
                frozenset((receipt_result.record.name,)),
            ),
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

    if args.as_list and args.output in {"table", "wide"}:
        raise OperationError("--as-list requires --output yaml or json")

    if args.selector == ALL_SELECTOR:
        _validate_all_options(args)
        with InventorySession(repository_root, RESOURCE_REGISTRY) as inventory:
            selected: list[tuple[ResourceFamilyDefinition, tuple[InspectionResult, ...]]] = []
            table = args.output in {"table", "wide"}
            for family in _aggregate_families():
                evaluate = (
                    table
                    and (
                        bool(family.inspection and family.inspection.observation)
                        or family.name in {"stack", "stacktemplate"}
                    )
                ) or _is_authenticated_artifact_family(family)
                selected.append((family, _select(inventory, family, args, evaluate=evaluate)))
            if table:
                populated = tuple((family, results) for family, results in selected if results)
                if not populated:
                    print("No resources found.")
                    return
                for index, (family, results) in enumerate(populated):
                    if index:
                        print()
                    print(family.plural.upper())
                    assert family.inspection is not None
                    headers = list(family.inspection.columns_for(wide=args.output == "wide"))
                    if args.all_environments:
                        headers.insert(0, "ENVIRONMENT")
                    rows = _table_rows(
                        family,
                        results,
                        include_environment=args.all_environments,
                        inventory=inventory,
                        wide=args.output == "wide",
                    )
                    _print_table(headers, rows)
            else:
                results = tuple(result for _family, family_results in selected for result in family_results)
                _print_documents(results, args.output, collection_result=True)
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
        table_output = args.output in {"table", "wide"}
        table = table_output and not wants_artifacts
        evaluate = (
            (table or wants_artifacts)
            and (bool(family.inspection.observation) or family.name in {"stack", "stacktemplate"})
        ) or _is_authenticated_artifact_family(family)
        results = _select(inventory, family, args, evaluate=evaluate)
        if wants_artifacts:
            results = _artifact_results(inventory, results, args)
        if table:
            rows = _table_rows(
                family,
                results,
                include_environment=args.all_environments,
                inventory=inventory,
                wide=args.output == "wide",
            )
            headers = list(family.inspection.columns_for(wide=args.output == "wide"))
            if args.all_environments and family is not inventory.registry.namespace_family:
                headers.insert(0, "ENVIRONMENT")
            _print_table(headers, rows)
        else:
            _print_documents(
                results,
                "yaml" if table_output else args.output,
                collection_result=args.as_list or args.name is None or args.artifacts,
            )
