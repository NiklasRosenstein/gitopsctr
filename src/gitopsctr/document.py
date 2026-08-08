"""Library-neutral JSON document contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


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
