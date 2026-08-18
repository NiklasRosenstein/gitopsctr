"""CLI-facing inspection registry metadata and rendering.

Resource discovery and relationship orchestration live in the logical-workspace
service; this module deliberately retains only controller-facing presentation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import yaml

from gitopsctr.application.inspection import InspectionOutputFormat, InspectionTable, ResourceInspectionResult
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import IdentitySegmentDefinition, ResourceModelError

ALL_SELECTOR = "all"


def inspectable_selectors(registry=RESOURCE_REGISTRY) -> tuple[str, ...]:
    """Return selectors from the semantic registry, not a CLI-owned kind list."""

    selectors = {selector for family in registry.families if family.inspection for selector in family.selectors}
    selectors.add(ALL_SELECTOR)
    return tuple(sorted(selectors))


def identity_filter_options(registry=RESOURCE_REGISTRY) -> tuple[IdentitySegmentDefinition, ...]:
    """Return the registry-declared family identity filters exposed by ``get``."""

    options: dict[str, IdentitySegmentDefinition] = {}
    for family in registry.families:
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


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        print("No resources found.")
        return
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())


def _render_table(table: InspectionTable) -> None:
    if table.heading is not None:
        print(table.heading)
    _print_table(table.headers, table.rows)


def render_resource_inspection(result: ResourceInspectionResult, output: InspectionOutputFormat) -> None:
    """Render application-owned inspection data without performing inspection."""

    if result.tables:
        for index, table in enumerate(result.tables):
            if index:
                print()
            _render_table(table)
        return
    if result.document is None:
        print("No resources found.")
        return
    if output is InspectionOutputFormat.JSON:
        print(json.dumps(result.document, indent=2, sort_keys=False))
    else:
        print(yaml.safe_dump(result.document, sort_keys=False, default_flow_style=False), end="")
