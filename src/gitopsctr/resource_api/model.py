"""Generic resource-family identity and qualified-address protocols."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from gitopsctr.resource_api.api import GVK, ApiKind
from gitopsctr.resource_api.document import JsonObject, JsonValue


class ResourceApiError(ValueError):
    """A resource API definition or persisted relationship is inconsistent."""


@dataclass(frozen=True)
class IdentitySegmentDefinition:
    name: str
    filter_option: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]*", self.name) is None:
            raise ResourceApiError(f"invalid resource identity segment {self.name!r}")
        if self.filter_option is not None and re.fullmatch(r"--[a-z][a-z0-9-]*", self.filter_option) is None:
            raise ResourceApiError(f"invalid resource identity filter option {self.filter_option!r}")

    @property
    def option_destination(self) -> str | None:
        return self.filter_option[2:].replace("-", "_") if self.filter_option is not None else None


@dataclass(frozen=True)
class LocalResourceIdentity:
    values: tuple[str, ...]


@dataclass(frozen=True)
class IdentityConstraint:
    segment: str
    values: frozenset[str]

    def __post_init__(self) -> None:
        if any(not value for value in self.values):
            raise ResourceApiError(f"identity constraint {self.segment!r} requires non-empty values")


@dataclass(frozen=True)
class ResourceSelection:
    exact: LocalResourceIdentity | None = None
    constraints: tuple[IdentityConstraint, ...] = ()

    @classmethod
    def segment(cls, name: str, values: frozenset[str]) -> ResourceSelection:
        return cls(constraints=(IdentityConstraint(name, values),))

    def values_for(self, segment: str) -> frozenset[str] | None:
        values = tuple(item.values for item in self.constraints if item.segment == segment)
        if len(values) > 1:
            raise ResourceApiError(f"resource selection repeats identity segment {segment!r}")
        return values[0] if values else None


@dataclass(frozen=True)
class ResourceIdentityDefinition:
    segments: tuple[IdentitySegmentDefinition, ...] = (IdentitySegmentDefinition("name"),)
    separator: str = "/"

    def __post_init__(self) -> None:
        names = tuple(segment.name for segment in self.segments)
        if not names or len(set(names)) != len(names) or names[-1] != "name":
            raise ResourceApiError("resource identity segments must be unique, non-empty, and end with 'name'")
        if not self.separator:
            raise ResourceApiError("resource identity separator must not be empty")

    def build(self, values: tuple[str, ...]) -> LocalResourceIdentity:
        if len(values) != len(self.segments):
            raise ResourceApiError(f"resource identity requires {len(self.segments)} segments, received {len(values)}")
        if any(not value for value in values) or self.separator in values[-1]:
            raise ResourceApiError("resource identity values must be non-empty and the local name cannot nest")
        for value in values[:-1]:
            address_segments(value)
        return LocalResourceIdentity(values)

    def from_name(self, name: str, qualifiers: tuple[str, ...] = ()) -> LocalResourceIdentity:
        return self.build((*qualifiers, name))

    def parse(self, value: str) -> LocalResourceIdentity:
        return self.build(tuple(value.split(self.separator)))

    def render(self, identity: LocalResourceIdentity) -> str:
        return self.separator.join(self.build(identity.values).values)

    def value(self, identity: LocalResourceIdentity, segment: str) -> str:
        try:
            index = tuple(item.name for item in self.segments).index(segment)
        except ValueError as exc:
            raise ResourceApiError(f"resource identity has no segment {segment!r}") from exc
        return self.build(identity.values).values[index]

    def matches(self, identity: LocalResourceIdentity, selection: ResourceSelection | None) -> bool:
        identity = self.build(identity.values)
        if selection is None:
            return True
        if selection.exact is not None and identity != self.build(selection.exact.values):
            return False
        segment_names = {item.name for item in self.segments}
        for constraint in selection.constraints:
            if constraint.segment not in segment_names:
                raise ResourceApiError(f"resource identity has no segment {constraint.segment!r}")
            if self.value(identity, constraint.segment) not in constraint.values:
                return False
        return True


def address_segments(value: str) -> tuple[str, ...]:
    segments = tuple(value.split("/"))
    if not segments or any(not segment or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", segment) for segment in segments):
        raise ResourceApiError(f"invalid qualified resource name: {value!r}")
    return segments


@runtime_checkable
class InspectionRecord(Protocol):
    @property
    def document(self) -> JsonObject: ...

    @property
    def gvk(self) -> GVK: ...

    @property
    def name(self) -> str: ...

    @property
    def qualified_name(self) -> str: ...

    @property
    def content_id(self) -> str | None: ...

    @property
    def parsed(self) -> object: ...


@runtime_checkable
class ResourceAddressRuntime(Protocol):
    def relationship_sources(self, relationship: str, target: InspectionRecord) -> tuple[InspectionRecord, ...]: ...

    def resource_qualified_name(self, record: InspectionRecord) -> str: ...


@runtime_checkable
class ResourceAddressing(Protocol):
    @property
    def parent_families(self) -> tuple[str, ...]: ...

    @property
    def relationships(self) -> tuple[str, ...]: ...

    @property
    def requires_relationship_authentication(self) -> bool: ...

    def validate(self, value: str) -> None: ...

    def storage_selection(self, value: str, identity: ResourceIdentityDefinition) -> ResourceSelection | None: ...

    def storage_constraint(
        self, segment: str, value: str, identity: ResourceIdentityDefinition
    ) -> IdentityConstraint | None: ...

    def filter_value(self, qualified_name: str, segment: str, identity: ResourceIdentityDefinition) -> str: ...

    def documentation(self) -> str: ...

    def qualified_name(self, record: InspectionRecord, runtime: ResourceAddressRuntime) -> str: ...


@dataclass(frozen=True)
class RootResourceAddressing:
    parent_families: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()
    requires_relationship_authentication: bool = False

    def validate(self, value: str) -> None:
        if len(address_segments(value)) != 1:
            raise ResourceApiError("root resource addresses contain exactly one segment")

    def qualified_name(self, record: InspectionRecord, runtime: ResourceAddressRuntime) -> str:
        self.validate(record.name)
        return record.name

    def storage_selection(self, value: str, identity: ResourceIdentityDefinition) -> ResourceSelection:
        self.validate(value)
        return ResourceSelection(exact=identity.parse(value))

    def storage_constraint(self, segment: str, value: str, identity: ResourceIdentityDefinition) -> IdentityConstraint:
        if segment != "name":
            raise ResourceApiError(f"root resource addressing has no filter segment {segment!r}")
        self.validate(value)
        return IdentityConstraint(segment, frozenset((value,)))

    def filter_value(self, qualified_name: str, segment: str, identity: ResourceIdentityDefinition) -> str:
        if segment != "name":
            raise ResourceApiError(f"root resource addressing has no filter segment {segment!r}")
        self.validate(qualified_name)
        return qualified_name

    def documentation(self) -> str:
        return "`name` (root)"


@dataclass(frozen=True)
class JsonFieldPath:
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.parts or any(not part for part in self.parts):
            raise ResourceApiError("JSON field path must contain non-empty components")

    def get(self, document: JsonObject) -> JsonValue:
        value: JsonValue = document
        for part in self.parts:
            if not isinstance(value, dict) or part not in value:
                raise ResourceApiError(f"missing relationship field {self}")
            value = value[part]
        return value

    def get_optional(self, document: JsonObject) -> JsonValue | None:
        value: JsonValue = document
        for part in self.parts:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    def __str__(self) -> str:
        return "/" + "/".join(self.parts)


@dataclass(frozen=True)
class PersistedQualifiedNameAddressing:
    path: JsonFieldPath
    parent_family: str
    relationship: str
    append_local_name: bool = False
    requires_relationship_authentication: bool = True

    @property
    def parent_families(self) -> tuple[str, ...]:
        return (self.parent_family,)

    @property
    def relationships(self) -> tuple[str, ...]:
        return (self.relationship,)

    def validate(self, value: str) -> None:
        count = len(address_segments(value))
        minimum = 2 if self.append_local_name else 1
        if count < minimum:
            raise ResourceApiError(f"resource address requires at least {minimum} segment(s)")

    def qualified_name(self, record: InspectionRecord, runtime: ResourceAddressRuntime) -> str:
        parent = self.path.get(record.document)
        if not isinstance(parent, str):
            raise ResourceApiError(f"resource {record.name!r} has no persisted qualified parent name")
        value = f"{parent}/{record.name}" if self.append_local_name else parent
        self.validate(value)
        return value

    def storage_selection(self, value: str, identity: ResourceIdentityDefinition) -> ResourceSelection | None:
        self.validate(value)
        if not self.append_local_name and "/" not in value:
            return ResourceSelection(exact=identity.parse(value))
        return None

    def storage_constraint(
        self, segment: str, value: str, identity: ResourceIdentityDefinition
    ) -> IdentityConstraint | None:
        segment_names = tuple(item.name for item in identity.segments)
        if segment not in segment_names:
            raise ResourceApiError(f"resource addressing has no filter segment {segment!r}")
        address_segments(value)
        return IdentityConstraint(segment, frozenset((value,))) if "/" not in value else None

    def filter_value(self, qualified_name: str, segment: str, identity: ResourceIdentityDefinition) -> str:
        parts = address_segments(qualified_name)
        segment_names = tuple(item.name for item in identity.segments)
        if segment not in segment_names:
            raise ResourceApiError(f"resource addressing has no filter segment {segment!r}")
        if segment == "name":
            return parts[-1]
        if self.append_local_name and segment == segment_names[-2]:
            return "/".join(parts[:-1])
        raise ResourceApiError(f"resource addressing cannot derive filter segment {segment!r}")

    def documentation(self) -> str:
        if self.append_local_name:
            return f"`parent-qualified-name/name` (child via `{self.relationship}`)"
        return f"`subject-qualified-name` (mirror via `{self.relationship}`)"


@runtime_checkable
class ApiKindMembership(Protocol):
    def matches(self, api_kind: ApiKind[object]) -> bool: ...


@dataclass(frozen=True)
class ResourceApiFamily:
    name: str
    singular: str
    plural: str
    membership_rules: tuple[ApiKindMembership, ...]
    aliases: tuple[str, ...] = ()
    identity: ResourceIdentityDefinition = field(default_factory=ResourceIdentityDefinition)
    addressing: ResourceAddressing = field(default_factory=RootResourceAddressing)

    @property
    def selectors(self) -> tuple[str, ...]:
        return (self.singular, self.plural, *self.aliases)
