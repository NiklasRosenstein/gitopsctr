"""Library-neutral JSON document contracts and core document models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mashumaro.config import BaseConfig
from mashumaro.jsonschema import build_json_schema
from mashumaro.jsonschema.annotations import Pattern
from mashumaro.jsonschema.models import Context, JSONSchema, JSONSchemaInstanceType
from mashumaro.jsonschema.plugins import BasePlugin
from mashumaro.jsonschema.schema import Instance
from mashumaro.mixins.dict import DataClassDictMixin

from gitopsctr.document import (
    ContractError,
    DocumentContract,
    JsonObject,
    JsonObjectValue,
    JsonValue,
    ResolvedJsonObjectValue,
    TypedDocumentContract,
)
from gitopsctr.formats import CANDIDATE_REF_TEMPLATE_PATTERN
from gitopsctr.templates import (
    REFERENCE_KEYS,
    ArtifactReference,
    EnvironmentReference,
    ParameterTemplateObject,
    ProjectionObject,
    PromotionReference,
    ReceiptReference,
    TemplateError,
    TemplateObject,
    contains_parameter_expression,
    dump_template_value,
    resolve_parameter_value,
    validate_parameter_values,
)

SCHEMA_ROOT = "https://niklasrosenstein.github.io/gitopsctr/schemas"


class _ContractSchemaPlugin(BasePlugin):
    """Mark pass-through types for expansion after Mashumaro builds the enclosing schema."""

    def get_schema(
        self,
        instance: Instance,
        ctx: Context,
        schema: JSONSchema | None = None,
    ) -> JSONSchema | None:
        if instance.type is TemplateObject:
            return JSONSchema(title="__gitopsctr_template_object__")
        if instance.type is ProjectionObject:
            return JSONSchema(title="__gitopsctr_projection_object__")
        if instance.type is ParameterTemplateObject:
            return JSONSchema(title="__gitopsctr_parameter_template_object__")
        reference_type = {
            ReceiptReference: "fromReceipt",
            ArtifactReference: "fromArtifact",
            PromotionReference: "fromPromotion",
            EnvironmentReference: "fromEnvironment",
        }.get(instance.type)
        if reference_type is not None:
            return JSONSchema(title=f"__gitopsctr_reference_{reference_type}__")
        if instance.type is JsonObjectValue:
            return JSONSchema(type=JSONSchemaInstanceType.OBJECT)
        if instance.type is ResolvedJsonObjectValue:
            return JSONSchema(title="__gitopsctr_resolved_json_object__")
        return None


def _reference_target_schema(reference_type: str, value_ref: str = "#/$defs/TemplateValue") -> JsonObject:
    unit_description = (
        "Promoted source unit name; omit it to use the target unit name."
        if reference_type == "fromPromotion"
        else "Logical name of the unit that provides the referenced value."
    )
    pointer_descriptions = {
        "fromReceipt": "JSON Pointer relative to the producer's typed receipt result; empty selects the whole result.",
        "fromArtifact": "JSON Pointer relative to the complete artifact resource; empty selects the whole resource.",
        "fromPromotion": (
            "JSON Pointer relative to the promoted unit's public spec; omit it to use the containing field path, "
            "or set it to an empty string to select the whole spec."
        ),
        "fromEnvironment": "JSON Pointer relative to the target Environment resource.",
    }
    properties: JsonObject = {
        "unit": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9-]*$",
            "description": unit_description,
        },
        "pointer": {
            "type": "string",
            "pattern": "^(?:$|/(?:[^~]|~[01])*)$",
            "description": pointer_descriptions[reference_type],
        },
    }
    if reference_type != "fromEnvironment":
        properties["dryFallback"] = {
            "$ref": value_ref,
            "description": (
                "Type-correct speculative value used only during dry resolution when the reference is unavailable."
            ),
        }
    required = [] if reference_type in {"fromPromotion", "fromEnvironment"} else ["unit"]
    if reference_type not in {"fromPromotion", "fromEnvironment"}:
        properties["pointer"]["default"] = ""
    if reference_type == "fromArtifact":
        properties.update(
            {
                "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                "apiVersion": {"type": "string", "pattern": "^[^/]+/[^/]+$"},
                "kind": {"type": "string", "pattern": "^[A-Z][A-Za-z0-9]*$"},
            }
        )
        required.extend(("name", "apiVersion", "kind"))
    return cast(
        JsonObject,
        {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


def _reference_expression_schema(
    reference_type: str,
    value_ref: str = "#/$defs/TemplateValue",
) -> JsonObject:
    return {
        "type": "object",
        "properties": {reference_type: _reference_target_schema(reference_type, value_ref)},
        "required": [reference_type],
        "additionalProperties": False,
    }


def _template_definitions() -> JsonObject:
    variants: list[JsonObject] = []
    for key in sorted(REFERENCE_KEYS):
        variants.append(_reference_expression_schema(key))
    variants.append(
        {
            "type": "object",
            "properties": {
                "fromParameter": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "pattern": DESIRED_UID_PATTERN}},
                    "required": ["name"],
                    "additionalProperties": False,
                }
            },
            "required": ["fromParameter"],
            "additionalProperties": False,
        }
    )
    variants.extend(
        cast(
            list[JsonObject],
            [
                {"type": "null"},
                {"type": "boolean"},
                {"type": "number"},
                {"type": "string"},
                {"type": "array", "items": {"$ref": "#/$defs/TemplateValue"}},
                {
                    "type": "object",
                    "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                    "additionalProperties": {"$ref": "#/$defs/TemplateValue"},
                },
            ],
        )
    )
    return cast(JsonObject, {"TemplateValue": {"oneOf": variants}})


def _parameter_template_definitions() -> JsonObject:
    return cast(
        JsonObject,
        {
            "ParameterTemplateValue": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "fromParameter": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "pattern": DESIRED_UID_PATTERN,
                                    }
                                },
                                "required": ["name"],
                                "additionalProperties": False,
                            }
                        },
                        "required": ["fromParameter"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                    {"type": "boolean"},
                    {"type": "number"},
                    {"type": "string"},
                    {"type": "array", "items": {"$ref": "#/$defs/ParameterTemplateValue"}},
                    {
                        "type": "object",
                        "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                        "additionalProperties": {"$ref": "#/$defs/ParameterTemplateValue"},
                    },
                ]
            }
        },
    )


def _expand_special_schemas(schema: JsonObject) -> JsonObject:
    used_template = False
    used_projection = False
    used_parameter_template = False
    used_resolved_json = False

    def visit(value: JsonValue) -> JsonValue:
        nonlocal used_template
        nonlocal used_projection
        nonlocal used_parameter_template
        nonlocal used_resolved_json
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("title") == "__gitopsctr_template_object__":
            used_template = True
            return cast(
                JsonObject,
                {
                    "type": "object",
                    "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                    "additionalProperties": {"$ref": "#/$defs/TemplateValue"},
                },
            )
        if value.get("title") == "__gitopsctr_projection_object__":
            used_projection = True
            return cast(
                JsonObject,
                {
                    "type": "object",
                    "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                    "additionalProperties": {"$ref": "#/$defs/ProjectionValue"},
                },
            )
        if value.get("title") == "__gitopsctr_parameter_template_object__":
            used_parameter_template = True
            return cast(
                JsonObject,
                {
                    "type": "object",
                    "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                    "additionalProperties": {"$ref": "#/$defs/ParameterTemplateValue"},
                },
            )
        reference_marker = value.get("title")
        if isinstance(reference_marker, str) and reference_marker.startswith("__gitopsctr_reference_"):
            used_template = True
            reference_type = reference_marker.removeprefix("__gitopsctr_reference_").removesuffix("__")
            return _reference_expression_schema(reference_type)
        if value.get("title") == "__gitopsctr_resolved_json_object__":
            used_resolved_json = True
            return cast(
                JsonObject,
                {
                    "type": "object",
                    "propertyNames": {"not": {"enum": sorted(REFERENCE_KEYS)}},
                    "additionalProperties": {"$ref": "#/$defs/ResolvedJsonValue"},
                },
            )
        return {name: visit(item) for name, item in value.items()}

    expanded = cast(JsonObject, visit(schema))
    if used_template:
        definitions = cast(JsonObject, expanded.setdefault("$defs", {}))
        definitions.update(_template_definitions())
    if used_projection:
        definitions = cast(JsonObject, expanded.setdefault("$defs", {}))
        projection_variants: list[JsonObject] = [
            _reference_expression_schema(key, "#/$defs/ProjectionValue") for key in sorted(REFERENCE_KEYS)
        ]
        projection_variants.extend(
            cast(
                list[JsonObject],
                [
                    {"type": "null"},
                    {"type": "boolean"},
                    {"type": "number"},
                    {"type": "string"},
                    {"type": "array", "items": {"$ref": "#/$defs/ProjectionValue"}},
                    {
                        "type": "object",
                        "propertyNames": {"not": {"enum": sorted((*REFERENCE_KEYS, "fromParameter"))}},
                        "additionalProperties": {"$ref": "#/$defs/ProjectionValue"},
                    },
                ],
            )
        )
        definitions["ProjectionValue"] = cast(
            JsonValue,
            {"oneOf": projection_variants},
        )
    if used_parameter_template:
        definitions = cast(JsonObject, expanded.setdefault("$defs", {}))
        definitions.update(_parameter_template_definitions())
    if used_resolved_json:
        definitions = cast(JsonObject, expanded.setdefault("$defs", {}))
        definitions["ResolvedJsonValue"] = cast(
            JsonValue,
            {
                "anyOf": [
                    {"type": "null"},
                    {"type": "boolean"},
                    {"type": "number"},
                    {"type": "string"},
                    {"type": "array", "items": {"$ref": "#/$defs/ResolvedJsonValue"}},
                    {
                        "type": "object",
                        "propertyNames": {"not": {"enum": sorted(REFERENCE_KEYS)}},
                        "additionalProperties": {"$ref": "#/$defs/ResolvedJsonValue"},
                    },
                ]
            },
        )
    return expanded


class StrictModel(DataClassDictMixin):
    class Config(BaseConfig):
        forbid_extra_keys = True
        allow_deserialization_not_by_alias = True
        serialize_by_alias = True


@dataclass(frozen=True, kw_only=True)
class SchemaDocument(StrictModel):
    schema_hint: str | None = field(default=None, metadata={"alias": "$schema"})


DESIRED_UID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
PARTITION_LABEL = "gitopsctr.io/partition"


def stack_generated_unit_name(stack_name: str, resource_name: str) -> str:
    """Return the deterministic desired Unit name for one Stack-local resource."""

    raw = f"{stack_name}--{resource_name}"
    if len(raw) <= 63:
        return raw
    digest = hashlib.sha256(f"gitopsctr/stack-unit-name/v1\0{stack_name}\0{resource_name}".encode()).hexdigest()[:10]
    return f"{raw[:52].rstrip('-')}-{digest}"


@dataclass(frozen=True, kw_only=True)
class DesiredOwnerReference(StrictModel):
    """A UID-fenced owner in the same desired-resource graph."""

    apiVersion: str
    kind: str
    name: str
    uid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]

    def __post_init__(self) -> None:
        if not self.apiVersion or not self.kind or not self.name or not re.fullmatch(DESIRED_UID_PATTERN, self.uid):
            raise ValueError("owner reference requires apiVersion, kind, name, and uid")


DELETION_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
CONTENT_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


@dataclass(frozen=True, kw_only=True)
class DeletionMetadata(StrictModel):
    """Deletion state for one desired resource."""

    generation: int
    resourceDigest: Annotated[str, Pattern(DELETION_DIGEST_PATTERN)]

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("deletion generation must be at least 1")
        if not re.fullmatch(DELETION_DIGEST_PATTERN, self.resourceDigest):
            raise ValueError("deletion resourceDigest must use sha256 and 64 lowercase hex characters")


@dataclass(frozen=True, kw_only=True)
class DesiredResourceMetadata(StrictModel):
    """Canonical desired-resource metadata for one immutable incarnation."""

    name: str
    uid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    labels: dict[str, str] | None = None
    ownerReferences: list[DesiredOwnerReference] | None = None
    deletion: DeletionMetadata | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("desired resource metadata.name must not be empty")
        if not re.fullmatch(DESIRED_UID_PATTERN, self.uid):
            raise ValueError("desired resource metadata.uid has an invalid format")
        if self.ownerReferences is not None and len(self.ownerReferences) != 1:
            raise ValueError("desired resource metadata.ownerReferences must contain exactly one reference")
        if self.labels is not None and any(not key for key in self.labels):
            raise ValueError("desired resource metadata label keys must not be empty")
        partition = self.labels.get(PARTITION_LABEL) if self.labels is not None else None
        if partition is not None and not re.fullmatch(DESIRED_UID_PATTERN, partition):
            raise ValueError("desired resource metadata partition label has an invalid format")
        if partition is not None and self.ownerReferences is not None:
            raise ValueError("owned desired resources must not carry the partition label")


@dataclass(frozen=True, kw_only=True)
class AuthoredResourceMetadata(StrictModel):
    """Source-authored metadata for a Stack or StackTemplate."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("authored resource metadata.name must not be empty")


ParameterType = Literal["string", "integer", "number", "boolean", "object", "array"]


@dataclass(frozen=True, kw_only=True)
class ParameterDeclaration(StrictModel):
    """A required, typed StackTemplate parameter."""

    name: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    type: ParameterType

    def __post_init__(self) -> None:
        if not re.fullmatch(DESIRED_UID_PATTERN, self.name):
            raise ValueError(f"invalid parameter name: {self.name!r}")


@dataclass(frozen=True, kw_only=True)
class StackTemplateUnitTemplate(StrictModel):
    """One logical Unit template in a StackTemplate."""

    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    spec: TemplateObject
    dependsOn: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[^/]+/[^/]+$", self.apiVersion) is None:
            raise ValueError(f"invalid Unit template apiVersion: {self.apiVersion!r}")
        if re.fullmatch(r"^[A-Z][A-Za-z0-9]*$", self.kind) is None:
            raise ValueError(f"invalid Unit template kind: {self.kind!r}")
        if any(re.fullmatch(DESIRED_UID_PATTERN, dependency) is None for dependency in self.dependsOn):
            raise ValueError("Unit template dependencies must use desired resource names")
        if len(set(self.dependsOn)) != len(self.dependsOn):
            raise ValueError("Unit template has duplicate dependencies")


@dataclass(frozen=True, kw_only=True)
class StackTemplateResource(StrictModel):
    """One expanded, named Unit template used internally by Stack projection."""

    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    name: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    spec: ParameterTemplateObject
    dependsOn: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[^/]+/[^/]+$", self.apiVersion) is None:
            raise ValueError(f"invalid template resource apiVersion: {self.apiVersion!r}")
        if re.fullmatch(r"^[A-Z][A-Za-z0-9]*$", self.kind) is None:
            raise ValueError(f"invalid template resource kind: {self.kind!r}")
        if not re.fullmatch(DESIRED_UID_PATTERN, self.name):
            raise ValueError(f"invalid template resource name: {self.name!r}")
        if any(re.fullmatch(DESIRED_UID_PATTERN, dependency) is None for dependency in self.dependsOn):
            raise ValueError(f"template resource {self.name!r} dependencies must use desired resource names")
        if len(set(self.dependsOn)) != len(self.dependsOn):
            raise ValueError(f"template resource {self.name!r} has duplicate dependencies")
        if self.name in self.dependsOn:
            raise ValueError(f"template resource {self.name!r} cannot depend on itself")

    def resolved(self, parameters: Mapping[str, JsonValue]) -> StackTemplateResource:
        resolved = resolve_parameter_value(self.spec, parameters)
        if not isinstance(resolved, dict):
            raise TemplateError(f"template resource {self.name!r} spec must resolve to an object")
        return StackTemplateResource(
            apiVersion=self.apiVersion,
            kind=self.kind,
            name=self.name,
            spec=ParameterTemplateObject(cast(Any, resolved)),
            dependsOn=list(self.dependsOn),
        )


def _scope_stack_template_value(value: object, names: Mapping[str, str]) -> object:
    if isinstance(value, ReceiptReference):
        target = value.fromReceipt
        return ReceiptReference(fromReceipt=replace(target, unit=names.get(target.unit, target.unit)))
    if isinstance(value, ArtifactReference):
        target = value.fromArtifact
        return ArtifactReference(fromArtifact=replace(target, unit=names.get(target.unit, target.unit)))
    if isinstance(value, PromotionReference):
        target = value.fromPromotion
        return PromotionReference(fromPromotion=replace(target, unit=names.get(target.unit, target.unit)))
    if isinstance(value, list):
        return [_scope_stack_template_value(item, names) for item in value]
    if isinstance(value, dict):
        return {key: _scope_stack_template_value(item, names) for key, item in value.items()}
    return value


def scope_stack_template_resources(
    stack_name: str,
    resources: tuple[StackTemplateResource, ...],
) -> tuple[StackTemplateResource, ...]:
    """Resolve one template expansion into names isolated to a concrete Stack."""

    names = {resource.name: stack_generated_unit_name(stack_name, resource.name) for resource in resources}
    return tuple(
        StackTemplateResource(
            apiVersion=resource.apiVersion,
            kind=resource.kind,
            name=names[resource.name],
            spec=ParameterTemplateObject(cast(Any, _scope_stack_template_value(resource.spec, names))),
            dependsOn=[names[dependency] for dependency in resource.dependsOn],
        )
        for resource in resources
    )


@dataclass(frozen=True, kw_only=True)
class StackTemplateSpec(StrictModel):
    """Authored inline StackTemplate content."""

    parameters: list[ParameterDeclaration]
    unitTemplates: dict[Annotated[str, Pattern(DESIRED_UID_PATTERN)], StackTemplateUnitTemplate]

    def __post_init__(self) -> None:
        if not self.unitTemplates:
            raise ValueError("StackTemplate requires unitTemplates")
        for name, template in self.unitTemplates.items():
            if not re.fullmatch(DESIRED_UID_PATTERN, name):
                raise ValueError(f"invalid Unit template name: {name!r}")
            if name in template.dependsOn:
                raise ValueError(f"template resource {name!r} cannot depend on itself")
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("StackTemplate parameter names must be unique")
        resource_names = [resource.name for resource in self.resources]
        if len(set(resource_names)) != len(resource_names):
            raise ValueError("StackTemplate resource names must be unique")
        known = set(resource_names)
        for resource in self.resources or []:
            missing = [name for name in resource.dependsOn if name not in known]
            if missing:
                raise ValueError(f"template resource {resource.name!r} has missing dependencies: {', '.join(missing)}")
        self._validate_acyclic()

    @property
    def resources(self) -> list[StackTemplateResource]:
        return [
            StackTemplateResource(
                apiVersion=template.apiVersion,
                kind=template.kind,
                name=name,
                spec=ParameterTemplateObject(cast(Any, template.spec)),
                dependsOn=list(template.dependsOn),
            )
            for name, template in self.unitTemplates.items()
        ]

    def _validate_acyclic(self) -> None:
        resources = {resource.name: resource for resource in self.resources}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("StackTemplate resource dependencies must be acyclic")
            if name in visited:
                return
            visiting.add(name)
            for dependency in resources[name].dependsOn:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for resource in self.resources:
            visit(resource.name)

    def expand(self, parameters: Mapping[str, object]) -> tuple[StackTemplateResource, ...]:
        """Resolve parameters in authored order, admitting only installed Unit kinds."""

        values = validate_parameter_values(cast(Any, self.parameters), parameters)
        from gitopsctr.registry import UNIT_DRIVERS

        for resource in self.resources:
            if resource.apiVersion != "unit.gitopsctr.io/v1" or resource.kind not in {
                driver.kind for driver in UNIT_DRIVERS.values()
            }:
                raise TemplateError(
                    f"StackTemplate resource {resource.name!r} must be an installed Unit kind, "
                    f"got {resource.apiVersion}/{resource.kind}"
                )
        return tuple(resource.resolved(values) for resource in self.resources)

    def normalized_content(self) -> JsonObject:
        """Return the semantic StackTemplate content, excluding acquisition metadata."""

        def serialize_template(template: StackTemplateUnitTemplate) -> JsonObject:
            spec = (
                template.spec if isinstance(template.spec, TemplateObject) else TemplateObject(cast(Any, template.spec))
            )
            return StackTemplateUnitTemplate(
                apiVersion=template.apiVersion,
                kind=template.kind,
                spec=spec,
                dependsOn=sorted(template.dependsOn),
            ).to_dict()

        return {
            "parameters": [parameter.to_dict() for parameter in sorted(self.parameters, key=lambda item: item.name)],
            "unitTemplates": {
                name: serialize_template(template) for name, template in sorted(self.unitTemplates.items())
            },
        }

    def semantic_content_digest(self) -> str:
        """Return the digest of the canonical parameter and Unit-template content."""

        encoded = json.dumps(self.normalized_content(), separators=(",", ":"), sort_keys=True).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, kw_only=True)
class StackTemplateContent(StrictModel):
    """Normalized desired StackTemplate content used for semantic identity."""

    parameters: list[ParameterDeclaration]
    unitTemplates: dict[Annotated[str, Pattern(DESIRED_UID_PATTERN)], StackTemplateUnitTemplate]

    @classmethod
    def from_spec(cls, spec: StackTemplateSpec) -> StackTemplateContent:
        return cls(parameters=list(spec.parameters), unitTemplates=dict(spec.unitTemplates))

    def as_spec(self) -> StackTemplateSpec:
        return StackTemplateSpec(parameters=list(self.parameters), unitTemplates=dict(self.unitTemplates))

    def semantic_content_digest(self) -> str:
        return self.as_spec().semantic_content_digest()


@dataclass(frozen=True, kw_only=True)
class StackTemplateFromInput(StrictModel):
    """Empty marker for the only supported StackTemplate acquisition mode."""


@dataclass(frozen=True, kw_only=True)
class StackTemplateAcquisition(StrictModel):
    """The single acquisition record for a directly applied inline template."""

    documentDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    fromInput: StackTemplateFromInput

    def __post_init__(self) -> None:
        if not isinstance(self.fromInput, StackTemplateFromInput):
            raise ValueError("StackTemplate acquisition requires fromInput")
        if not re.fullmatch(CONTENT_DIGEST_PATTERN, self.documentDigest):
            raise ValueError("StackTemplate acquisition documentDigest must be a SHA-256 digest")


@dataclass(frozen=True, kw_only=True)
class StackTemplateSourceContext(StrictModel):
    """Exact repository context retained for later projection of inline Unit sources."""

    revision: Annotated[str, Pattern(r"^[0-9a-f]{40}$")]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("StackTemplate sourceContext revision must be a full lowercase Git commit")


@dataclass(frozen=True, kw_only=True)
class StackProjectionIdentity(StrictModel):
    """Identity fence for one Stack's structural Unit projection."""

    stackUid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    templateUid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    templateContentDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    projectionContextDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    projectionDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]

    def __post_init__(self) -> None:
        if not re.fullmatch(DESIRED_UID_PATTERN, self.stackUid):
            raise ValueError("Stack projection identity requires a valid Stack UID")
        if not re.fullmatch(DESIRED_UID_PATTERN, self.templateUid):
            raise ValueError("Stack projection identity requires a valid StackTemplate UID")
        for name, value in (
            ("templateContentDigest", self.templateContentDigest),
            ("projectionContextDigest", self.projectionContextDigest),
            ("projectionDigest", self.projectionDigest),
        ):
            if not re.fullmatch(CONTENT_DIGEST_PATTERN, value):
                raise ValueError(f"Stack projection identity {name} must be a SHA-256 digest")


@dataclass(frozen=True, kw_only=True)
class StackProjectionUnit(StrictModel):
    """One typed Unit declaration in a Stack projection."""

    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    spec: ProjectionObject
    dependsOn: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]]

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[^/]+/[^/]+$", self.apiVersion) is None:
            raise ValueError(f"invalid Stack projection Unit apiVersion: {self.apiVersion!r}")
        if re.fullmatch(r"^[A-Z][A-Za-z0-9]*$", self.kind) is None:
            raise ValueError(f"invalid Stack projection Unit kind: {self.kind!r}")
        if any(re.fullmatch(DESIRED_UID_PATTERN, dependency) is None for dependency in self.dependsOn):
            raise ValueError("Stack projection Unit dependencies must use desired resource names")
        if len(set(self.dependsOn)) != len(self.dependsOn):
            raise ValueError("Stack projection Unit has duplicate dependencies")
        if _contains_unresolved_projection_expression(self.spec):
            raise ValueError("Stack projection Unit spec contains an unresolved fromParameter expression")


def _contains_unresolved_projection_expression(value: object) -> bool:
    return contains_parameter_expression(value)


@dataclass(frozen=True, kw_only=True)
class StackProjectionUnitBinding(StrictModel):
    """The concrete desired Unit authenticated by an active Stack projection."""

    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    name: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    uid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    desiredDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    dependsOn: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[^/]+/[^/]+$", self.apiVersion) is None:
            raise ValueError("Stack active projection Unit binding has an invalid apiVersion")
        if re.fullmatch(r"^[A-Z][A-Za-z0-9]*$", self.kind) is None:
            raise ValueError("Stack active projection Unit binding has an invalid kind")
        for name, value in (("name", self.name), ("uid", self.uid)):
            if not re.fullmatch(DESIRED_UID_PATTERN, value):
                raise ValueError(f"Stack active projection Unit binding has an invalid {name}")
        if not re.fullmatch(CONTENT_DIGEST_PATTERN, self.desiredDigest):
            raise ValueError("Stack active projection Unit binding desiredDigest must be a SHA-256 digest")
        if any(re.fullmatch(DESIRED_UID_PATTERN, dependency) is None for dependency in self.dependsOn):
            raise ValueError("Stack active projection Unit binding dependencies must use desired resource names")
        if len(set(self.dependsOn)) != len(self.dependsOn):
            raise ValueError("Stack active projection Unit binding has duplicate dependencies")


@dataclass(frozen=True, kw_only=True)
class StackActiveProjection(StrictModel):
    """Concrete desired Units currently executable for one structural projection."""

    sourceProjectionDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    projectionContextDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    units: dict[Annotated[str, Pattern(DESIRED_UID_PATTERN)], StackProjectionUnitBinding]
    projectionDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]

    def __post_init__(self) -> None:
        if not re.fullmatch(CONTENT_DIGEST_PATTERN, self.sourceProjectionDigest):
            raise ValueError("Stack active projection sourceProjectionDigest must be a SHA-256 digest")
        if not re.fullmatch(CONTENT_DIGEST_PATTERN, self.projectionContextDigest):
            raise ValueError("Stack active projection projectionContextDigest must be a SHA-256 digest")
        concrete_names = [binding.name for binding in self.units.values()]
        if len(set(concrete_names)) != len(concrete_names):
            raise ValueError("Stack active projection has duplicate concrete Unit names")
        concrete_set = set(concrete_names)
        by_name = {binding.name: binding for binding in self.units.values()}
        for binding in self.units.values():
            if binding.name in binding.dependsOn:
                raise ValueError(f"Stack active projection Unit {binding.name!r} cannot depend on itself")
            missing = sorted(set(binding.dependsOn) - concrete_set)
            if missing:
                raise ValueError(
                    f"Stack active projection Unit {binding.name!r} has unknown dependencies: {', '.join(missing)}"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("Stack active projection dependencies must be acyclic")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_name[name].dependsOn:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in concrete_names:
            visit(name)
        expected = self.compute_projection_digest(
            self.sourceProjectionDigest,
            self.projectionContextDigest,
            self.units,
        )
        if self.projectionDigest != expected:
            raise ValueError("Stack active projection projectionDigest does not match concrete Unit bindings")

    @staticmethod
    def compute_projection_digest(
        source_projection_digest: str,
        projection_context_digest: str,
        units: Mapping[str, StackProjectionUnitBinding],
    ) -> str:
        payload = {
            "sourceProjectionDigest": source_projection_digest,
            "projectionContextDigest": projection_context_digest,
            "units": {
                name: {
                    "apiVersion": unit.apiVersion,
                    "kind": unit.kind,
                    "name": unit.name,
                    "uid": unit.uid,
                    "desiredDigest": unit.desiredDigest,
                    "dependsOn": sorted(unit.dependsOn),
                }
                for name, unit in sorted(units.items())
            },
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @classmethod
    def build(
        cls,
        *,
        source_projection_digest: str,
        projection_context_digest: str,
        units: Mapping[str, StackProjectionUnitBinding],
    ) -> StackActiveProjection:
        return cls(
            sourceProjectionDigest=source_projection_digest,
            projectionContextDigest=projection_context_digest,
            units=dict(units),
            projectionDigest=cls.compute_projection_digest(
                source_projection_digest,
                projection_context_digest,
                units,
            ),
        )


@dataclass(frozen=True, kw_only=True)
class StackProjection(StrictModel):
    """The required, fully validated structural projection for one desired Stack."""

    identity: StackProjectionIdentity
    units: dict[Annotated[str, Pattern(DESIRED_UID_PATTERN)], StackProjectionUnit]

    def __post_init__(self) -> None:
        names = set(self.units)
        for name, unit in self.units.items():
            if name in unit.dependsOn:
                raise ValueError(f"Stack projection Unit {name!r} cannot depend on itself")
            missing = sorted(set(unit.dependsOn) - names)
            if missing:
                raise ValueError(f"Stack projection Unit {name!r} has unknown dependencies: {', '.join(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("Stack projection dependencies must be acyclic")
            if name in visited:
                return
            visiting.add(name)
            for dependency in self.units[name].dependsOn:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in self.units:
            visit(name)
        if self.identity.projectionDigest != self.compute_projection_digest(
            self.identity.stackUid,
            self.identity.templateUid,
            self.identity.templateContentDigest,
            self.identity.projectionContextDigest,
            self.units,
        ):
            raise ValueError("Stack projection identity projectionDigest does not match canonical units")

    @staticmethod
    def compute_projection_digest(
        stack_uid: str,
        template_uid: str,
        template_content_digest: str,
        projection_context_digest: str,
        units: Mapping[str, StackProjectionUnit],
    ) -> str:
        canonical_units = {
            name: {
                "apiVersion": unit.apiVersion,
                "kind": unit.kind,
                "spec": dump_template_value(unit.spec),
                "dependsOn": sorted(unit.dependsOn),
            }
            for name, unit in sorted(units.items())
        }
        payload = {
            "stackUid": stack_uid,
            "templateUid": template_uid,
            "templateContentDigest": template_content_digest,
            "projectionContextDigest": projection_context_digest,
            "units": canonical_units,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @classmethod
    def build(
        cls,
        *,
        stack_uid: str,
        template_uid: str,
        template_content_digest: str,
        units: Mapping[str, StackProjectionUnit],
        context_digest: str | None = None,
    ) -> StackProjection:
        if context_digest is None:
            # Keep the Python construction helper usable by old in-memory
            # callers while every persisted desired document carries the
            # explicit context fence.
            context_digest = f"sha256:{hashlib.sha256(template_content_digest.encode()).hexdigest()}"
        projection_digest = cls.compute_projection_digest(
            stack_uid,
            template_uid,
            template_content_digest,
            context_digest,
            units,
        )
        return cls(
            identity=StackProjectionIdentity(
                stackUid=stack_uid,
                templateUid=template_uid,
                templateContentDigest=template_content_digest,
                projectionContextDigest=context_digest,
                projectionDigest=projection_digest,
            ),
            units=dict(units),
        )


@dataclass(frozen=True, kw_only=True)
class PromotionStackReference(StrictModel):
    stack: Annotated[str, Pattern(DESIRED_UID_PATTERN)]


@dataclass(frozen=True, kw_only=True)
class DesiredStackTemplateSpec(StackTemplateSpec):
    """Desired inline StackTemplate content and its direct-input acquisition record."""

    contentDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]
    acquisition: StackTemplateAcquisition
    sourceContext: StackTemplateSourceContext | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.contentDigest != self.semantic_content_digest():
            raise ValueError(
                f"StackTemplate contentDigest {self.contentDigest!r} does not match "
                f"content {self.semantic_content_digest()!r}"
            )


@dataclass(frozen=True, kw_only=True)
class StackTemplateReference(StrictModel):
    name: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    uid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    contentDigest: Annotated[str, Pattern(CONTENT_DIGEST_PATTERN)]

    def __post_init__(self) -> None:
        if not re.fullmatch(DESIRED_UID_PATTERN, self.name):
            raise ValueError(f"invalid Stack template name: {self.name!r}")
        if not re.fullmatch(DESIRED_UID_PATTERN, self.uid):
            raise ValueError("StackTemplate reference UID must be supplied")
        if not re.fullmatch(CONTENT_DIGEST_PATTERN, self.contentDigest):
            raise ValueError("StackTemplate reference contentDigest must be a SHA-256 digest")

    @property
    def is_fenced(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class ArtifactImport(StrictModel):
    unit: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    name: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    fromPromotion: PromotionStackReference


@dataclass(frozen=True, kw_only=True)
class ResolvedArtifactImport(StrictModel):
    """Immutable lineage evidence for one promoted artifact import."""

    sourceStack: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    sourceStackUid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    sourceUnit: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    sourceUnitUid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    sourceDesiredRevision: Annotated[str, Pattern(r"^[0-9a-f]{40}$")]
    sourceObservedRevision: Annotated[str, Pattern(r"^[0-9a-f]{40}$")]
    receiptUnitBlob: Annotated[str, Pattern(r"^[0-9a-f]{40}$")]
    artifactName: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    apiVersion: Annotated[str, Pattern(r"^[^/]+/[^/]+$")]
    kind: Annotated[str, Pattern(r"^[A-Z][A-Za-z0-9]*$")]
    artifactDigest: Annotated[str, Pattern(r"^[a-z0-9]+:[0-9a-f]{64}$")]
    targetStackUid: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    artifactDocument: JsonObjectValue


@dataclass(frozen=True, kw_only=True)
class StackSpec(StrictModel):
    """Source-authored Stack template selection and parameter values."""

    template: Annotated[str, Pattern(DESIRED_UID_PATTERN)]
    parameters: JsonObjectValue = field(default_factory=JsonObjectValue)
    units: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]] | None = None
    artifactImports: list[ArtifactImport] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not re.fullmatch(DESIRED_UID_PATTERN, self.template):
            raise ValueError(f"invalid Stack template name: {self.template!r}")
        if self.units is not None:
            if not self.units or len(set(self.units)) != len(self.units):
                raise ValueError("Stack units must be non-empty and unique")
        if len({(item.unit, item.name) for item in self.artifactImports}) != len(self.artifactImports):
            raise ValueError("Stack artifactImports must be unique")


@dataclass(frozen=True, kw_only=True)
class DesiredStackSpec(StrictModel):
    """Self-contained desired Stack selection and structural projection."""

    templateRef: StackTemplateReference
    parameters: JsonObjectValue = field(default_factory=JsonObjectValue)
    units: list[Annotated[str, Pattern(DESIRED_UID_PATTERN)]] | None = None
    artifactImports: list[ArtifactImport] = field(default_factory=list)
    resolvedArtifactImports: dict[str, ResolvedArtifactImport] | None = None
    structuralProjection: StackProjection
    activeProjection: StackActiveProjection | None = None

    def __post_init__(self) -> None:
        if self.units is not None and (not self.units or len(set(self.units)) != len(self.units)):
            raise ValueError("Stack units must be non-empty and unique")
        if len({(item.unit, item.name) for item in self.artifactImports}) != len(self.artifactImports):
            raise ValueError("Stack artifactImports must be unique")
        if self.templateRef.uid != self.structuralProjection.identity.templateUid:
            raise ValueError("Stack templateRef UID must equal structural projection template UID")
        if self.templateRef.contentDigest != self.structuralProjection.identity.templateContentDigest:
            raise ValueError("Stack templateRef contentDigest must equal structural projection content digest")
        active = self.activeProjection
        if active is not None and active.sourceProjectionDigest == self.structuralProjection.identity.projectionDigest:
            for logical_name, binding in active.units.items():
                structural = self.structuralProjection.units.get(logical_name)
                if structural is None:
                    raise ValueError(f"Stack active projection has unknown structural Unit template: {logical_name!r}")
                expected_dependencies: list[str] = []
                for dependency in structural.dependsOn:
                    dependency_binding = active.units.get(dependency)
                    if dependency_binding is None:
                        raise ValueError(
                            f"Stack active projection Unit {logical_name!r} is missing structural dependency "
                            f"{dependency!r}"
                        )
                    expected_dependencies.append(dependency_binding.name)
                if sorted(binding.dependsOn) != sorted(expected_dependencies):
                    raise ValueError(
                        f"Stack active projection dependencies do not match structural topology for {logical_name!r}"
                    )


@dataclass(frozen=True, kw_only=True)
class StackTemplateDocument(SchemaDocument):
    apiVersion: Literal["gitopsctr.io/v1"]
    kind: Literal["StackTemplate"]
    metadata: AuthoredResourceMetadata
    spec: StackTemplateSpec

    def __post_init__(self) -> None:
        if self.apiVersion != "gitopsctr.io/v1" or self.kind != "StackTemplate":
            raise ValueError("StackTemplate document has an invalid apiVersion/kind")


@dataclass(frozen=True, kw_only=True)
class DesiredStackTemplateDocument(SchemaDocument):
    apiVersion: Literal["gitopsctr.io/v1"]
    kind: Literal["StackTemplate"]
    metadata: DesiredResourceMetadata
    spec: DesiredStackTemplateSpec

    def __post_init__(self) -> None:
        if self.apiVersion != "gitopsctr.io/v1" or self.kind != "StackTemplate":
            raise ValueError("desired StackTemplate document has an invalid apiVersion/kind")


@dataclass(frozen=True, kw_only=True)
class StackDocument(SchemaDocument):
    apiVersion: Literal["gitopsctr.io/v1"]
    kind: Literal["Stack"]
    metadata: AuthoredResourceMetadata
    spec: StackSpec

    def __post_init__(self) -> None:
        if self.apiVersion != "gitopsctr.io/v1" or self.kind != "Stack":
            raise ValueError("Stack document has an invalid apiVersion/kind")


@dataclass(frozen=True, kw_only=True)
class DesiredStackDocument(SchemaDocument):
    apiVersion: Literal["gitopsctr.io/v1"]
    kind: Literal["Stack"]
    metadata: DesiredResourceMetadata
    spec: DesiredStackSpec

    def __post_init__(self) -> None:
        if self.apiVersion != "gitopsctr.io/v1" or self.kind != "Stack":
            raise ValueError("desired Stack document has an invalid apiVersion/kind")
        if metadata_uid := self.metadata.uid:
            if metadata_uid != self.spec.structuralProjection.identity.stackUid:
                raise ValueError("desired Stack metadata.uid must equal structural projection stackUid")
        else:
            raise ValueError("desired Stack metadata.uid is required")


@dataclass(frozen=True, kw_only=True)
class EmptyResultModel(StrictModel):
    pass


@dataclass(frozen=True)
class MashumaroContract[ModelT: StrictModel](TypedDocumentContract[ModelT]):
    model: type[ModelT]
    schema_id: str

    def json_schema(self) -> JsonObject:
        schema = cast(
            JsonObject,
            build_json_schema(
                self.model,
                with_dialect_uri=True,
                plugins=(_ContractSchemaPlugin(),),
            ).to_dict(),
        )
        schema = _expand_special_schemas(schema)
        schema["$id"] = self.schema_id
        return schema

    def _candidate(self, document: object) -> JsonObject:
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            raise ContractError("expected a JSON object")
        candidate = cast(JsonObject, dict(document))
        # $schema is only a transport hint. Its value never selects a validator or triggers IO.
        schema = self.json_schema()
        if "$schema" in cast(dict[str, Any], schema.get("properties", {})):
            candidate["$schema"] = None
        else:
            candidate.pop("$schema", None)
        return candidate

    def parse(self, document: object) -> ModelT:
        candidate = self._candidate(document)
        try:
            Draft202012Validator(self.json_schema()).validate(candidate)
            return self.model.from_dict(candidate)
        except (ValidationError, TypeError, ValueError) as exc:
            detail = exc.message if isinstance(exc, ValidationError) else str(exc)
            raise ContractError(detail) from exc

    def dump(self, value: ModelT) -> JsonObject:
        document = cast(JsonObject, value.to_dict())
        if document.get("$schema") is None:
            document.pop("$schema", None)
        return document

    def validate(self, document: object) -> JsonObject:
        self.parse(document)
        return cast(JsonObject, document)


@dataclass(frozen=True)
class MashumaroUnionContract[ModelT: StrictModel](TypedDocumentContract[ModelT]):
    models: tuple[type[ModelT], ...]
    schema_id: str
    title: str

    def json_schema(self) -> JsonObject:
        variants: list[JsonObject] = []
        for model in self.models:
            schema = cast(JsonObject, build_json_schema(model).to_dict())
            schema.pop("title", None)
            variants.append(schema)
        return cast(
            JsonObject,
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": self.schema_id,
                "title": self.title,
                "oneOf": variants,
            },
        )

    def parse(self, document: object) -> ModelT:
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            raise ContractError("expected a JSON object")
        candidate = dict(document)
        try:
            Draft202012Validator(self.json_schema()).validate(candidate)
        except ValidationError as exc:
            raise ContractError(exc.message) from exc
        errors: list[Exception] = []
        for model in self.models:
            try:
                return model.from_dict(candidate)
            except (LookupError, TypeError, ValueError) as exc:
                errors.append(exc)
        raise ContractError(str(errors[-1]) if errors else "document does not match a union variant")

    def dump(self, value: ModelT) -> JsonObject:
        document = cast(JsonObject, value.to_dict())
        if document.get("$schema") is None:
            document.pop("$schema", None)
        return document

    def validate(self, document: object) -> JsonObject:
        self.parse(document)
        return cast(JsonObject, document)


@dataclass(frozen=True, kw_only=True)
class EnvironmentRefs(StrictModel):
    desired: str | None = None
    observed: str | None = None
    candidate: Annotated[str, Pattern(CANDIDATE_REF_TEMPLATE_PATTERN)] | None = field(
        default=None,
        metadata={
            "description": (
                "Candidate ref template for reviewed promotions and rollbacks. Must contain {environment}; "
                "may also contain {id} and {operation}."
            )
        },
    )


@dataclass(frozen=True, kw_only=True)
class EnvironmentPromotion(StrictModel):
    allowedSources: list[str]


@dataclass(frozen=True, kw_only=True)
class PromotionPolicy(StrictModel):
    minimumEvidence: Literal["reconciled", "materialized"] = "reconciled"


@dataclass(frozen=True, kw_only=True)
class EnvironmentDocument(SchemaDocument):
    name: str
    changeGate: Literal["pullRequest", "none"] = "none"
    refs: EnvironmentRefs | None = None
    promotion: EnvironmentPromotion | None = None
    promotionPolicy: PromotionPolicy | None = None


@dataclass(frozen=True, kw_only=True)
class PromotionSource(StrictModel):
    environment: str
    desiredRef: str
    desiredRevision: str
    observedRef: str
    observedRevision: str | None


@dataclass(frozen=True, kw_only=True)
class PromotionDocument(SchemaDocument):
    source: PromotionSource
    specificationRevision: str


@dataclass(frozen=True, kw_only=True)
class MaterializationDocument(SchemaDocument):
    path: str
    mediaType: str
    digest: str
    metadata: JsonObjectValue


@dataclass(frozen=True, kw_only=True)
class ResolvedInputs(StrictModel):
    promotions: dict[str, str] | None = None
    receipts: dict[str, str] | None = None
    artifacts: dict[str, str] | None = None
    importedArtifacts: dict[str, str] | None = None
    importedArtifactEvidence: dict[str, JsonObjectValue] | None = None


@dataclass(frozen=True, kw_only=True)
class AuthoredSource(StrictModel):
    path: str = field(
        metadata={"description": "Repository-relative path resolved from the root of the selected source revision."}
    )
    inputs: list[str] | None = field(
        default=None,
        metadata={"description": "Input paths or glob patterns resolved relative to source.path."},
    )


@dataclass(frozen=True, kw_only=True)
class DesiredSource(StrictModel):
    path: str = field(
        metadata={"description": "Repository-relative path resolved from the root of the selected source revision."}
    )
    revision: str | None = None
    driverVersion: int | None = None
    inputHash: str | None = None
    inputs: list[str] | None = field(
        default=None,
        metadata={"description": "Input paths or glob patterns resolved relative to source.path."},
    )


@dataclass(frozen=True, kw_only=True)
class AwsEcrCredentialProvider(StrictModel):
    type: Literal["aws-ecr"]


@dataclass(frozen=True, kw_only=True)
class ReceiptDesired(StrictModel):
    unitBlob: str
    revision: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReceiptSubjectDocument(StrictModel):
    apiVersion: str
    kind: str
    name: str


@dataclass(frozen=True, kw_only=True)
class ReceiptSpecDocument(StrictModel):
    subject: ReceiptSubjectDocument
    desired: ReceiptDesired
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True, kw_only=True)
class ReceiptStatusDocument(StrictModel):
    controller: JsonObjectValue
    result: JsonObjectValue
    artifacts: JsonObjectValue | None = None


@dataclass(frozen=True, kw_only=True)
class ReceiptMetadata(StrictModel):
    name: str


@dataclass(frozen=True, kw_only=True)
class ReceiptDocument(SchemaDocument):
    apiVersion: Literal["gitopsctr.io/v1"]
    kind: Literal["Receipt"]
    metadata: ReceiptMetadata
    spec: ReceiptSpecDocument
    status: ReceiptStatusDocument


@dataclass(frozen=True, kw_only=True)
class ArtifactDescriptor(StrictModel):
    apiVersion: str
    kind: str
    path: str
    digest: str
    mediaType: str


CORE_CONTRACTS: dict[str, DocumentContract] = {
    "environment": MashumaroContract(
        EnvironmentDocument,
        f"{SCHEMA_ROOT}/core/v1/environment.schema.json",
    ),
    "promotion": MashumaroContract(PromotionDocument, f"{SCHEMA_ROOT}/core/v1/promotion.schema.json"),
    "materialization": MashumaroContract(
        MaterializationDocument,
        f"{SCHEMA_ROOT}/core/v1/materialization.schema.json",
    ),
    "receipt": MashumaroContract(ReceiptDocument, f"{SCHEMA_ROOT}/core/v1/receipt.schema.json"),
    "stack-template-authored": MashumaroContract(
        StackTemplateDocument,
        f"{SCHEMA_ROOT}/core/v1/stack-template-authored.schema.json",
    ),
    "stack-template-desired": MashumaroContract(
        DesiredStackTemplateDocument,
        f"{SCHEMA_ROOT}/core/v1/stack-template-desired.schema.json",
    ),
    "stack-authored": MashumaroContract(
        StackDocument,
        f"{SCHEMA_ROOT}/core/v1/stack-authored.schema.json",
    ),
    "stack-desired": MashumaroContract(
        DesiredStackDocument,
        f"{SCHEMA_ROOT}/core/v1/stack-desired.schema.json",
    ),
}


def schema_url(scope: str, version: int, kind: str) -> str:
    return f"{SCHEMA_ROOT}/{scope}/v{version}/{kind}.schema.json"


def artifact_descriptors_schema(artifacts: Mapping[str, tuple[str, str, str]]) -> JsonObject:
    properties: dict[str, Any] = {}
    for name, (api_version, kind, media_type) in artifacts.items():
        properties[name] = {
            "type": "object",
            "properties": {
                "apiVersion": {"const": api_version},
                "kind": {"const": kind},
                "path": {"type": "string"},
                "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "mediaType": {"enum": [f"{media_type}+yaml", f"{media_type}+json"]},
            },
            "required": ["apiVersion", "kind", "path", "digest", "mediaType"],
            "additionalProperties": False,
        }
    return cast(
        JsonObject,
        {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
    )


def receipt_schema(
    driver: str,
    version: int,
    result_contract: DocumentContract,
    artifacts: Mapping[str, tuple[str, str, str]] | None = None,
) -> JsonObject:
    """Compose a driver receipt schema with the typed result nested in status."""
    core = deepcopy(CORE_CONTRACTS["receipt"].json_schema())
    result = deepcopy(result_contract.json_schema())
    for component in (core, result):
        component.pop("$schema", None)
        component.pop("$id", None)
        component.pop("title", None)
        component.pop("additionalProperties", None)
    result.pop("additionalProperties", None)
    for variant in cast(list[dict[str, Any]], result.get("oneOf", [])):
        variant.pop("additionalProperties", None)
    if artifacts:
        core_properties = cast(dict[str, Any], core["properties"])
        status = cast(dict[str, Any], core_properties["status"])
        status_properties = cast(dict[str, Any], status["properties"])
        status_properties["artifacts"] = artifact_descriptors_schema(artifacts)
        cast(list[str], status["required"]).append("artifacts")
    core_properties = cast(dict[str, Any], core["properties"])
    status = cast(dict[str, Any], core_properties["status"])
    status_result = cast(dict[str, Any], status["properties"]["result"])
    status_result.clear()
    status_result.update(result)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(f"drivers/{driver}", version, "receipt"),
        "title": f"{driver} receipt v{version}",
        "allOf": [core],
        "unevaluatedProperties": False,
    }


def with_schema(document: dict[str, Any], schema_id: str) -> dict[str, Any]:
    return {"$schema": schema_id, **document}
