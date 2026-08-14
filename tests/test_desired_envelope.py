from __future__ import annotations

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.resources import PARTITION_LABEL, ResourceMetadata, validate_desired_resource_graph


def authored_terraform(name: str) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": name},
        "spec": {"source": {"path": "."}},
    }


def desired_terraform(
    name: str,
    *,
    uid: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": name, "uid": uid, **(metadata or {})},
        "spec": {
            "source": {
                "path": ".",
                "revision": "a" * 40,
                "inputHash": "sha256:inputs",
                "driverVersion": controller.DRIVER_VERSIONS["terraform"],
            }
        },
    }


def test_desired_round_trip_preserves_canonical_identity_and_partition() -> None:
    document = desired_terraform(
        "infra",
        uid="d1-infra",
        metadata={"labels": {"team": "platform", PARTITION_LABEL: "application"}},
    )

    parsed = controller.parse_desired_unit_document(document, "infra")
    round_tripped = controller.serialize_unit_document(parsed)

    assert round_tripped["metadata"] == document["metadata"]
    assert parsed.metadata.is_root
    assert parsed.metadata.partition == "application"


@pytest.mark.parametrize("profile", ["authored", "desired"])
def test_unit_parsers_require_canonical_resource_envelopes(profile: str) -> None:
    parse = controller.parse_authored_unit_document if profile == "authored" else controller.parse_desired_unit_document
    with pytest.raises(OperationError, match="unit envelope requires apiVersion, kind, and metadata"):
        parse(
            {
                "name": "infra",
                "driver": "terraform",
                "source": {"path": ".", "revision": "a" * 40, "driverVersion": 2},
            },
            "infra",
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": "infra", "uid": "uid-1"},
        {"name": "infra", "labels": {PARTITION_LABEL: "application"}},
        {"name": "infra", "uid": "uid-1", "lifecycle": {"management": {"mode": "sourceTracked"}}},
    ],
)
def test_authored_metadata_rejects_controller_owned_fields(metadata: dict[str, object]) -> None:
    document = authored_terraform("infra")
    document["metadata"] = metadata

    with pytest.raises(OperationError, match="authored unit metadata may contain only name"):
        controller.parse_authored_unit_document(document, "infra")


@pytest.mark.parametrize("field", ["uid", "labels", "ownerReferences", "deletion"])
def test_desired_metadata_rejects_explicit_null_fields(field: str) -> None:
    document = desired_terraform("infra", uid="uid-infra")
    document["metadata"][field] = None  # type: ignore[index]

    with pytest.raises(OperationError, match="invalid metadata"):
        controller.parse_desired_unit_document(document, "infra")


def test_owner_uid_fencing_and_cycles_are_validated() -> None:
    owner_document = desired_terraform(
        "owner",
        uid="uid-owner",
        metadata={"labels": {PARTITION_LABEL: "application"}},
    )
    child_document = desired_terraform(
        "child",
        uid="uid-child",
        metadata={
            "ownerReferences": [
                {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "owner",
                    "uid": "uid-owner",
                }
            ]
        },
    )
    owner_resource = controller.parse_desired_unit_document(owner_document, "owner")
    child_resource = controller.parse_desired_unit_document(child_document, "child")
    validate_desired_resource_graph(
        {
            ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
            ("unit.gitopsctr.io/v1", "Terraform", "child"): child_resource,
        }
    )

    bad_child = desired_terraform(
        "child",
        uid="uid-child",
        metadata={
            "ownerReferences": [
                {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "owner",
                    "uid": "uid-wrong",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="different UID"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
                ("unit.gitopsctr.io/v1", "Terraform", "child"): controller.parse_desired_unit_document(
                    bad_child, "child"
                ),
            }
        )

    cycle = desired_terraform(
        "child",
        uid="uid-child",
        metadata={
            "ownerReferences": [
                {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "child",
                    "uid": "uid-child",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="acyclic"):
        validate_desired_resource_graph(
            {("unit.gitopsctr.io/v1", "Terraform", "child"): controller.parse_desired_unit_document(cycle, "child")}
        )

    with pytest.raises(ValueError, match="mapping key"):
        validate_desired_resource_graph({("wrong", "Terraform", "owner"): owner_resource})

    duplicate_owner = owner_resource.with_metadata(ResourceMetadata.new_root("owner"))
    with pytest.raises(ValueError, match="duplicate desired resource identity"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
                ("wrong", "Terraform", "duplicate"): duplicate_owner,
            }
        )
