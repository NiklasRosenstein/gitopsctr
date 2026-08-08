"""Typed recursive desired-state template resolution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gitopsctr.document import JsonValue
from gitopsctr.errors import OperationError
from gitopsctr.templates import (
    ArtifactReference,
    ArtifactReferenceTarget,
    PromotionReference,
    PromotionReferenceTarget,
    ReceiptReference,
    ReceiptReferenceTarget,
    TemplateError,
    TemplateValue,
    parse_template_value,
)


@dataclass(frozen=True)
class FingerprintedValue:
    value: JsonValue
    fingerprint: str


@dataclass(frozen=True)
class ResolutionContext:
    receipt: Callable[[ReceiptReferenceTarget], FingerprintedValue]
    artifact: Callable[[ArtifactReferenceTarget], FingerprintedValue]
    promotion: Callable[[PromotionReferenceTarget], FingerprintedValue]


@dataclass(frozen=True)
class TemplateResolution:
    value: JsonValue
    promotions: dict[str, str]
    receipts: dict[str, str]
    artifacts: dict[str, str]


def resolve_template(value: object, context: ResolutionContext) -> TemplateResolution:
    promotions: dict[str, str] = {}
    receipts: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    try:
        expression = parse_template_value(value)
    except TemplateError as exc:
        raise OperationError(str(exc)) from exc

    def resolve(candidate: TemplateValue) -> JsonValue:
        if isinstance(candidate, list):
            return [resolve(item) for item in candidate]
        if isinstance(candidate, dict):
            return {name: resolve(item) for name, item in candidate.items()}
        if isinstance(candidate, PromotionReference):
            resolved = context.promotion(candidate.fromPromotion)
            promotions[candidate.fromPromotion.unit] = resolved.fingerprint
            return resolved.value
        if isinstance(candidate, ReceiptReference):
            resolved = context.receipt(candidate.fromReceipt)
            receipts[candidate.fromReceipt.unit] = resolved.fingerprint
            return resolved.value
        if isinstance(candidate, ArtifactReference):
            resolved = context.artifact(candidate.fromArtifact)
            artifacts[f"{candidate.fromArtifact.unit}/{candidate.fromArtifact.name}"] = resolved.fingerprint
            return resolved.value
        return candidate

    return TemplateResolution(resolve(expression), promotions, receipts, artifacts)
