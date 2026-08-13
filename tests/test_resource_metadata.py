from __future__ import annotations

import pytest

from gitopsctr.contracts import (
    DeletionMetadata,
    DesiredLifecycle,
    DesiredOwnerReference,
    DesiredResourceMetadata,
    LifecycleManagement,
)
from gitopsctr.resources import ResourceMetadata

OWNER = DesiredOwnerReference(
    apiVersion="gitopsctr.io/v1",
    kind="Stack",
    name="application",
    uid="d1-application",
)


def test_root_metadata_uses_lifecycle_management() -> None:
    metadata = ResourceMetadata(
        name="application",
        uid="d1-application",
        lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked")),
    )

    metadata.validate_desired()
    assert metadata.document(profile="desired") == {
        "name": "application",
        "uid": "d1-application",
        "lifecycle": {"management": {"mode": "sourceTracked"}},
    }


def test_owned_metadata_uses_one_owner_reference() -> None:
    metadata = ResourceMetadata(name="application--deploy", uid="d1-deploy", ownerReferences=[OWNER])

    metadata.validate_desired()
    assert metadata.document(profile="desired")["ownerReferences"] == [OWNER.to_dict()]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "exactly one"),
        (
            {"lifecycle": DesiredLifecycle(management=LifecycleManagement(mode="direct")), "ownerReferences": [OWNER]},
            "exactly one",
        ),
        ({"ownerReferences": []}, "exactly one reference"),
        ({"ownerReferences": [OWNER, OWNER]}, "exactly one reference"),
    ],
)
def test_desired_metadata_requires_one_authority(kwargs: dict[str, object], message: str) -> None:
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


def test_old_lifecycle_owner_field_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError, KeyError)):
        DesiredResourceMetadata.from_dict(
            {
                "name": "application--deploy",
                "uid": "d1-deploy",
                "lifecycle": {"owner": OWNER.to_dict()},
            }
        )
