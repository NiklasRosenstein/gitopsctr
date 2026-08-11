"""Typed authored-value expressions resolved while desired state is built."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, Protocol, TypeAlias, TypeVar, cast

from mashumaro.config import BaseConfig
from mashumaro.exceptions import MissingField
from mashumaro.jsonschema.annotations import Pattern
from mashumaro.mixins.dict import DataClassDictMixin
from mashumaro.types import SerializableType

from gitopsctr.api import GVK
from gitopsctr.document import REFERENCE_KEYS as DOCUMENT_REFERENCE_KEYS
from gitopsctr.document import JsonObject, JsonScalar, JsonValue, require_json_value

# ``fromEnvironment`` is a value lookup, not a source or lifecycle reference.
# Keep it in this module's expression language without changing the lower-level
# document contract used by resolved JSON values.
REFERENCE_KEYS = frozenset((*DOCUMENT_REFERENCE_KEYS, "fromEnvironment"))

type ResourceName = Annotated[str, Pattern("^[a-z0-9][a-z0-9-]*$")]
type JsonPointer = Annotated[str, Pattern("^(?:$|/(?:[^~]|~[01])*)$")]
type ApiVersion = Annotated[str, Pattern("^[^/]+/[^/]+$")]
type Kind = Annotated[str, Pattern("^[A-Z][A-Za-z0-9]*$")]


class TemplateModel(DataClassDictMixin):
    class Config(BaseConfig):
        forbid_extra_keys = True
        allow_deserialization_not_by_alias = True
        serialize_by_alias = True


class TemplateError(ValueError):
    """An authored value does not conform to the reference expression language."""


@dataclass(frozen=True)
class DryFallbackValue(SerializableType):
    """Distinguish an omitted fallback from an explicitly authored JSON null."""

    value: JsonValue
    specified: bool = True

    def _serialize(self) -> JsonValue:
        return self.value

    @classmethod
    def _deserialize(cls, value: object) -> DryFallbackValue:
        return cls(cast(JsonValue, value))


_NO_DRY_FALLBACK = DryFallbackValue(None, specified=False)


@dataclass(frozen=True, kw_only=True)
class ReceiptReferenceTarget(TemplateModel):
    unit: ResourceName
    pointer: JsonPointer = ""
    dryFallback: DryFallbackValue = field(default=_NO_DRY_FALLBACK, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        _validate_unit(self.unit, "fromReceipt")
        _validate_pointer(self.pointer)

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        if not has_dry_fallback(self):
            d.pop("dryFallback", None)
        return d


@dataclass(frozen=True, kw_only=True)
class ArtifactReferenceTarget(TemplateModel):
    unit: ResourceName
    name: ResourceName
    apiVersion: ApiVersion
    kind: Kind
    pointer: JsonPointer = ""
    dryFallback: DryFallbackValue = field(default=_NO_DRY_FALLBACK, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        _validate_unit(self.unit, "fromArtifact")
        if not _RESOURCE_NAME.fullmatch(self.name):
            raise TemplateError(f"invalid fromArtifact name: {self.name!r}")
        if not _KIND.fullmatch(self.kind):
            raise TemplateError(f"invalid fromArtifact kind: {self.kind!r}")
        _validate_pointer(self.pointer)
        _ = self.gvk

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        if not has_dry_fallback(self):
            d.pop("dryFallback", None)
        return d

    @property
    def gvk(self) -> GVK:
        return GVK(self.apiVersion, self.kind)


@dataclass(frozen=True, kw_only=True)
class PromotionReferenceTarget(TemplateModel):
    unit: ResourceName | None = None
    pointer: JsonPointer | None = None
    dryFallback: DryFallbackValue = field(default=_NO_DRY_FALLBACK, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.unit is not None:
            _validate_unit(self.unit, "fromPromotion")
        if self.pointer is not None:
            _validate_pointer(self.pointer)

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        if self.unit is None:
            d.pop("unit", None)
        if self.pointer is None:
            d.pop("pointer", None)
        if not has_dry_fallback(self):
            d.pop("dryFallback", None)
        return d


@dataclass(frozen=True, kw_only=True)
class ReceiptReference(TemplateModel):
    fromReceipt: ReceiptReferenceTarget


@dataclass(frozen=True, kw_only=True)
class ArtifactReference(TemplateModel):
    fromArtifact: ArtifactReferenceTarget


@dataclass(frozen=True, kw_only=True)
class PromotionReference(TemplateModel):
    fromPromotion: PromotionReferenceTarget


@dataclass(frozen=True, kw_only=True)
class EnvironmentReferenceTarget(TemplateModel):
    pointer: JsonPointer = ""

    def __post_init__(self) -> None:
        _validate_pointer(self.pointer)


@dataclass(frozen=True, kw_only=True)
class EnvironmentReference(TemplateModel):
    """Read a value from the Environment resource in the target context."""

    fromEnvironment: EnvironmentReferenceTarget


@dataclass(frozen=True, kw_only=True)
class ParameterReferenceTarget(TemplateModel):
    name: ResourceName

    def __post_init__(self) -> None:
        if not _RESOURCE_NAME.fullmatch(self.name):
            raise TemplateError(f"invalid fromParameter name: {self.name!r}")


@dataclass(frozen=True, kw_only=True)
class ParameterReference(TemplateModel):
    """A safe, typed reference to one Stack parameter."""

    fromParameter: ParameterReferenceTarget


type ReferenceExpression = ReceiptReference | ArtifactReference | PromotionReference | EnvironmentReference
type ParameterExpression = ParameterReference
FixedT = TypeVar("FixedT")
# Mashumaro does not yet expand a PEP 695 generic alias while generating JSON Schema.
AuthoredValue: TypeAlias = FixedT | ReferenceExpression  # noqa: UP040
type TemplateValue = (
    ParameterReference
    | EnvironmentReference
    | ReceiptReference
    | ArtifactReference
    | PromotionReference
    | JsonScalar
    | list[TemplateValue]
    | dict[str, TemplateValue]
)

type ParameterTemplateValue = (
    ParameterReference
    | ReferenceExpression
    | JsonScalar
    | list[ParameterTemplateValue]
    | dict[str, ParameterTemplateValue]
)


class TemplateObject(dict[str, TemplateValue], SerializableType):
    """Mashumaro-compatible mapping whose values may contain references."""

    def _serialize(self) -> dict[str, JsonValue]:
        return {name: dump_template_value(value) for name, value in self.items()}

    @classmethod
    def _deserialize(cls, value: object) -> TemplateObject:
        parsed = parse_template_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a template object")
        return cls(parsed)


class ParameterTemplateObject(dict[str, ParameterTemplateValue], SerializableType):
    """A recursively typed object containing only fixed values and parameters."""

    def _serialize(self) -> dict[str, JsonValue]:
        return {name: dump_parameter_value(value) for name, value in self.items()}

    @classmethod
    def _deserialize(cls, value: object) -> ParameterTemplateObject:
        parsed = parse_parameter_value(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a parameter template object")
        return cls(parsed)


_RESOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_KIND = re.compile(r"[A-Z][A-Za-z0-9]*")


def _validate_pointer(pointer: str) -> None:
    if pointer and not pointer.startswith("/"):
        raise TemplateError(f"JSON pointer must start with '/': {pointer!r}")
    if re.search(r"~(?![01])", pointer):
        raise TemplateError(f"JSON pointer has an invalid escape: {pointer!r}")


def _validate_unit(unit: str, reference_type: str) -> None:
    if not _RESOURCE_NAME.fullmatch(unit):
        raise TemplateError(f"invalid {reference_type} unit: {unit!r}")


def _child_pointer(pointer: str, child: str | int) -> str:
    token = str(child).replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{token}"


def child_pointer(pointer: str, child: str | int) -> str:
    """Append one RFC 6901 token to a JSON Pointer."""

    return _child_pointer(pointer, child)


def has_dry_fallback(
    target: ReceiptReferenceTarget | ArtifactReferenceTarget | PromotionReferenceTarget,
) -> bool:
    return target.dryFallback.specified


def _parse_dry_fallback(target: TemplateModel, value: dict[str, object], pointer: str) -> TemplateModel:
    if "dryFallback" not in value:
        return target
    fallback = parse_template_value(value["dryFallback"], _child_pointer(pointer, "dryFallback"))
    return cast(
        TemplateModel,
        replace(
            cast(Any, target),
            dryFallback=DryFallbackValue(dump_template_value(fallback)),
        ),
    )


def _parse_reference(value: dict[str, object], pointer: str) -> ReferenceExpression:
    keys = REFERENCE_KEYS.intersection(value)
    if len(keys) != 1 or len(value) != 1:
        raise TemplateError(f"{pointer or '/'}: reference expression must contain exactly one reference variant")
    key = next(iter(keys))
    target = value[key]
    if not isinstance(target, dict) or not all(isinstance(name, str) for name in target):
        raise TemplateError(f"{_child_pointer(pointer, key)}: invalid {key} reference")
    try:
        if key == "fromEnvironment":
            parsed = EnvironmentReferenceTarget.from_dict(target)
            _validate_pointer(parsed.pointer)
            return EnvironmentReference(fromEnvironment=parsed)
        if key == "fromReceipt":
            if "unit" not in target:
                raise TemplateError("fromReceipt requires unit")
            parsed = cast(
                ReceiptReferenceTarget,
                _parse_dry_fallback(ReceiptReferenceTarget.from_dict(target), target, _child_pointer(pointer, key)),
            )
            _validate_unit(parsed.unit, key)
            _validate_pointer(parsed.pointer)
            return ReceiptReference(fromReceipt=parsed)
        if key == "fromPromotion":
            if "unit" in target and not isinstance(target["unit"], str):
                raise TemplateError("fromPromotion unit must be a string")
            if "pointer" in target and not isinstance(target["pointer"], str):
                raise TemplateError("fromPromotion pointer must be a string")
            parsed = cast(
                PromotionReferenceTarget,
                _parse_dry_fallback(PromotionReferenceTarget.from_dict(target), target, _child_pointer(pointer, key)),
            )
            if parsed.unit is not None:
                _validate_unit(parsed.unit, key)
            if parsed.pointer is not None:
                _validate_pointer(parsed.pointer)
            return PromotionReference(fromPromotion=parsed)
        if "unit" not in target or "name" not in target:
            raise TemplateError("fromArtifact requires unit and name")
        if not isinstance(target.get("apiVersion"), str) or not isinstance(target.get("kind"), str):
            raise TemplateError("fromArtifact requires string apiVersion and kind")
        parsed = cast(
            ArtifactReferenceTarget,
            _parse_dry_fallback(ArtifactReferenceTarget.from_dict(target), target, _child_pointer(pointer, key)),
        )
        _validate_unit(parsed.unit, key)
        if not _RESOURCE_NAME.fullmatch(parsed.name):
            raise TemplateError(f"invalid {key} name: {parsed.name!r}")
        if not _KIND.fullmatch(parsed.kind):
            raise TemplateError(f"invalid {key} kind: {parsed.kind!r}")
        _validate_pointer(parsed.pointer)
        try:
            _ = parsed.gvk
        except ValueError as exc:
            raise TemplateError(str(exc)) from exc
        return ArtifactReference(fromArtifact=parsed)
    except (MissingField, TypeError, ValueError) as exc:
        if isinstance(exc, TemplateError):
            raise TemplateError(f"{_child_pointer(pointer, key)}: {exc}") from exc
        raise TemplateError(f"{_child_pointer(pointer, key)}: invalid {key} reference: {exc}") from exc


def _parse_parameter_reference(value: dict[str, object], pointer: str) -> ParameterReference:
    if set(value) != {"fromParameter"}:
        raise TemplateError(f"{pointer or '/'}: parameter expression must contain exactly fromParameter")
    target = value["fromParameter"]
    if not isinstance(target, dict) or not all(isinstance(name, str) for name in target):
        raise TemplateError(f"{_child_pointer(pointer, 'fromParameter')}: invalid fromParameter expression")
    try:
        return ParameterReference(fromParameter=ParameterReferenceTarget.from_dict(target))
    except (MissingField, TypeError, ValueError) as exc:
        raise TemplateError(
            f"{_child_pointer(pointer, 'fromParameter')}: invalid fromParameter expression: {exc}"
        ) from exc


def parse_template_value(value: object, pointer: str = "") -> TemplateValue:
    """Parse untrusted JSON into a complete authored-value expression tree."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [parse_template_value(item, _child_pointer(pointer, index)) for index, item in enumerate(value)]
    if isinstance(value, dict):
        if not all(isinstance(name, str) for name in value):
            raise TemplateError("template object keys must be strings")
        candidate = cast(dict[str, object], value)
        if "fromParameter" in candidate:
            return _parse_parameter_reference(candidate, pointer)
        if REFERENCE_KEYS.intersection(candidate):
            return _parse_reference(candidate, pointer)
        return {name: parse_template_value(item, _child_pointer(pointer, name)) for name, item in candidate.items()}
    raise TemplateError(f"template value is not JSON: {type(value).__name__}")


def parse_parameter_value(value: object, pointer: str = "") -> ParameterTemplateValue:
    """Parse a StackTemplate value, permitting only safe parameter expressions."""

    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TemplateError(f"{pointer or '/'}: parameter value is not a finite JSON number")
        return value
    if isinstance(value, list):
        return [parse_parameter_value(item, _child_pointer(pointer, index)) for index, item in enumerate(value)]
    if isinstance(value, dict):
        if not all(isinstance(name, str) for name in value):
            raise TemplateError("parameter template object keys must be strings")
        candidate = cast(dict[str, object], value)
        if "fromParameter" in candidate:
            return _parse_parameter_reference(candidate, pointer)
        if REFERENCE_KEYS.intersection(candidate):
            raise TemplateError(f"{pointer or '/'}: only fromParameter expressions are allowed in StackTemplate specs")
        return {name: parse_parameter_value(item, _child_pointer(pointer, name)) for name, item in candidate.items()}
    raise TemplateError(f"parameter template value is not JSON: {type(value).__name__}")


def dump_template_value(value: TemplateValue) -> JsonValue:
    """Serialize an authored expression tree to its public JSON representation."""

    if isinstance(value, ReceiptReference):
        target: JsonObject = {"unit": value.fromReceipt.unit}
        if value.fromReceipt.pointer:
            target["pointer"] = value.fromReceipt.pointer
        if has_dry_fallback(value.fromReceipt):
            target["dryFallback"] = value.fromReceipt.dryFallback.value
        return {"fromReceipt": target}
    if isinstance(value, ArtifactReference):
        target = cast(
            JsonObject,
            {
                "unit": value.fromArtifact.unit,
                "name": value.fromArtifact.name,
                "apiVersion": value.fromArtifact.apiVersion,
                "kind": value.fromArtifact.kind,
            },
        )
        if value.fromArtifact.pointer:
            target["pointer"] = value.fromArtifact.pointer
        if has_dry_fallback(value.fromArtifact):
            target["dryFallback"] = value.fromArtifact.dryFallback.value
        return {"fromArtifact": target}
    if isinstance(value, PromotionReference):
        target = cast(JsonObject, {})
        if value.fromPromotion.unit is not None:
            target["unit"] = value.fromPromotion.unit
        if value.fromPromotion.pointer is not None:
            target["pointer"] = value.fromPromotion.pointer
        if has_dry_fallback(value.fromPromotion):
            target["dryFallback"] = value.fromPromotion.dryFallback.value
        return {"fromPromotion": target}
    if isinstance(value, EnvironmentReference):
        target: JsonObject = {}
        if value.fromEnvironment.pointer:
            target["pointer"] = value.fromEnvironment.pointer
        return {"fromEnvironment": target}
    if isinstance(value, ParameterReference):
        return {"fromParameter": {"name": value.fromParameter.name}}
    if isinstance(value, list):
        return [dump_template_value(item) for item in value]
    if isinstance(value, dict):
        return {name: dump_template_value(item) for name, item in value.items()}
    return cast(JsonValue, value)


def dump_parameter_value(value: ParameterTemplateValue) -> JsonValue:
    """Serialize a StackTemplate parameter expression tree."""

    if isinstance(value, ParameterReference):
        return {"fromParameter": {"name": value.fromParameter.name}}
    if isinstance(value, list):
        return [dump_parameter_value(item) for item in value]
    if isinstance(value, dict):
        return {name: dump_parameter_value(item) for name, item in value.items()}
    return cast(JsonValue, value)


def resolve_parameter_value(
    value: ParameterTemplateValue, parameters: Mapping[str, JsonValue]
) -> ParameterTemplateValue:
    """Resolve parameters and preserve other runtime references for later resolution."""

    if isinstance(value, ParameterReference):
        try:
            return cast(ParameterTemplateValue, deepcopy(parameters[value.fromParameter.name]))
        except KeyError as exc:
            raise TemplateError(f"missing parameter: {value.fromParameter.name}") from exc
    if isinstance(value, list):
        return [resolve_parameter_value(item, parameters) for item in value]
    if isinstance(value, dict):
        return {name: resolve_parameter_value(item, parameters) for name, item in value.items()}
    return value


class ParameterDeclarationLike(Protocol):
    name: str
    type: Literal["string", "integer", "number", "boolean", "object", "array"]


def validate_parameter_values(
    declarations: list[ParameterDeclarationLike],
    values: Mapping[str, object],
) -> dict[str, JsonValue]:
    """Validate Stack values in declaration order and return a detached copy."""

    if not all(isinstance(name, str) for name in values):
        raise TemplateError("parameter names must be strings")
    declared = {declaration.name: declaration for declaration in declarations}
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise TemplateError(f"unknown parameters: {', '.join(unknown)}")
    missing = [declaration.name for declaration in declarations if declaration.name not in values]
    if missing:
        raise TemplateError(f"missing parameters: {', '.join(missing)}")

    result: dict[str, JsonValue] = {}
    for declaration in declarations:
        value = require_json_value(values[declaration.name])
        _require_finite_json(value)
        if not _parameter_type_matches(declaration.type, value):
            raise TemplateError(
                f"parameter {declaration.name!r} must be {declaration.type}, got {type(value).__name__}"
            )
        result[declaration.name] = value
    return result


def _parameter_type_matches(
    kind: Literal["string", "integer", "number", "boolean", "object", "array"], value: JsonValue
) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in (int, float) and (not isinstance(value, float) or math.isfinite(value))
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return False


def _require_finite_json(value: JsonValue) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TemplateError("parameter values must contain only finite JSON numbers")
    elif isinstance(value, list):
        for item in value:
            _require_finite_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            _require_finite_json(item)


def references(value: TemplateValue) -> tuple[ReferenceExpression, ...]:
    """Return all references in deterministic document order."""

    found: list[ReferenceExpression] = []

    def visit(candidate: TemplateValue) -> None:
        if isinstance(candidate, (ReceiptReference, ArtifactReference, PromotionReference)):
            found.append(candidate)
        elif isinstance(candidate, list):
            for item in candidate:
                visit(item)
        elif isinstance(candidate, dict):
            for item in candidate.values():
                visit(item)

    visit(value)
    return tuple(found)


def contains_reference(value: TemplateValue) -> bool:
    return bool(references(value))
