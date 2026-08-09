"""Library-neutral JSON document contracts and core document models."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
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
    PromotionReference,
    ReceiptReference,
    TemplateObject,
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
        reference_type = {
            ReceiptReference: "fromReceipt",
            ArtifactReference: "fromArtifact",
            PromotionReference: "fromPromotion",
        }.get(instance.type)
        if reference_type is not None:
            return JSONSchema(title=f"__gitopsctr_reference_{reference_type}__")
        if instance.type is JsonObjectValue:
            return JSONSchema(type=JSONSchemaInstanceType.OBJECT)
        if instance.type is ResolvedJsonObjectValue:
            return JSONSchema(title="__gitopsctr_resolved_json_object__")
        return None


def _reference_target_schema(reference_type: str) -> JsonObject:
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
        "dryFallback": {
            "$ref": "#/$defs/TemplateValue",
            "description": (
                "Type-correct speculative value used only during dry resolution when the reference is unavailable."
            ),
        },
    }
    required = [] if reference_type == "fromPromotion" else ["unit"]
    if reference_type != "fromPromotion":
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


def _reference_expression_schema(reference_type: str) -> JsonObject:
    return {
        "type": "object",
        "properties": {reference_type: _reference_target_schema(reference_type)},
        "required": [reference_type],
        "additionalProperties": False,
    }


def _template_definitions() -> JsonObject:
    variants: list[JsonObject] = []
    for key in sorted(REFERENCE_KEYS):
        variants.append(_reference_expression_schema(key))
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
                    "propertyNames": {"not": {"enum": sorted(REFERENCE_KEYS)}},
                    "additionalProperties": {"$ref": "#/$defs/TemplateValue"},
                },
            ],
        )
    )
    return cast(JsonObject, {"TemplateValue": {"oneOf": variants}})


def _expand_special_schemas(schema: JsonObject) -> JsonObject:
    used_template = False
    used_resolved_json = False

    def visit(value: JsonValue) -> JsonValue:
        nonlocal used_template
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
                    "propertyNames": {"not": {"enum": sorted(REFERENCE_KEYS)}},
                    "additionalProperties": {"$ref": "#/$defs/TemplateValue"},
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
        # Flat documents from before the resource API carried ``schema: 1``.
        # It is discarded at the boundary and is not part of any current contract.
        if "schema" not in cast(dict[str, Any], schema.get("properties", {})) and candidate.get("schema") == 1:
            candidate.pop("schema", None)
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
        if candidate.get("schema") == 1:
            candidate.pop("schema", None)
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
