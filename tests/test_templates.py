import pytest

from gitopsctr.document import ResolvedJsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.resolution import FingerprintedValue, ResolutionContext, resolve_template
from gitopsctr.templates import (
    ArtifactReference,
    PromotionReference,
    ReceiptReference,
    TemplateError,
    dump_template_value,
    parse_template_value,
    references,
)


def test_template_values_round_trip_nested_fixed_values_and_every_reference_variant():
    document = {
        "enabled": True,
        "replicas": 2,
        "nested": [
            {"fromReceipt": {"unit": "database", "pointer": "/outputs/url"}},
            {
                "fromArtifact": {
                    "unit": "images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "OciImages",
                    "pointer": "/images/web/digest",
                }
            },
            {"fromPromotion": {"unit": "frontend", "pointer": "/source/revision"}},
            {"fixed": [None, 3.5, "value"]},
        ],
    }

    parsed = parse_template_value(document)

    assert dump_template_value(parsed) == document
    assert tuple(type(reference) for reference in references(parsed)) == (
        ReceiptReference,
        ArtifactReference,
        PromotionReference,
    )


@pytest.mark.parametrize(
    "document, message",
    [
        ({"fromReceipt": {"unit": "db"}, "fromPromotion": {"unit": "db"}}, "exactly one"),
        ({"fromReceipt": {"unit": "db"}, "fixed": True}, "exactly one"),
        ({"fromReceipt": {"unit": "db", "dryFallback": "value"}}, "dryFallback"),
        ({"fromReceipt": {"unit": "db", "pointer": "outputs/url"}}, "must start"),
        ({"fromReceipt": {"unit": "db", "pointer": "/bad~2escape"}}, "invalid escape"),
        (
            {
                "fromArtifact": {
                    "unit": "images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "not/a/kind",
                }
            },
            "kind",
        ),
    ],
)
def test_template_values_reject_invalid_or_mixed_reference_expressions(document, message):
    with pytest.raises(TemplateError, match=message):
        parse_template_value({"nested": document}, "/spec/inputs")


def test_resolution_returns_separate_fingerprints_for_each_input_class():
    document = [
        {"fromReceipt": {"unit": "database", "pointer": "/url"}},
        {
            "fromArtifact": {
                "unit": "images",
                "name": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "OciImages",
            }
        },
        {"fromPromotion": {"unit": "frontend"}},
    ]
    result = resolve_template(
        document,
        ResolutionContext(
            receipt=lambda reference: FingerprintedValue(f"receipt:{reference.pointer}", "receipt-sha"),
            artifact=lambda reference: FingerprintedValue(reference.gvk.kind, "artifact-sha"),
            promotion=lambda reference: FingerprintedValue(reference.unit, "promotion-sha"),
        ),
    )

    assert result.value == ["receipt:/url", "OciImages", "frontend"]
    assert result.receipts == {"database": "receipt-sha"}
    assert result.artifacts == {"images/containers": "artifact-sha"}
    assert result.promotions == {"frontend": "promotion-sha"}


def test_resolution_wraps_invalid_authored_values_as_operation_errors():
    context = ResolutionContext(
        receipt=lambda _reference: FingerprintedValue(None, "unused"),
        artifact=lambda _reference: FingerprintedValue(None, "unused"),
        promotion=lambda _reference: FingerprintedValue(None, "unused"),
    )
    with pytest.raises(OperationError, match="dryFallback"):
        resolve_template({"fromReceipt": {"unit": "db", "dryFallback": None}}, context)


def test_resolved_json_rejects_authored_reference_shapes_recursively():
    with pytest.raises(ValueError, match="authored reference"):
        ResolvedJsonObjectValue._deserialize({"nested": [{"fromReceipt": {"unit": "db"}}]})
