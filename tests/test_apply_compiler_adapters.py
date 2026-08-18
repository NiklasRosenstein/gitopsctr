"""Production Git adapter boundary tests for controller-free projection."""

from __future__ import annotations

import pytest

from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder, GitPromotionLineageError
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder, GitSourceLineageError
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelectionError, GitUnitSourceSelector
from gitopsctr.application.apply_compilers import UnitSourceSelectionRequest
from gitopsctr.application.apply_projection import (
    ExactPlane,
    RetainedSourcePlane,
    SourceBindingRole,
    _issue_promotion_source_descriptor,
    _issue_retained_source_descriptor,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
    _issue_retained_source,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.contracts import DesiredSource
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resources import ResourceCatalog, ResourceMetadata, UnitResource

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
SHA = "a" * 40


def _plane(channel: str, snapshot: str) -> ExactPlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("source.yaml", b"value"),), mutable=False)
    snapshot_id = SnapshotId(snapshot)
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot_id, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot_id, workspace.content_id, workspace),
    )


def _retained(*, snapshot: str = f"git-source:{SHA}", role: SourceBindingRole = SourceBindingRole.WORKLOAD):
    plane = _plane("source", snapshot)
    retained = _issue_retained_source(
        RetainedSourceHandle("retained"),
        RetentionStoreId("store"),
        SourceSnapshotId(SourceId("source"), SnapshotId(snapshot)),
        plane.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained, "workload", role, "source.yaml", ContentId("sha256:" + "a" * 64)
    )
    return RetainedSourcePlane(retained, plane, (descriptor,))


def _unit() -> UnitResource:
    return CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "web"},
            "spec": {"source": {"path": "."}},
        },
        profile="authored",
    ).with_metadata(ResourceMetadata(name="web", uid="d1-web"))


def test_git_source_lineage_requires_exact_bound_git_source_evidence() -> None:
    plane = _retained()
    encoder = GitSourceLineageEncoder({SourceId("source"): "https://example.invalid/source.git"})
    assert encoder.encode(plane.descriptors[0], plane).revision == SHA
    with pytest.raises(GitSourceLineageError, match="no configured"):
        GitSourceLineageEncoder({}).encode(plane.descriptors[0], plane)
    wrong_snapshot = _retained(snapshot="opaque-snapshot")
    with pytest.raises(GitSourceLineageError, match="exact git-source"):
        encoder.encode(wrong_snapshot.descriptors[0], wrong_snapshot)
    with pytest.raises(GitSourceLineageError, match="not bound"):
        encoder.encode(plane.descriptors[0], wrong_snapshot)


def test_git_unit_selector_fences_revision_binding_and_ambiguity() -> None:
    plane = _retained()
    selector = GitUnitSourceSelector(GitSourceLineageEncoder({SourceId("source"): "repo"}), {"web": "workload"})
    request = UnitSourceSelectionRequest("web", _unit(), {"revision": SHA}, None, None, plane.descriptors, (plane,))
    assert selector.select(request).plane is plane
    with pytest.raises(GitUnitSourceSelectionError, match="exact requested"):
        selector.select(UnitSourceSelectionRequest("web", _unit(), {}, None, None, plane.descriptors, (plane,)))
    with pytest.raises(GitUnitSourceSelectionError, match="configured workload"):
        GitUnitSourceSelector(GitSourceLineageEncoder({SourceId("source"): "repo"}), {}).select(request)
    ambiguous = UnitSourceSelectionRequest(
        "web", _unit(), {"revision": SHA}, None, None, plane.descriptors * 2, (plane,)
    )
    with pytest.raises(GitUnitSourceSelectionError, match="ambiguous"):
        selector.select(ambiguous)
    prior = UnitSourceSelectionRequest(
        "web",
        _unit(),
        {},
        DesiredSource(path=".", revision=SHA, inputHash="sha256:" + "b" * 64),
        None,
        plane.descriptors,
        (plane,),
    )
    assert selector.select(prior).descriptor is plane.descriptors[0]


def test_git_promotion_lineage_enforces_policy_refs_and_exact_snapshot_shapes() -> None:
    descriptor = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        _plane("desired/staging", "git-commit:" + "1" * 40),
        _plane("observed/staging", "git-commit:" + "2" * 40),
        _plane("desired/dev", "git-commit:" + "3" * 40),
        _plane("observed/dev", "git-commit:" + "4" * 40),
        ContentId("sha256:" + "c" * 64),
    )
    encoder = GitPromotionLineageEncoder(
        {
            ChannelId("desired/staging"): "desired/staging",
            ChannelId("desired/dev"): "desired/dev",
        },
        {
            ChannelId("observed/staging"): "observed/staging",
            ChannelId("observed/dev"): "observed/dev",
        },
        {EnvironmentId("dev"): {EnvironmentId("staging")}},
    )
    assert encoder.encode(descriptor).source_desired_revision == "1" * 40
    with pytest.raises(GitPromotionLineageError, match="not allowed"):
        GitPromotionLineageEncoder({}, {}, {}).encode(descriptor)
    with pytest.raises(GitPromotionLineageError, match="invalid ref"):
        GitPromotionLineageEncoder({ChannelId("desired/staging"): "bad..ref"}, {}, {})
    missing_ref = GitPromotionLineageEncoder({}, {}, {EnvironmentId("dev"): {EnvironmentId("staging")}})
    with pytest.raises(GitPromotionLineageError, match="no source desired"):
        missing_ref.encode(descriptor)
