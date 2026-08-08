"""Typed dependency analysis for authored unit specifications."""

from __future__ import annotations

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


def _authored_document(unit: UnitResource) -> object:
    return unit.driver.unit_contract.dump(unit.spec)


def convergence_scope(
    specifications: Mapping[str, UnitResource],
    targets: Sequence[str] | None,
    max_depth: int | None = None,
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
        dependencies = observation_reference_units(_authored_document(specifications[unit_name]))
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


def convergence_order(specifications: Mapping[str, UnitResource], scope: Sequence[str]) -> tuple[str, ...]:
    included = set(scope)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(unit_name: str) -> None:
        if unit_name in visited or unit_name in visiting:
            return
        visiting.add(unit_name)
        for dependency in sorted(observation_reference_units(_authored_document(specifications[unit_name])) & included):
            visit(dependency)
        visiting.remove(unit_name)
        visited.add(unit_name)
        ordered.append(unit_name)

    for unit_name in sorted(scope):
        visit(unit_name)
    return tuple(ordered)


def dependency_graph(specifications: Mapping[str, UnitResource], scope: Sequence[str]) -> DependencyGraph:
    included = set(scope)
    return DependencyGraph(
        {
            unit_name: tuple(
                sorted(observation_reference_units(_authored_document(specifications[unit_name])) & included)
            )
            for unit_name in sorted(scope)
        }
    )


def downstream_unit_closure(specifications: Mapping[str, UnitResource], selected: Sequence[str]) -> tuple[str, ...]:
    consumers: dict[str, set[str]] = {unit: set() for unit in specifications}
    for consumer, specification in specifications.items():
        for producer in observation_reference_units(_authored_document(specification)):
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
