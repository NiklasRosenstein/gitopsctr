from __future__ import annotations

import pytest

from gitopsctr.contracts import (
    DeletionMetadata,
    DesiredOwnerReference,
    DesiredResourceMetadata,
)
from gitopsctr.resources import PARTITION_LABEL, ResourceMetadata

OWNER = DesiredOwnerReference(
    apiVersion="gitopsctr.io/v1",
    kind="Stack",
    name="application",
    uid="d1-application",
)


def test_root_metadata_may_name_an_apply_partition() -> None:
    metadata = ResourceMetadata(
        name="application",
        uid="d1-application",
        labels={"team": "platform", PARTITION_LABEL: "application"},
    )

    metadata.validate_desired()
    assert metadata.document(profile="desired") == {
        "name": "application",
        "uid": "d1-application",
        "labels": {"team": "platform", PARTITION_LABEL: "application"},
    }
    assert metadata.is_root
    assert metadata.partition == "application"
    assert not metadata.is_unpartitioned_root


def test_unpartitioned_root_has_no_reserved_label() -> None:
    metadata = ResourceMetadata.new_root("application")

    assert metadata.is_root
    assert metadata.is_unpartitioned_root
    assert metadata.partition is None
    assert "labels" not in metadata.document(profile="desired")


def test_owned_metadata_uses_one_owner_reference() -> None:
    metadata = ResourceMetadata(name="deploy", uid="d1-deploy", ownerReferences=[OWNER])

    metadata.validate_desired()
    assert metadata.document(profile="desired")["ownerReferences"] == [OWNER.to_dict()]
    assert not metadata.is_root
    with pytest.raises(ValueError, match="do not have"):
        _ = metadata.partition


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"ownerReferences": []}, "exactly one reference"),
        ({"ownerReferences": [OWNER, OWNER]}, "exactly one reference"),
        (
            {"ownerReferences": [OWNER], "labels": {PARTITION_LABEL: "application"}},
            "must not carry the partition label",
        ),
        ({"labels": {PARTITION_LABEL: "Not Valid"}}, "partition label has an invalid format"),
        ({"labels": {"": "value"}}, "label keys must not be empty"),
    ],
)
def test_desired_metadata_validates_ownership_and_labels(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DesiredResourceMetadata(name="resource", uid="d1-resource", **kwargs)


def test_deletion_metadata_requires_positive_generation_and_sha256_digest() -> None:
    deletion = DeletionMetadata(generation=1, resourceDigest="sha256:" + "a" * 64)
    assert deletion.to_dict() == {"generation": 1, "resourceDigest": "sha256:" + "a" * 64}

    with pytest.raises(ValueError, match="at least 1"):
        DeletionMetadata(generation=0, resourceDigest="sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="64 lowercase hex"):
        DeletionMetadata(generation=1, resourceDigest="sha256:" + "A" * 64)


def test_authored_metadata_serializes_as_name_only() -> None:
    assert ResourceMetadata(name="application").document(profile="authored") == {"name": "application"}

    with pytest.raises(ValueError, match="only name"):
        ResourceMetadata(name="application", labels={"team": "platform"}).document(profile="authored")


def test_partition_stamping_preserves_general_labels_and_can_retain_existing_partition() -> None:
    metadata = ResourceMetadata(name="application", uid="d1-application", labels={"team": "platform"})
    partitioned = metadata.with_partition("group-a")

    assert partitioned.labels == {"team": "platform", PARTITION_LABEL: "group-a"}
    assert partitioned.with_partition(None, preserve_existing=True) == partitioned
    assert partitioned.with_partition(None).labels == {"team": "platform"}
    assert metadata.labels == {"team": "platform"}

    owned = ResourceMetadata(name="child", uid="d1-child", ownerReferences=[OWNER])
    with pytest.raises(ValueError, match="cannot stamp"):
        owned.with_partition("group-a")


def test_root_identity_from_provenance_is_deterministic_and_partition_aware() -> None:
    first = ResourceMetadata.root_from_provenance("application", "source/revision/path", partition="group-a")
    repeated = ResourceMetadata.root_from_provenance("application", "source/revision/path", partition="group-a")

    assert first == repeated
    assert first.uid is not None and first.uid.startswith("d1-")
    assert first.partition == "group-a"


def test_lifecycle_field_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError, KeyError)):
        DesiredResourceMetadata.from_dict(
            {
                "name": "deploy",
                "uid": "d1-deploy",
                "lifecycle": {"management": {"mode": "sourceTracked"}},
            }
        )
