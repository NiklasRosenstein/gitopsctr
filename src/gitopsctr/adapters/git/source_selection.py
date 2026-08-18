"""Git policy adapter for selecting exact retained Unit source workspaces."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.application.apply_compilers import (
    UnitSourceSelection,
    UnitSourceSelectionRequest,
)
from gitopsctr.application.apply_projection import SourceBindingRole

_SHA = re.compile(r"[0-9a-f]{40}$")


class GitUnitSourceSelectionError(ValueError):
    """No authenticated retained Git source satisfies the Unit policy."""


@dataclass(frozen=True, slots=True)
class GitUnitSourceSelector:
    """Choose workload evidence by exact Git revision and configured binding.

    ``unit_bindings`` is an explicit composition policy keyed by the storage
    qualified Unit name.  It prevents a same-revision source from a different
    workload binding being selected accidentally.
    """

    lineage: GitSourceLineageEncoder
    unit_bindings: Mapping[str, str]

    def select(self, request: UnitSourceSelectionRequest) -> UnitSourceSelection:
        # Every descriptor is capability-bearing issued evidence.  Validate
        # them before role/binding filtering so a tampered or foreign entry
        # can never be silently treated as merely unavailable.
        for descriptor in request.named:
            descriptor._validate()
        for plane in request.retained_sources:
            plane._validate()
        expected = request.source_request.get("revision")
        prior = request.prior_source.revision if request.prior_source is not None else None
        revision = expected if isinstance(expected, str) and _SHA.fullmatch(expected) else prior
        if revision is None or _SHA.fullmatch(revision) is None:
            raise GitUnitSourceSelectionError("Git Unit source selection requires an exact requested or prior revision")
        binding = self.unit_bindings.get(request.qualified_name)
        if binding is None:
            raise GitUnitSourceSelectionError(f"Git Unit {request.qualified_name!r} has no configured workload binding")
        matches: list[UnitSourceSelection] = []
        for descriptor in request.named:
            if descriptor.role is not SourceBindingRole.WORKLOAD or descriptor.binding_key != binding:
                continue
            for plane in request.retained_sources:
                if descriptor not in plane.descriptors:
                    continue
                resolved = self.lineage.encode(descriptor, plane)
                if resolved.revision == revision:
                    matches.append(UnitSourceSelection(descriptor, plane))
        if len(matches) != 1:
            raise GitUnitSourceSelectionError(
                "Git Unit source revision is unavailable or ambiguous in retained evidence"
            )
        return matches[0]
