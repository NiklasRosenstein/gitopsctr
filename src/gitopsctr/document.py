"""Library-neutral JSON document contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import cast

from mashumaro.types import SerializableType

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
REFERENCE_KEYS = frozenset(("fromReceipt", "fromArtifact", "fromPromotion"))


def require_json_value(value: object) -> JsonValue:
    """Validate an arbitrary Python value as JSON without coercing it."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [require_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {cast(str, key): require_json_value(item) for key, item in value.items()}
    raise ValueError(f"expected a JSON value, got {type(value).__name__}")


class JsonObjectValue(dict[str, JsonValue], SerializableType):
    """Mashumaro-compatible, recursively typed arbitrary JSON object."""

    def _serialize(self) -> JsonObject:
        return dict(self)

    @classmethod
    def _deserialize(cls, value: object) -> JsonObjectValue:
        parsed = require_json_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return cls(parsed)


def require_resolved_json_value(value: object) -> JsonValue:
    """Validate JSON while rejecting authored reference-expression keys."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [require_resolved_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        if REFERENCE_KEYS.intersection(value):
            raise ValueError("resolved JSON must not contain authored reference expressions")
        return {cast(str, key): require_resolved_json_value(item) for key, item in value.items()}
    raise ValueError(f"expected a resolved JSON value, got {type(value).__name__}")


class ResolvedJsonObjectValue(dict[str, JsonValue], SerializableType):
    """Mashumaro-compatible JSON object that cannot contain authored references."""

    def _serialize(self) -> JsonObject:
        return dict(self)

    @classmethod
    def _deserialize(cls, value: object) -> ResolvedJsonObjectValue:
        parsed = require_resolved_json_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a resolved JSON object")
        return cls(parsed)


class ContractError(ValueError):
    """A JSON document does not satisfy its public structural contract."""


class DocumentContract(ABC):
    """Validation and JSON Schema generation without imposing a model library on plugins."""

    @abstractmethod
    def validate(self, document: object) -> JsonObject:
        """Validate and return a JSON object without trusting its ``$schema`` hint."""

    @abstractmethod
    def json_schema(self) -> JsonObject:
        """Return this contract's Draft 2020-12 JSON Schema."""


class TypedDocumentContract[T](DocumentContract):
    """A contract that validates documents into a well-typed Python value."""

    @abstractmethod
    def parse(self, document: object) -> T:
        """Validate and deserialize an untrusted document."""

    @abstractmethod
    def dump(self, value: T) -> JsonObject:
        """Serialize a typed value into its public document representation."""
