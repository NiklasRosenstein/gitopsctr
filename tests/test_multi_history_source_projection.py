"""End-to-end source identity coverage for Stack-owned Units."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelector
from gitopsctr.application.apply import AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    UnitProjection,
    UnitProjectionRequest,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    HmacRootIncarnationIssuer,
    RetainedSourcePlane,
    SourceBindingRole,
    WorkspaceProjectionContext,
    _issue_retained_source_descriptor,
    project_apply,
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
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, UnitResource

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
HISTORY_REVISION = "a" * 40
CURRENT_REVISION = "b" * 40


def _plane(channel: str, snapshot: SnapshotId, workspace: InMemoryWorkspace) -> ExactPlane:
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _source(binding: str, source_id: str, revision: str) -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("main.tf", f"{binding}:{revision}".encode()),), mutable=False)
    snapshot = SnapshotId(f"git-source:{revision}")
    retained = _issue_retained_source(
        RetainedSourceHandle(f"retained-{source_id}"),
        RetentionStoreId(f"store-{source_id}"),
        SourceSnapshotId(SourceId(source_id), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        binding,
        SourceBindingRole.WORKLOAD,
        f"workloads/{binding}",
        ContentId(f"selector-{source_id}"),
    )
    return RetainedSourcePlane(retained, _plane(f"source/{source_id}", snapshot, workspace), (descriptor,))


def _primary_source() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("template.yaml", b"kind: StackTemplate\n"),), mutable=False)
    snapshot = SnapshotId(f"git-source:{CURRENT_REVISION}")
    retained = _issue_retained_source(
        RetainedSourceHandle("retained-authored"),
        RetentionStoreId("store-authored"),
        SourceSnapshotId(SourceId("authored"), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "authored-primary",
        SourceBindingRole.PRIMARY_AUTHORED,
        "template.yaml",
        ContentId("selector-authored"),
    )
    return RetainedSourcePlane(retained, _plane("source/authored", snapshot, workspace), (descriptor,))


def _document(name: str, kind: str, spec: dict[str, object]) -> JsonObject:
    return cast(
        JsonObject,
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": kind,
            "metadata": {"name": name},
            "spec": spec,
        },
    )


@dataclass
class _CapturingHost:
    selected_sources: dict[str, SourceId] = field(default_factory=dict)

    def project(self, request: UnitProjectionRequest) -> UnitProjection:
        assert request.source is not None and request.selected_source is not None
        self.selected_sources[request.qualified_name] = request.selected_source.retained.source_snapshot_id.source_id
        return UnitProjection(
            UnitResource(
                request.unit.gvk,
                request.metadata,
                request.unit.driver,
                TerraformDesiredUnit(source=request.source),
            )
        )


def test_project_apply_stack_children_select_independent_qualified_historical_source_bindings() -> None:
    one_history = _source("one-workload", "one-history", HISTORY_REVISION)
    one_current = _source("one-workload", "one-current", CURRENT_REVISION)
    two_current = _source("two-workload", "two-current", CURRENT_REVISION)
    primary = _primary_source()
    sources = (primary, one_history, one_current, two_current)
    named = tuple(plane.descriptors[0] for plane in sources if plane is not primary)
    encoder = GitSourceLineageEncoder(
        {
            SourceId("one-history"): "https://example.test/one-history.git",
            SourceId("one-current"): "https://example.test/one-current.git",
            SourceId("two-current"): "https://example.test/two-current.git",
            SourceId("authored"): "https://example.test/authored.git",
        }
    )
    host = _CapturingHost()
    logical_projector = CatalogLogicalUnitProjector(
        CATALOG,
        encoder,
        host,
        source_selector=GitUnitSourceSelector(
            encoder,
            {"one/db": "one-workload", "two/db": "two-workload"},
        ),
    )
    stack_compiler = CatalogStackProjectionCompiler(CATALOG, logical_projector, source_encoder=encoder)
    template = _document(
        "shared",
        "StackTemplate",
        {
            "parameters": [],
            "unitTemplates": {
                "db": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": ".", "revision": CURRENT_REVISION, "inputs": ["**"]}},
                }
            },
        },
    )
    changes = AuthoredChangeSet(
        (
            _issue_authored_document("template", template, ContentId("sha256:" + "1" * 64)),
            _issue_authored_document(
                "stack-one", _document("one", "Stack", {"template": "shared"}), ContentId("sha256:" + "2" * 64)
            ),
            _issue_authored_document(
                "stack-two", _document("two", "Stack", {"template": "shared"}), ContentId("sha256:" + "3" * 64)
            ),
        ),
        primary.retained.source_snapshot_id,
    )
    desired_workspace = InMemoryWorkspace(mutable=False)
    observed_workspace = InMemoryWorkspace(mutable=False)
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(b"kind: Project\n", b"kind: Environment\n"),
        primary_source=primary.descriptors[0],
        named_sources=named,
        root_identity_issuer=HmacRootIncarnationIssuer("multi-history", "multi-history-seed"),
    )

    result = project_apply(
        changes,
        current_desired=_plane("desired/dev", SnapshotId("desired-empty"), desired_workspace),
        observed=_plane("observed/dev", SnapshotId("observed-empty"), observed_workspace),
        retained_sources=sources,
        context=context,
        validator=CatalogApplyDocumentValidator(CATALOG),
        stack_compiler=stack_compiler,
    )

    assert host.selected_sources == {"one/db": SourceId("one-current"), "two/db": SourceId("two-current")}
    assert {entry.key for entry in result.candidate.list_entries() if entry.key.startswith("units/")} == {
        "units/one/db.json",
        "units/two/db.json",
    }
