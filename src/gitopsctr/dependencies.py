"""Typed dependency analysis for authored unit specifications."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from gitopsctr.errors import OperationError
from gitopsctr.resources import UnitResource
from gitopsctr.templates import (
    ArtifactReference,
    ReceiptReference,
    TemplateError,
    parse_template_value,
    references,
)


@dataclass(frozen=True)
class ConvergenceSelection:
    targets: tuple[str, ...]
    scope: tuple[str, ...]


@dataclass(frozen=True)
class DependencyGraph:
    dependencies: Mapping[str, tuple[str, ...]]

    def render_tree(self, target: str, style_name: Callable[[str], str] | None = None) -> tuple[str, ...]:
        render_name = style_name or (lambda name: name)
        lines = [render_name(target)]

        def render(unit_name: str, prefix: str, ancestors: set[str]) -> None:
            dependencies = self.dependencies[unit_name]
            for index, dependency in enumerate(dependencies):
                last = index == len(dependencies) - 1
                cycle = dependency in ancestors
                lines.append(
                    f"{prefix}{'└── ' if last else '├── '}{render_name(dependency)}{' [cycle]' if cycle else ''}"
                )
                if not cycle:
                    render(dependency, prefix + ("    " if last else "│   "), ancestors | {dependency})

        render(target, "", {target})
        return tuple(lines)


def observation_reference_units(value: object, pointer: str = "") -> frozenset[str]:
    try:
        expression = parse_template_value(value, pointer)
    except TemplateError as exc:
        raise OperationError(str(exc)) from exc
    return frozenset(
        reference.fromReceipt.unit if isinstance(reference, ReceiptReference) else reference.fromArtifact.unit
        for reference in references(expression)
        if isinstance(reference, (ReceiptReference, ArtifactReference))
    )


_UNIT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def _validated_unit_name(value: object, description: str) -> str:
    if not isinstance(value, str) or _UNIT_NAME_PATTERN.fullmatch(value) is None:
        raise OperationError(f"invalid {description} unit name: {value!r}")
    return value


def desired_observation_reference_units(unit: UnitResource) -> frozenset[str]:
    """Return authored and persisted observation producers for a desired Unit."""

    producers = set(observation_reference_units(unit.driver.desired_unit_contract.dump(unit.spec)))
    resolved_inputs = getattr(unit.spec, "resolvedInputs", None)
    if resolved_inputs is None:
        return frozenset(producers)
    for producer in resolved_inputs.receipts or {}:
        producers.add(_validated_unit_name(producer, "receipt producer"))
    for key in resolved_inputs.artifacts or {}:
        if not isinstance(key, str) or key.count("/") != 1:
            raise OperationError(f"invalid artifact dependency key: {key!r}")
        producer, artifact = key.split("/", 1)
        _validated_unit_name(producer, "artifact producer")
        _validated_unit_name(artifact, "artifact")
        producers.add(producer)
    return frozenset(producers)


def _authored_document(unit: UnitResource) -> object:
    return unit.driver.unit_contract.dump(unit.spec)


def _unit_dependencies(
    specifications: Mapping[str, UnitResource],
    unit_name: str,
    additional_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> frozenset[str]:
    """Return observation and controller dependency edges for one Unit.

    StackTemplate edges live in the desired Stack graph, not in the Unit spec.
    Pass them as a separate input. This keeps controller metadata out of
    authored Unit documents and lets one ordering algorithm handle both forms.
    """

    dependencies = set(observation_reference_units(_authored_document(specifications[unit_name])))
    if additional_dependencies is not None:
        dependencies.update(additional_dependencies.get(unit_name, ()))
    return frozenset(dependencies)


def convergence_scope(
    specifications: Mapping[str, UnitResource],
    targets: Sequence[str] | None,
    max_depth: int | None = None,
    additional_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> ConvergenceSelection:
    if max_depth is not None and max_depth < 0:
        raise OperationError("--depth must be zero or a positive integer")
    selected = sorted(set(targets or specifications))
    unknown = sorted(set(selected) - specifications.keys())
    if unknown:
        available = ", ".join(sorted(specifications))
        raise OperationError(f"unknown unit(s) {', '.join(unknown)}; available units: {available}")
    depths = {unit_name: 0 for unit_name in selected}
    pending = list(selected)
    while pending:
        unit_name = pending.pop()
        depth = depths[unit_name]
        dependencies = _unit_dependencies(specifications, unit_name, additional_dependencies)
        missing = sorted(dependencies - specifications.keys())
        if missing:
            raise OperationError(f"{unit_name} references unknown observation unit(s): {', '.join(missing)}")
        if max_depth is not None and depth >= max_depth:
            continue
        for dependency in sorted(dependencies):
            dependency_depth = depth + 1
            if dependency_depth < depths.get(dependency, dependency_depth + 1):
                depths[dependency] = dependency_depth
                pending.append(dependency)
    return ConvergenceSelection(tuple(selected), tuple(sorted(depths)))


def convergence_order(
    specifications: Mapping[str, UnitResource],
    scope: Sequence[str],
    additional_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    included = set(scope)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(unit_name: str) -> None:
        if unit_name in visited or unit_name in visiting:
            return
        visiting.add(unit_name)
        for dependency in sorted(_unit_dependencies(specifications, unit_name, additional_dependencies) & included):
            visit(dependency)
        visiting.remove(unit_name)
        visited.add(unit_name)
        ordered.append(unit_name)

    for unit_name in sorted(scope):
        visit(unit_name)
    return tuple(ordered)


def dependency_graph(
    specifications: Mapping[str, UnitResource],
    scope: Sequence[str],
    additional_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> DependencyGraph:
    included = set(scope)
    return DependencyGraph(
        {
            unit_name: tuple(sorted(_unit_dependencies(specifications, unit_name, additional_dependencies) & included))
            for unit_name in sorted(scope)
        }
    )


def downstream_unit_closure(
    specifications: Mapping[str, UnitResource],
    selected: Sequence[str],
    additional_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    consumers: dict[str, set[str]] = {unit: set() for unit in specifications}
    for consumer in specifications:
        for producer in _unit_dependencies(specifications, consumer, additional_dependencies):
            if producer not in specifications:
                raise OperationError(f"{consumer} references unknown observation unit {producer!r}")
            consumers[producer].add(consumer)
    closure: set[str] = set()
    pending = list(selected)
    while pending:
        producer = pending.pop()
        for consumer in consumers[producer]:
            if consumer not in closure and consumer not in selected:
                closure.add(consumer)
                pending.append(consumer)
    return tuple(sorted(closure))
