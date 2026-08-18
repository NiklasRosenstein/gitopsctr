"""Structural registry validation for generic resource APIs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from gitopsctr.resource_api.api import GVK, ApiKind
from gitopsctr.resource_api.model import ApiKindMembership, ResourceAddressing, ResourceApiError, ResourceApiFamily


@dataclass(frozen=True)
class ResourceApiRelationship:
    """A named directed edge and its optional child-to-parent addressing role."""

    name: str
    source_family: str
    target_family: str
    address_child_family: str | None = None
    address_parent_family: str | None = None

    def __post_init__(self) -> None:
        if (self.address_child_family is None) != (self.address_parent_family is None):
            raise ResourceApiError(f"relationship {self.name!r} must define both addressing endpoints or neither")
        if self.address_child_family is not None and {
            self.address_child_family,
            self.address_parent_family,
        } != {self.source_family, self.target_family}:
            raise ResourceApiError(f"relationship {self.name!r} addressing endpoints must match its directed edge")


@dataclass(frozen=True)
class ResourceApiContribution:
    api_kinds: tuple[ApiKind[object], ...] = ()
    families: tuple[ResourceApiFamily, ...] = ()
    relationships: tuple[ResourceApiRelationship, ...] = ()


@dataclass(frozen=True)
class ResourceApiRegistry:
    api_kinds: Mapping[GVK, ApiKind[object]]
    families: tuple[ResourceApiFamily, ...]
    relationships: tuple[ResourceApiRelationship, ...] = ()
    _families_by_name: Mapping[str, ResourceApiFamily] = field(init=False, repr=False)
    _families_by_selector: Mapping[str, ResourceApiFamily] = field(init=False, repr=False)
    _family_by_gvk: Mapping[GVK, ResourceApiFamily] = field(init=False, repr=False)
    _relationships_by_name: Mapping[str, ResourceApiRelationship] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        families = self._unique(self.families, "family")
        selectors: dict[str, ResourceApiFamily] = {}
        for family in self.families:
            self._validate_name(family.name, "family")
            if not family.membership_rules or any(
                not isinstance(rule, ApiKindMembership) for rule in family.membership_rules
            ):
                raise ResourceApiError(f"resource family {family.name!r} has no executable API membership rule")
            if not isinstance(family.addressing, ResourceAddressing):
                raise ResourceApiError(f"resource family {family.name!r} has no executable address resolver")
            if family.identity.separator != "/":
                raise ResourceApiError(
                    f"resource family {family.name!r} identity must use the canonical '/' address separator"
                )
            sample = family.identity.build(tuple(f"sample-{segment.name}" for segment in family.identity.segments))
            if family.identity.parse(family.identity.render(sample)) != sample:
                raise ResourceApiError(f"resource family {family.name!r} identity does not round trip canonically")
            for selector in family.selectors:
                self._validate_name(selector, "resource selector")
                previous = selectors.get(selector)
                if previous is not None:
                    raise ResourceApiError(
                        f"duplicate resource selector {selector!r}: {previous.name!r} and {family.name!r}"
                    )
                selectors[selector] = family

        family_by_gvk: dict[GVK, ResourceApiFamily] = {}
        members_by_family = {name: 0 for name in families}
        for api_kind in self.api_kinds.values():
            matches = tuple(
                (family, rule) for family in self.families for rule in family.membership_rules if rule.matches(api_kind)
            )
            if len(matches) != 1:
                raise ResourceApiError(f"API kind {api_kind.gvk} matches {len(matches)} family membership rules")
            family = matches[0][0]
            family_by_gvk[api_kind.gvk] = family
            members_by_family[family.name] += 1
        for family, count in members_by_family.items():
            if count == 0:
                raise ResourceApiError(f"resource family {family!r} has no installed API kinds")

        relationships = self._unique(self.relationships, "relationship")
        for relationship in self.relationships:
            self._validate_name(relationship.name, "relationship")
            missing = {relationship.source_family, relationship.target_family} - set(families)
            if missing:
                description = (
                    f"an unknown family: {next(iter(missing))}"
                    if len(missing) == 1
                    else f"unknown families: {', '.join(sorted(missing))}"
                )
                raise ResourceApiError(f"relationship {relationship.name!r} references {description}")

        for family in self.families:
            missing_families = set(family.addressing.parent_families) - set(families)
            missing_relationships = set(family.addressing.relationships) - set(relationships)
            if missing_families:
                raise ResourceApiError(
                    f"resource family {family.name!r} addressing references unknown families: "
                    f"{', '.join(sorted(missing_families))}"
                )
            if missing_relationships:
                raise ResourceApiError(
                    f"resource family {family.name!r} addressing references unknown relationships: "
                    f"{', '.join(sorted(missing_relationships))}"
                )
            for relationship_name in family.addressing.relationships:
                relationship = relationships[relationship_name]
                if relationship.address_child_family != family.name:
                    raise ResourceApiError(
                        f"resource family {family.name!r} addressing relationship {relationship_name!r} "
                        "does not declare it as the child or mirror endpoint"
                    )
                if relationship.address_parent_family not in family.addressing.parent_families:
                    raise ResourceApiError(
                        f"resource family {family.name!r} addressing relationship {relationship_name!r} "
                        "does not connect it to a declared parent family"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ResourceApiError("resource addressing relationships must be acyclic")
            if name in visited:
                return
            visiting.add(name)
            for parent in families[name].addressing.parent_families:
                visit(parent)
            visiting.remove(name)
            visited.add(name)

        for name in families:
            visit(name)

        object.__setattr__(self, "api_kinds", MappingProxyType(dict(self.api_kinds)))
        object.__setattr__(self, "_families_by_name", MappingProxyType(families))
        object.__setattr__(self, "_families_by_selector", MappingProxyType(selectors))
        object.__setattr__(self, "_family_by_gvk", MappingProxyType(family_by_gvk))
        object.__setattr__(self, "_relationships_by_name", MappingProxyType(relationships))

    @staticmethod
    def _validate_name(value: str, description: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise ResourceApiError(f"invalid {description}: {value!r}")

    @staticmethod
    def _unique[T](values: tuple[T, ...], description: str) -> dict[str, T]:
        result: dict[str, T] = {}
        for value in values:
            name = getattr(value, "name", None)
            if not isinstance(name, str):
                raise ResourceApiError(f"{description} has no string name")
            if name in result:
                raise ResourceApiError(f"duplicate {description}: {name!r}")
            result[name] = value
        return result

    def family(self, selector: str) -> ResourceApiFamily:
        try:
            return self._families_by_name.get(selector) or self._families_by_selector[selector]
        except KeyError as exc:
            raise KeyError(f"unknown resource family: {selector!r}") from exc

    def family_for_api_kind(self, gvk: GVK) -> ResourceApiFamily:
        try:
            return self._family_by_gvk[gvk]
        except KeyError as exc:
            raise KeyError(f"API kind is not registered in a resource family: {gvk}") from exc

    def relationship(self, name: str) -> ResourceApiRelationship:
        try:
            return self._relationships_by_name[name]
        except KeyError as exc:
            raise KeyError(f"unknown relationship: {name!r}") from exc

    def api_kinds_for_family(self, family: str) -> tuple[ApiKind[object], ...]:
        definition = self.family(family)
        return tuple(
            sorted(
                (kind for kind in self.api_kinds.values() if self._family_by_gvk[kind.gvk] is definition),
                key=lambda item: str(item.gvk),
            )
        )


def build_resource_api_registry(
    api_kinds: Iterable[ApiKind[object]] | Mapping[GVK, ApiKind[object]],
    families: Iterable[ResourceApiFamily],
    relationships: Iterable[ResourceApiRelationship] = (),
    contributions: Iterable[ResourceApiContribution] = (),
) -> ResourceApiRegistry:
    """Merge caller registrations and contributions into one validated registry."""

    registrations = tuple(api_kinds.values() if isinstance(api_kinds, Mapping) else api_kinds)
    base_families = tuple(families)
    base_relationships = tuple(relationships)
    installed = tuple(contributions)
    merged_kinds = (*registrations, *(kind for contribution in installed for kind in contribution.api_kinds))
    kinds_by_gvk: dict[GVK, ApiKind[object]] = {}
    for api_kind in merged_kinds:
        if api_kind.gvk in kinds_by_gvk:
            raise ResourceApiError(f"duplicate API kind registration: {api_kind.gvk}")
        kinds_by_gvk[api_kind.gvk] = api_kind
    return ResourceApiRegistry(
        kinds_by_gvk,
        (*base_families, *(family for contribution in installed for family in contribution.families)),
        (
            *base_relationships,
            *(relationship for contribution in installed for relationship in contribution.relationships),
        ),
    )
