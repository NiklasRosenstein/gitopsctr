"""Path-free dependency orchestration over an immutable source workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from gitopsctr.application.dependencies import DependencyCommand, DependencyEntry, DependencyResult
from gitopsctr.application.model import SnapshotId
from gitopsctr.contracts import (
    StackSpec,
    StackTemplateInlineSpec,
    scope_stack_template_resources,
    stack_generated_unit_name,
)
from gitopsctr.dependencies import convergence_order, convergence_scope, dependency_graph
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.resource_api import JsonObject
from gitopsctr.resource_model import ResourcePlane, ResourceRegistry
from gitopsctr.resources import ResourceCatalog, UnitResource
from gitopsctr.templates import TemplateError, dump_template_value
from gitopsctr.workspace_inspection import WorkspacePlaneProvider
from gitopsctr.workspace_inventory import WorkspaceInventorySession


@dataclass(frozen=True, slots=True)
class _ConvergenceSpecifications:
    """Internal source-only graph inputs with canonical display addresses."""

    units: Mapping[str, UnitResource[Any]]
    dependencies: Mapping[str, tuple[str, ...]]
    qualified_names: Mapping[str, str]


def dependency_workspace_provider(
    planes: WorkspacePlaneProvider,
    registry: ResourceRegistry,
    catalog: ResourceCatalog,
    command: DependencyCommand,
    *,
    source_revision: str,
    source_snapshot: SnapshotId,
) -> DependencyResult:
    """Evaluate the graph from one preselected, immutable source workspace.

    The caller resolves the selector before entering this function.  This keeps
    the graph independent of Git, paths, mutable heads, and deployment state.
    """

    try:
        inventory = WorkspaceInventorySession(registry, planes)
    except Exception:
        planes.close()
        raise
    with inventory:
        selected_source = planes.source()
        if selected_source.workspace.is_mutable:
            raise OperationError("dependency source workspace must be immutable")
        # Resolve the namespace before reading any graph input.  This retains
        # the source Environment as the authority for the command rather than
        # treating an arbitrary directory as an environment.
        inventory.deployment_refs(command.environment)
        loaded = _load_convergence_specifications(inventory, catalog, command.environment)
        addresses = {qualified: concrete for concrete, qualified in loaded.qualified_names.items()}
        requested = _resolve_qualified_units(command.units, addresses)
        selection = convergence_scope(loaded.units, requested, command.depth, loaded.dependencies)
        graph = dependency_graph(loaded.units, selection.scope, loaded.dependencies)
        order = convergence_order(loaded.units, selection.scope, loaded.dependencies)

        def qualified(name: str) -> str:
            return loaded.qualified_names.get(name, name)

        return DependencyResult(
            command.environment,
            source_revision,
            source_snapshot,
            tuple(qualified(name) for name in selection.targets),
            tuple(
                DependencyEntry(qualified(name), tuple(qualified(value) for value in graph.dependencies[name]))
                for name in order
            ),
        )


def _resolve_qualified_units(selectors: tuple[str, ...], addresses: Mapping[str, str]) -> tuple[str, ...]:
    unknown = tuple(selector for selector in selectors if selector not in addresses)
    if unknown:
        available = ", ".join(sorted(addresses)) or "none"
        raise OperationError(f"unknown Unit qualified name(s): {', '.join(unknown)}; available Units: {available}")
    return tuple(addresses[selector] for selector in selectors)


def _load_convergence_specifications(
    inventory: WorkspaceInventorySession,
    catalog: ResourceCatalog,
    environment: str,
) -> _ConvergenceSpecifications:
    """Load authored Units and inline Stack projections without a filesystem."""

    source_units = inventory.resources("unit", environment=environment, plane=ResourcePlane.SOURCE)
    specifications: dict[str, UnitResource[Any]] = {}
    for record in source_units:
        qualified_name = record.qualified_name
        specifications[qualified_name] = catalog.parse_unit(
            record.document, profile="authored", expected_name=record.name
        )

    templates = inventory.resources("stacktemplate", plane=ResourcePlane.SOURCE)
    templates_by_name = {
        record.name: catalog.parse_stack_template(record.document, profile="authored", expected_name=record.name)
        for record in templates
    }
    stacks = inventory.resources("stack", environment=environment, plane=ResourcePlane.SOURCE)
    dependencies: dict[str, tuple[str, ...]] = {}
    qualified_names: dict[str, str] = {name: name for name in specifications}
    for record in stacks:
        stack = catalog.parse_stack(record.document, profile="authored", expected_name=record.name)
        if not isinstance(stack.spec, StackSpec):
            raise OperationError(f"source Stack {stack.name!r} has an invalid specification")
        template = templates_by_name.get(stack.spec.template)
        if template is None:
            raise OperationError(
                f"Stack {stack.name!r} references missing StackTemplate {stack.spec.template!r} in environment {environment!r}"
            )
        if not isinstance(template.spec, StackTemplateInlineSpec):
            raise OperationError(
                f"StackTemplate {template.name!r} is not inline and cannot be inspected from an immutable source workspace"
            )
        try:
            expanded = template.spec.expand(stack.spec.parameters)
        except (TemplateError, ValueError) as exc:
            raise OperationError(f"Stack {stack.name!r}: {exc}") from exc
        selected = set(stack.spec.units or (resource.name for resource in expanded))
        known = {resource.name for resource in expanded}
        unknown = sorted(selected - known)
        if unknown:
            raise OperationError(f"Stack {stack.name!r} selects unknown Unit templates: {', '.join(unknown)}")
        for resource in expanded:
            if resource.name in selected:
                omitted = sorted(set(resource.dependsOn) - selected)
                if omitted:
                    raise OperationError(
                        f"Stack {stack.name!r} selects {resource.name!r} but omits dependencies: {', '.join(omitted)}"
                    )
        scoped = scope_stack_template_resources(
            stack.name, tuple(resource for resource in expanded if resource.name in selected)
        )
        for resource in scoped:
            concrete = stack_generated_unit_name(stack.name, resource.name)
            if concrete in specifications:
                raise OperationError(f"generated Stack Unit {concrete!r} collides with a source Unit")
            document: JsonObject = {
                "apiVersion": resource.apiVersion,
                "kind": resource.kind,
                "metadata": {"name": resource.name},
                "spec": cast(JsonObjectValue, dump_template_value(resource.spec)),
            }
            specifications[concrete] = catalog.parse_unit(document, profile="authored", expected_name=resource.name)
            qualified_names[concrete] = concrete
            dependencies[concrete] = tuple(stack_generated_unit_name(stack.name, value) for value in resource.dependsOn)
    return _ConvergenceSpecifications(specifications, dependencies, qualified_names)
