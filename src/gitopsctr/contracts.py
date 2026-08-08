"""Library-neutral JSON document contracts and core document models."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mashumaro.config import BaseConfig
from mashumaro.jsonschema import build_json_schema
from mashumaro.mixins.dict import DataClassDictMixin

from gitopsctr.document import ContractError, DocumentContract, JsonObject

SCHEMA_ROOT = "https://niklasrosenstein.github.io/gitopsctr/schemas"


class StrictModel(DataClassDictMixin):
    class Config(BaseConfig):
        forbid_extra_keys = True
        allow_deserialization_not_by_alias = True
        serialize_by_alias = True


@dataclass(frozen=True, kw_only=True)
class SchemaDocument(StrictModel):
    schema_hint: Any = field(default=None, metadata={"alias": "$schema"})


@dataclass(frozen=True)
class MashumaroContract(DocumentContract):
    model: type[StrictModel]
    schema_id: str

    def json_schema(self) -> JsonObject:
        schema = cast(JsonObject, build_json_schema(self.model, with_dialect_uri=True).to_dict())
        schema["$id"] = self.schema_id
        return schema

    def validate(self, document: object) -> JsonObject:
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            raise ContractError("expected a JSON object")
        candidate = dict(document)
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
        try:
            Draft202012Validator(schema).validate(candidate)
            self.model.from_dict(candidate)
        except (ValidationError, TypeError, ValueError) as exc:
            detail = exc.message if isinstance(exc, ValidationError) else str(exc)
            raise ContractError(detail) from exc
        return cast(JsonObject, document)


@dataclass(frozen=True)
class MashumaroUnionContract(DocumentContract):
    models: tuple[type[StrictModel], ...]
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

    def validate(self, document: object) -> JsonObject:
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            raise ContractError("expected a JSON object")
        candidate = dict(document)
        if candidate.get("schema") == 1:
            candidate.pop("schema", None)
        try:
            Draft202012Validator(self.json_schema()).validate(candidate)
        except ValidationError as exc:
            raise ContractError(exc.message) from exc
        return cast(JsonObject, document)


@dataclass(frozen=True, kw_only=True)
class EnvironmentRefs(StrictModel):
    desired: str | None = None
    observed: str | None = None


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
    metadata: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class AuthoredSource(StrictModel):
    path: str
    inputs: list[str] | None = None


@dataclass(frozen=True, kw_only=True)
class DesiredSource(StrictModel):
    path: str
    revision: str | None = None
    driverVersion: int | None = None
    inputHash: str | None = None
    inputs: list[str] | None = None


@dataclass(frozen=True, kw_only=True)
class DesiredUnitDocument(SchemaDocument):
    name: str
    driver: str
    source: DesiredSource
    inputs: dict[str, Any] | None = None
    resolvedInputs: dict[str, dict[str, str]] | None = None
    materialization: MaterializationDocument | None = None


@dataclass(frozen=True, kw_only=True)
class AwsEcrCredentialProvider(StrictModel):
    type: Literal["aws-ecr"]


@dataclass(frozen=True, kw_only=True)
class ReceiptDesired(StrictModel):
    unitBlob: str
    revision: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReceiptDocument(SchemaDocument):
    unit: str
    driver: str
    desired: ReceiptDesired
    resolvedInputs: dict[str, dict[str, str]] | None = None
    controller: dict[str, Any] | None = None
    planEvidence: dict[str, Any] | None = None


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
    "desired-unit": MashumaroContract(
        DesiredUnitDocument,
        f"{SCHEMA_ROOT}/core/v1/desired-unit.schema.json",
    ),
    "receipt": MashumaroContract(ReceiptDocument, f"{SCHEMA_ROOT}/core/v1/receipt.schema.json"),
}


def schema_url(scope: str, version: int, kind: str) -> str:
    return f"{SCHEMA_ROOT}/{scope}/v{version}/{kind}.schema.json"


def receipt_schema(driver: str, version: int, result_contract: DocumentContract) -> JsonObject:
    """Compose the generic receipt envelope with a plugin's flattened result."""
    core = deepcopy(CORE_CONTRACTS["receipt"].json_schema())
    result = deepcopy(result_contract.json_schema())
    for component in (core, result):
        component.pop("$schema", None)
        component.pop("$id", None)
        component.pop("title", None)
        component.pop("additionalProperties", None)
    for variant in cast(list[dict[str, Any]], result.get("oneOf", [])):
        variant.pop("additionalProperties", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(f"drivers/{driver}", version, "receipt"),
        "title": f"{driver} receipt v{version}",
        "allOf": [core, result],
        "unevaluatedProperties": False,
    }


def with_schema(document: dict[str, Any], schema_id: str) -> dict[str, Any]:
    return {"$schema": schema_id, **document}
