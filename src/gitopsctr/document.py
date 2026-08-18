"""GitOpsCtr model-library helpers layered on the resource API documents."""

from __future__ import annotations

from typing import cast

from mashumaro.types import SerializableType

from gitopsctr.resource_api import JsonObject as _JsonObject
from gitopsctr.resource_api import JsonValue as _JsonValue
from gitopsctr.resource_api import require_json_value as _require_json_value

REFERENCE_KEYS = frozenset(("fromReceipt", "fromArtifact", "fromPromotion"))


class JsonObjectValue(dict[str, _JsonValue], SerializableType):
    """Mashumaro-compatible, recursively typed arbitrary JSON object."""

    def _serialize(self) -> _JsonObject:
        return dict(self)

    @classmethod
    def _deserialize(cls, value: object) -> JsonObjectValue:
        parsed = _require_json_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return cls(parsed)


def require_resolved_json_value(value: object) -> _JsonValue:
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


class ResolvedJsonObjectValue(dict[str, _JsonValue], SerializableType):
    """Mashumaro-compatible JSON object that cannot contain authored references."""

    def _serialize(self) -> _JsonObject:
        return dict(self)

    @classmethod
    def _deserialize(cls, value: object) -> ResolvedJsonObjectValue:
        parsed = require_resolved_json_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a resolved JSON object")
        return cls(parsed)
