"""Built-in controller resource API-kind registrations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import cached_property
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator

from gitopsctr.api import GVK, ApiKind
from gitopsctr.contracts import CORE_CONTRACTS
from gitopsctr.document import ContractError, JsonObject, TypedDocumentContract, require_json_value
from gitopsctr.formats import PROJECT_RESOURCE_SCHEMA

CORE_API_VERSION = "gitopsctr.io/v1"


@dataclass(frozen=True)
class JsonSchemaResourceContract(TypedDocumentContract[JsonObject]):
    """Executable contract for a resource already described by JSON Schema."""

    schema: JsonObject

    @cached_property
    def _validator(self) -> Validator:
        return Draft202012Validator(self.schema)

    def parse(self, document: object) -> JsonObject:
        try:
            value = require_json_value(document)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            self._validator.validate(value)
            return value
        except (ValidationError, ValueError) as exc:
            detail = exc.message if isinstance(exc, ValidationError) else str(exc)
            raise ContractError(detail) from exc

    def dump(self, value: JsonObject) -> JsonObject:
        return self.parse(value)

    def validate(self, document: object) -> JsonObject:
        return self.parse(document)

    def json_schema(self) -> JsonObject:
        return deepcopy(self.schema)


@dataclass(frozen=True)
class EnvelopeSpecContract(TypedDocumentContract[Any]):
    """Adapt a typed specification contract to its resource envelope."""

    api_version: str
    kind: str
    specification: TypedDocumentContract[Any]
    name_in_specification: bool

    def parse(self, document: object) -> Any:
        if not isinstance(document, dict):
            raise ContractError("expected a JSON object")
        allowed = {"$schema", "apiVersion", "kind", "metadata", "spec"}
        unexpected = set(document) - allowed
        if unexpected:
            raise ContractError(f"unexpected resource fields: {sorted(unexpected)}")
        schema_hint = document.get("$schema")
        if schema_hint is not None and not isinstance(schema_hint, str):
            raise ContractError("resource $schema must be a string")
        if document.get("apiVersion") != self.api_version or document.get("kind") != self.kind:
            raise ContractError(f"expected {self.api_version}/{self.kind}")
        metadata, spec = document.get("metadata"), document.get("spec")
        if not isinstance(metadata, dict) or set(metadata) != {"name"}:
            raise ContractError("resource metadata must contain only name")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ContractError("resource metadata requires a non-empty name")
        if not isinstance(spec, dict):
            raise ContractError("resource spec must be an object")
        candidate = dict(spec)
        if self.name_in_specification:
            candidate["name"] = name
        parsed = self.specification.parse(candidate)
        if not self.name_in_specification:
            source = getattr(parsed, "source", None)
            source_environment = getattr(source, "environment", None)
            if source_environment != name:
                raise ContractError(f"{self.kind} metadata.name must match spec.source.environment")
        return parsed

    def dump(self, value: Any) -> JsonObject:
        spec = self.specification.dump(value)
        if self.name_in_specification:
            name = spec.pop("name", None)
        else:
            source = spec.get("source")
            name = source.get("environment") if isinstance(source, dict) else None
        if not isinstance(name, str) or not name:
            raise ContractError(f"{self.kind} contract cannot derive metadata.name")
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "metadata": {"name": name},
            "spec": spec,
        }

    def validate(self, document: object) -> JsonObject:
        self.parse(document)
        return cast(JsonObject, document)

    def json_schema(self) -> JsonObject:
        specification = deepcopy(self.specification.json_schema())
        specification.pop("$schema", None)
        specification.pop("$id", None)
        properties = cast(dict[str, Any], specification.get("properties", {}))
        required = cast(list[str], specification.get("required", []))
        if self.name_in_specification:
            properties.pop("name", None)
            required = [item for item in required if item != "name"]
        specification["properties"] = properties
        specification["required"] = cast(Any, required)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "apiVersion": {"const": self.api_version},
                "kind": {"const": self.kind},
                "metadata": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "spec": specification,
            },
            "required": ["apiVersion", "kind", "metadata", "spec"],
            "additionalProperties": False,
        }


@dataclass(frozen=True)
class CoreResourceApi:
    """Core resource API with executable contracts for every representation."""

    description: str
    profiles: Mapping[str, TypedDocumentContract[Any]]

    def contract(self, profile: str) -> TypedDocumentContract[Any] | None:
        return self.profiles.get(profile)


def _core_api(
    kind: str,
    description: str,
    profiles: Mapping[str, TypedDocumentContract[Any]],
) -> ApiKind[CoreResourceApi]:
    return ApiKind(GVK(CORE_API_VERSION, kind), CoreResourceApi(description, profiles))


PROJECT = _core_api(
    "Project",
    "Repository configuration and resource root",
    {"authored": JsonSchemaResourceContract(cast(JsonObject, PROJECT_RESOURCE_SCHEMA))},
)
ENVIRONMENT = _core_api(
    "Environment",
    "Deployment namespace and policy",
    {
        "authored": EnvelopeSpecContract(
            CORE_API_VERSION,
            "Environment",
            cast(TypedDocumentContract[Any], CORE_CONTRACTS["environment"]),
            True,
        )
    },
)
STACK_TEMPLATE = _core_api(
    "StackTemplate",
    "Parameterized collection of Unit templates",
    {
        "authored": cast(TypedDocumentContract[Any], CORE_CONTRACTS["stack-template-authored"]),
        "desired": cast(TypedDocumentContract[Any], CORE_CONTRACTS["stack-template-desired"]),
    },
)
STACK = _core_api(
    "Stack",
    "Instantiated StackTemplate",
    {
        "authored": cast(TypedDocumentContract[Any], CORE_CONTRACTS["stack-authored"]),
        "desired": cast(TypedDocumentContract[Any], CORE_CONTRACTS["stack-desired"]),
    },
)
PROMOTION = _core_api(
    "Promotion",
    "Pinned cross-environment promotion lineage",
    {
        "desired": EnvelopeSpecContract(
            CORE_API_VERSION,
            "Promotion",
            cast(TypedDocumentContract[Any], CORE_CONTRACTS["promotion"]),
            False,
        )
    },
)
RECEIPT = _core_api(
    "Receipt",
    "Observation of one exact desired Unit",
    {},
)
