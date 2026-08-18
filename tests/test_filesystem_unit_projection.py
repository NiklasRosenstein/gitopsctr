"""Acceptance coverage for the real filesystem Unit projection host."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from gitopsctr.adapters.filesystem.unit_projection import FilesystemUnitProjectionError, FilesystemUnitProjectionHost
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelector
from gitopsctr.application.apply_compilers import CatalogLogicalUnitProjector, UnitProjectionRequest
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    HmacRootIncarnationIssuer,
    ProjectedDocument,
    RetainedSourcePlane,
    SourceBindingRole,
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
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, ResourceMetadata

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
HOST = FilesystemUnitProjectionHost(CATALOG)


def _source(*entries: WorkspaceEntry) -> RetainedSourcePlane:
    workspace = InMemoryWorkspace(entries, mutable=False)
    snapshot = SnapshotId("source-snapshot")
    retained = _issue_retained_source(
        RetainedSourceHandle("retained-source"),
        RetentionStoreId("retention-store"),
        SourceSnapshotId(SourceId("source"), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "workload-source",
        SourceBindingRole.WORKLOAD,
        "charts/web/manifest.yaml",
        ContentId("sha256:" + "a" * 64),
    )
    return RetainedSourcePlane(
        retained,
        ExactPlane(
            HeadObservation.present(ChannelId("source"), snapshot, "source-incarnation"),
            workspace,
            SnapshotView(snapshot, workspace.content_id, workspace),
        ),
        (descriptor,),
    )


def _terraform() -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": "web"},
        "spec": {"source": {"path": "."}},
    }


def _kubernetes(*, source_path: str = "charts/web") -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "KubernetesManifests",
        "metadata": {"name": "web"},
        "spec": {
            "source": {"path": source_path},
            "materialize": {"type": "plain", "paths": ["*.yaml"]},
            "delivery": {"mode": "external"},
        },
    }


def _kubernetes_with_revision() -> dict[str, object]:
    document = _kubernetes()
    specification = document["spec"]
    assert isinstance(specification, dict)
    source = specification["source"]
    assert isinstance(source, dict)
    source["revision"] = "a" * 40
    return document


def _request(
    document: dict[str, object],
    source: RetainedSourcePlane,
    *,
    current: InMemoryWorkspace | None = None,
    previous: ProjectedDocument | None = None,
    qualified_name: str = "web",
) -> UnitProjectionRequest:
    unit = CATALOG.parse_unit(cast(JsonObject, document), profile="authored")
    return UnitProjectionRequest(
        unit,
        ResourceMetadata(name="web", uid="d1-web"),
        previous,
        current or InMemoryWorkspace(mutable=False),
        DesiredSource(path=cast_source_path(document), revision="a" * 40, inputHash="sha256:" + "b" * 64),
        source,
        qualified_name,
        EnvironmentId("dev"),
        lambda _value, _pointer: pytest.fail("fixture did not expect template resolution"),
    )


def cast_source_path(document: dict[str, object]) -> str:
    specification = document["spec"]
    assert isinstance(specification, dict)
    source = specification["source"]
    assert isinstance(source, dict) and isinstance(source["path"], str)
    return source["path"]


def _previous(desired) -> ProjectedDocument:  # type: ignore[no-untyped-def]
    document = CATALOG.serialize_unit(desired, profile="desired")
    return ProjectedDocument("units/web.json", document)


def test_non_materialized_driver_returns_only_a_typed_desired_unit() -> None:
    source = _source(WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\n"))
    result = HOST.project(_request(_terraform(), source))

    assert result.payload_writes == ()
    assert result.payload_replacements == ()
    assert result.unit.metadata.uid == "d1-web"
    assert result.unit.driver_name == "terraform"


def test_builtin_materializer_emits_exact_entries_and_reuses_a_valid_payload() -> None:
    manifest = b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n"
    source = _source(WorkspaceEntry.file("charts/web/manifest.yaml", manifest, executable=True))
    first = HOST.project(_request(_kubernetes(), source))

    assert len(first.payload_replacements) == 1
    replacement = first.payload_replacements[0]
    assert replacement.prefix == "materialized/web"
    assert replacement.entries == (WorkspaceEntry.file("materialized/web/manifest.yaml", manifest, executable=True),)
    payload = InMemoryWorkspace(replacement.entries, mutable=False)
    reused = HOST.project(_request(_kubernetes(), source, current=payload, previous=_previous(first.unit)))
    assert reused.payload_replacements == ()
    assert reused.unit.spec.materialization == first.unit.spec.materialization


@pytest.mark.parametrize(
    "entries",
    (
        (),
        (WorkspaceEntry.file("materialized/web/manifest.yaml", b"corrupt"),),
    ),
)
def test_corrupt_or_missing_materialization_is_not_reused(entries: tuple[WorkspaceEntry, ...]) -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )
    first = HOST.project(_request(_kubernetes(), source))
    retry = HOST.project(
        _request(
            _kubernetes(), source, current=InMemoryWorkspace(entries, mutable=False), previous=_previous(first.unit)
        )
    )
    assert len(retry.payload_replacements) == 1


def test_rematerialization_replaces_the_complete_payload_subtree() -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )
    first = HOST.project(_request(_kubernetes(), source))
    current = InMemoryWorkspace(
        (*first.payload_replacements[0].entries, WorkspaceEntry.file("materialized/web/stale.yaml", b"stale")),
        mutable=False,
    )
    rematerialized = HOST.project(_request(_kubernetes(), source, current=current, previous=_previous(first.unit)))
    replacement = rematerialized.payload_replacements[0]
    assert {entry.key for entry in replacement.entries} == {"materialized/web/manifest.yaml"}
    assert {key for key, _content_id in replacement.expected_current_entries} == {
        "materialized/web/manifest.yaml",
        "materialized/web/stale.yaml",
    }


@pytest.mark.parametrize("source_path,qualified_name", (("../escape", "web"), ("charts/web", "../escape")))
def test_escaping_source_or_materialized_paths_fail_closed(source_path: str, qualified_name: str) -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )
    with pytest.raises(FilesystemUnitProjectionError):
        HOST.project(_request(_kubernetes(source_path=source_path), source, qualified_name=qualified_name))


def test_materializer_failure_returns_no_partial_projection_delta() -> None:
    source = _source(WorkspaceEntry.file("charts/web/manifest.yaml", b"not: [valid"))
    with pytest.raises(FilesystemUnitProjectionError):
        HOST.project(_request(_kubernetes(), source))


def test_host_rejects_missing_source_evidence_and_invalid_driver_resolution() -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )
    request = _request(_kubernetes(), source)
    with pytest.raises(FilesystemUnitProjectionError, match="exact retained source"):
        HOST.project(replace(request, selected_source=None))
    with pytest.raises(FilesystemUnitProjectionError, match="requires a source"):
        HOST.project(replace(request, source=None))


def test_reuse_refuses_wrong_prior_or_changed_resolved_model() -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n"),
        WorkspaceEntry.file("other/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: other\n"),
    )
    first = HOST.project(_request(_kubernetes(), source))
    current = InMemoryWorkspace(first.payload_replacements[0].entries, mutable=False)
    terraform_prior = _previous(HOST.project(_request(_terraform(), source)).unit)
    assert HOST.project(_request(_kubernetes(), source, current=current, previous=terraform_prior)).payload_replacements
    changed = _request(_kubernetes(source_path="other"), source, current=current, previous=_previous(first.unit))
    assert HOST.project(changed).payload_replacements


def test_unparseable_previous_document_is_a_closed_reuse_failure() -> None:
    source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )

    class BadPrevious:
        def mutable_document(self):  # type: ignore[no-untyped-def]
            return {}

    with pytest.raises(FilesystemUnitProjectionError, match="cannot be parsed"):
        HOST.project(_request(_kubernetes(), source, previous=cast(ProjectedDocument, BadPrevious())))


def test_catalog_logical_projector_uses_real_selector_hasher_and_filesystem_payload_host() -> None:
    # The production Git bridge requires an adapter-issued git-source snapshot.
    git_source = _source(
        WorkspaceEntry.file("charts/web/manifest.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n")
    )
    retained = _issue_retained_source(
        RetainedSourceHandle("git-retained"),
        RetentionStoreId("git-store"),
        SourceSnapshotId(SourceId("source"), SnapshotId("git-source:" + "a" * 40)),
        git_source.plane.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "workload-source",
        SourceBindingRole.WORKLOAD,
        "charts/web/manifest.yaml",
        ContentId("sha256:" + "a" * 64),
    )
    git_plane = RetainedSourcePlane(
        retained,
        ExactPlane(
            HeadObservation.present(
                ChannelId("source-git"), retained.source_snapshot_id.snapshot_id, "git-incarnation"
            ),
            git_source.plane.workspace,
            SnapshotView(
                retained.source_snapshot_id.snapshot_id, git_source.plane.content_id, git_source.plane.workspace
            ),
        ),
        (descriptor,),
    )
    unit = CATALOG.parse_unit(cast(JsonObject, _kubernetes_with_revision()), profile="authored")
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        None,
        ApplyProjectionPolicy(),
        named_sources=(descriptor,),
        root_identity_issuer=HmacRootIncarnationIssuer("test-issuer", "test-seed"),
    )
    projector = CatalogLogicalUnitProjector(
        CATALOG,
        GitSourceLineageEncoder({SourceId("source"): "repo"}),
        HOST,
        source_selector=GitUnitSourceSelector(
            GitSourceLineageEncoder({SourceId("source"): "repo"}), {"web": "workload-source"}
        ),
    )
    projected = projector.project_unit(
        unit,
        metadata=ResourceMetadata(name="web", uid="d1-web"),
        previous=None,
        current_workspace=InMemoryWorkspace(mutable=False),
        retained_sources=(git_plane,),
        observed=InMemoryWorkspace(mutable=False),
        context=context,
    )
    assert projected.payload_replacements[0].entries[0].key == "materialized/web/manifest.yaml"
