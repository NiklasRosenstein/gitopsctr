"""Typed recursive desired-state template resolution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from gitopsctr.document import JsonValue
from gitopsctr.errors import OperationError, ReferenceUnavailable
from gitopsctr.templates import (
    ArtifactReference,
    ArtifactReferenceTarget,
    EnvironmentReference,
    PromotionReference,
    ReceiptReference,
    ReceiptReferenceTarget,
    TemplateError,
    TemplateValue,
    child_pointer,
    has_dry_fallback,
    parse_template_value,
)


@dataclass(frozen=True)
class FingerprintedValue:
    value: JsonValue
    fingerprint: str
    imported: bool = False


@dataclass(frozen=True)
class PromotionReferenceSelection:
    """The effective source selection for a promotion reference."""

    unit: str
    pointer: str
    pointer_inferred: bool


@dataclass(frozen=True)
class ResolutionContext:
    receipt: Callable[[ReceiptReferenceTarget], FingerprintedValue]
    artifact: Callable[[ArtifactReferenceTarget], FingerprintedValue]
    promotion: Callable[[PromotionReferenceSelection], FingerprintedValue]
    environment: Callable[[str], FingerprintedValue] | None = None
    unit: str | None = None
    dry: bool = False


@dataclass(frozen=True)
class TemplateResolution:
    value: JsonValue
    promotions: dict[str, str]
    receipts: dict[str, str]
    artifacts: dict[str, str]
    imported_artifacts: dict[str, str]


def resolve_template(value: object, context: ResolutionContext, pointer: str = "") -> TemplateResolution:
    promotions: dict[str, str] = {}
    receipts: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    imported_artifacts: dict[str, str] = {}
    try:
        expression = parse_template_value(value, pointer)
    except TemplateError as exc:
        raise OperationError(str(exc)) from exc

    def resolve(candidate: TemplateValue, location: str) -> JsonValue:
        if isinstance(candidate, list):
            return [resolve(item, child_pointer(location, index)) for index, item in enumerate(candidate)]
        if isinstance(candidate, dict):
            return {name: resolve(item, child_pointer(location, name)) for name, item in candidate.items()}
        if isinstance(candidate, PromotionReference):
            target = candidate.fromPromotion
            unit = target.unit or context.unit
            if unit is None:
                raise OperationError(f"{location or '/'}: implicit fromPromotion unit requires a target unit")
            selection = PromotionReferenceSelection(
                unit=unit,
                pointer=location if target.pointer is None else target.pointer,
                pointer_inferred=target.pointer is None,
            )
            try:
                resolved = context.promotion(selection)
            except ReferenceUnavailable:
                if context.dry and has_dry_fallback(target):
                    return resolve(parse_template_value(target.dryFallback.value, location), location)
                raise
            promotions[f"{selection.unit}#{selection.pointer}"] = resolved.fingerprint
            return resolved.value
        if isinstance(candidate, EnvironmentReference):
            if context.environment is None:
                raise OperationError(f"{location or '/'}: fromEnvironment is unavailable in this context")
            resolved = context.environment(candidate.fromEnvironment.pointer)
            return resolved.value
        if isinstance(candidate, ReceiptReference):
            try:
                resolved = context.receipt(candidate.fromReceipt)
            except ReferenceUnavailable:
                if context.dry and has_dry_fallback(candidate.fromReceipt):
                    return resolve(parse_template_value(candidate.fromReceipt.dryFallback.value, location), location)
                raise
            receipts[candidate.fromReceipt.unit] = resolved.fingerprint
            return resolved.value
        if isinstance(candidate, ArtifactReference):
            try:
                resolved = context.artifact(candidate.fromArtifact)
            except ReferenceUnavailable:
                if context.dry and has_dry_fallback(candidate.fromArtifact):
                    return resolve(parse_template_value(candidate.fromArtifact.dryFallback.value, location), location)
                raise
            key = f"{candidate.fromArtifact.unit}/{candidate.fromArtifact.name}"
            if resolved.imported:
                imported_artifacts[key] = resolved.fingerprint
            else:
                artifacts[key] = resolved.fingerprint
            return resolved.value
        return cast(JsonValue, candidate)

    return TemplateResolution(resolve(expression, pointer), promotions, receipts, artifacts, imported_artifacts)
