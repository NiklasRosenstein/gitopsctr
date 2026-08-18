"""Strict application-owned parsing of finalized incarnation evidence."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

import pytest

from gitopsctr.application.apply_orchestration import ApplyOrchestrationError, _finalized_tombstones
from gitopsctr.application.apply_projection import ExactPlane, FinalizedTombstone
from gitopsctr.application.model import ChannelId, HeadObservation, SnapshotId
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry

_PREFIX = ".gitopsctr/incarnations/resources"
_PATH = f"{_PREFIX}/unit.gitopsctr.io/v1/Terraform/team/app/d1-retired.json"


def _document() -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "ResourceIncarnationTombstone",
        "resource": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "name": "app",
            "uid": "d1-retired",
            "deletionGeneration": 2,
            "qualifiedName": "team/app",
            "partition": "team",
            "effectLeaseRef": "lease:retired",
        },
    }


def _plane(document: object, *, path: str = _PATH) -> ExactPlane:
    workspace = InMemoryWorkspace(
        (WorkspaceEntry.file(path, json.dumps(document).encode()),),
        mutable=False,
    )
    snapshot_id = SnapshotId("desired-snapshot")
    snapshot = SnapshotView(snapshot_id, workspace.content_id, workspace)
    return ExactPlane(
        HeadObservation.present(ChannelId("desired/dev"), snapshot_id, "desired-incarnation"),
        workspace,
        snapshot,
    )


def test_finalized_tombstone_requires_the_complete_canonical_record_and_path() -> None:
    assert _finalized_tombstones(_plane(_document())) == (
        FinalizedTombstone("unit.gitopsctr.io/v1", "Terraform", "team/app", "d1-retired"),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda value: value.pop("schema"), id="missing-schema"),
        pytest.param(lambda value: value.__setitem__("extra", True), id="extra-envelope-key"),
        pytest.param(lambda value: value.__setitem__("schema", True), id="boolean-schema"),
        pytest.param(lambda value: value.__setitem__("kind", "Other"), id="wrong-envelope-kind"),
    ],
)
def test_finalized_tombstone_rejects_malformed_envelope(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(ApplyOrchestrationError, match="invalid desired incarnation evidence"):
        _finalized_tombstones(_plane(document))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        pytest.param("apiVersion", None, id="missing-api-version"),
        pytest.param("kind", None, id="missing-kind"),
        pytest.param("name", None, id="missing-name"),
        pytest.param("uid", None, id="missing-uid"),
        pytest.param("deletionGeneration", None, id="missing-generation"),
        pytest.param("qualifiedName", None, id="missing-qualified-name"),
        pytest.param("effectLeaseRef", None, id="missing-effect-lease-ref"),
        pytest.param("name", "Bad_Name", id="invalid-name"),
        pytest.param("uid", "UPPER", id="invalid-uid"),
        pytest.param("qualifiedName", "../app", id="escaping-qualified-name"),
        pytest.param("partition", "Bad_Partition", id="invalid-partition"),
        pytest.param("deletionGeneration", True, id="boolean-generation"),
        pytest.param("deletionGeneration", 0, id="zero-generation"),
        pytest.param("effectLeaseRef", 7, id="non-text-effect-lease-ref"),
        pytest.param("effectLeaseRef", "", id="empty-effect-lease-ref"),
    ],
)
def test_finalized_tombstone_rejects_missing_or_invalid_resource_fields(
    field: str,
    replacement: object,
) -> None:
    document = _document()
    resource = document["resource"]
    assert isinstance(resource, dict)
    if replacement is None:
        resource.pop(field)
    else:
        resource[field] = replacement
    with pytest.raises(ApplyOrchestrationError, match="invalid desired incarnation evidence"):
        _finalized_tombstones(_plane(document))


def test_finalized_tombstone_rejects_unknown_resource_fields_and_path_spoofing() -> None:
    document = _document()
    resource = document["resource"]
    assert isinstance(resource, dict)
    resource["untrustedFence"] = "accepted"
    with pytest.raises(ApplyOrchestrationError, match="invalid desired incarnation evidence"):
        _finalized_tombstones(_plane(document))

    canonical = copy.deepcopy(_document())
    with pytest.raises(ApplyOrchestrationError, match="invalid desired incarnation evidence path"):
        _finalized_tombstones(_plane(canonical, path=f"{_PREFIX}/spoofed/d1-retired.json"))


def test_finalized_tombstone_directory_or_non_json_evidence_fails_closed() -> None:
    workspace = InMemoryWorkspace(
        (WorkspaceEntry.file(f"{_PREFIX}/evidence.yaml", b"{}"),),
        mutable=False,
    )
    snapshot_id = SnapshotId("desired-snapshot")
    plane = ExactPlane(
        HeadObservation.present(ChannelId("desired/dev"), snapshot_id, "desired-incarnation"),
        workspace,
        SnapshotView(snapshot_id, workspace.content_id, workspace),
    )
    with pytest.raises(ApplyOrchestrationError, match="canonical JSON"):
        _finalized_tombstones(plane)
