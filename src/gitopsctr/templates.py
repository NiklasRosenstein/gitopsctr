"""Typed authored-value expressions resolved while desired state is built."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, TypeAlias, TypeVar, cast

from mashumaro.config import BaseConfig
from mashumaro.exceptions import MissingField
from mashumaro.jsonschema.annotations import Pattern
from mashumaro.mixins.dict import DataClassDictMixin
from mashumaro.types import SerializableType

from gitopsctr.api import GVK
from gitopsctr.document import JsonScalar, JsonValue

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


@dataclass(frozen=True, kw_only=True)
class ReceiptReferenceTarget(TemplateModel):
    unit: ResourceName
    pointer: JsonPointer = ""

    def __post_init__(self) -> None:
        _validate_unit(self.unit, "fromReceipt")
        _validate_pointer(self.pointer)


@dataclass(frozen=True, kw_only=True)
class ArtifactReferenceTarget(TemplateModel):
    unit: ResourceName
    name: ResourceName
    apiVersion: ApiVersion
    kind: Kind
    pointer: JsonPointer = ""

    def __post_init__(self) -> None:
        _validate_unit(self.unit, "fromArtifact")
        if not _RESOURCE_NAME.fullmatch(self.name):
            raise TemplateError(f"invalid fromArtifact name: {self.name!r}")
        if not _KIND.fullmatch(self.kind):
            raise TemplateError(f"invalid fromArtifact kind: {self.kind!r}")
        _validate_pointer(self.pointer)
        _ = self.gvk

    @property
    def gvk(self) -> GVK:
        return GVK(self.apiVersion, self.kind)


@dataclass(frozen=True, kw_only=True)
class PromotionReferenceTarget(TemplateModel):
    unit: ResourceName
    pointer: JsonPointer = ""

    def __post_init__(self) -> None:
        _validate_unit(self.unit, "fromPromotion")
        _validate_pointer(self.pointer)


@dataclass(frozen=True, kw_only=True)
class ReceiptReference(TemplateModel):
    fromReceipt: ReceiptReferenceTarget


@dataclass(frozen=True, kw_only=True)
class ArtifactReference(TemplateModel):
    fromArtifact: ArtifactReferenceTarget


@dataclass(frozen=True, kw_only=True)
class PromotionReference(TemplateModel):
    fromPromotion: PromotionReferenceTarget


type ReferenceExpression = ReceiptReference | ArtifactReference | PromotionReference
FixedT = TypeVar("FixedT")
# Mashumaro does not yet expand a PEP 695 generic alias while generating JSON Schema.
AuthoredValue: TypeAlias = FixedT | ReferenceExpression  # noqa: UP040
type TemplateValue = (
    ReceiptReference
    | ArtifactReference
    | PromotionReference
    | JsonScalar
    | list[TemplateValue]
    | dict[str, TemplateValue]
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


REFERENCE_KEYS = frozenset(("fromReceipt", "fromArtifact", "fromPromotion"))
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


def _parse_reference(value: dict[str, object], pointer: str) -> ReferenceExpression:
    keys = REFERENCE_KEYS.intersection(value)
    if len(keys) != 1 or len(value) != 1:
        raise TemplateError(f"{pointer or '/'}: reference expression must contain exactly one reference variant")
    key = next(iter(keys))
    target = value[key]
    if not isinstance(target, dict) or not all(isinstance(name, str) for name in target):
        raise TemplateError(f"{_child_pointer(pointer, key)}: invalid {key} reference")
    try:
        if key == "fromReceipt":
            if "unit" not in target:
                raise TemplateError("fromReceipt requires unit")
            parsed = ReceiptReferenceTarget.from_dict(target)
            _validate_unit(parsed.unit, key)
            _validate_pointer(parsed.pointer)
            return ReceiptReference(fromReceipt=parsed)
        if key == "fromPromotion":
            if "unit" not in target:
                raise TemplateError("fromPromotion requires unit")
            parsed = PromotionReferenceTarget.from_dict(target)
            _validate_unit(parsed.unit, key)
            _validate_pointer(parsed.pointer)
            return PromotionReference(fromPromotion=parsed)
        if "unit" not in target or "name" not in target:
            raise TemplateError("fromArtifact requires unit and name")
        if not isinstance(target.get("apiVersion"), str) or not isinstance(target.get("kind"), str):
            raise TemplateError("fromArtifact requires string apiVersion and kind")
        parsed = ArtifactReferenceTarget.from_dict(target)
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


def parse_template_value(value: object, pointer: str = "") -> TemplateValue:
    """Parse untrusted JSON into an exhaustive authored-value expression tree."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [parse_template_value(item, _child_pointer(pointer, index)) for index, item in enumerate(value)]
    if isinstance(value, dict):
        if not all(isinstance(name, str) for name in value):
            raise TemplateError("template object keys must be strings")
        candidate = cast(dict[str, object], value)
        if REFERENCE_KEYS.intersection(candidate):
            return _parse_reference(candidate, pointer)
        return {name: parse_template_value(item, _child_pointer(pointer, name)) for name, item in candidate.items()}
    raise TemplateError(f"template value is not JSON: {type(value).__name__}")


def dump_template_value(value: TemplateValue) -> JsonValue:
    """Serialize an authored expression tree to its public JSON representation."""

    if isinstance(value, ReceiptReference):
        return cast(JsonValue, value.to_dict())
    if isinstance(value, ArtifactReference):
        return cast(JsonValue, value.to_dict())
    if isinstance(value, PromotionReference):
        return cast(JsonValue, value.to_dict())
    if isinstance(value, list):
        return [dump_template_value(item) for item in value]
    if isinstance(value, dict):
        return {name: dump_template_value(item) for name, item in value.items()}
    return value


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
