"""Conformance checks for logical collection discovery across snapshot backends."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import cast

import pytest

from gitopsctr.adapters.git import GitSnapshotReader
from gitopsctr.adapters.git.workspace_inspection import inspect_workspace_provider
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneSession
from gitopsctr.application.inspection import InspectionOutputFormat, ResourceInspectionCommand
from gitopsctr.application.model import ContentId, SnapshotId
from gitopsctr.application.status import StatusCommand
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceCapabilities, WorkspaceEntry
from gitopsctr.errors import OperationError
from gitopsctr.formats import validate_project_document
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import ResourcePlane
from gitopsctr.workspace_collections import WorkspaceCollectionReadContext, discover_workspace_collection
from gitopsctr.workspace_inspection import WorkspaceSnapshot
from gitopsctr.workspace_status import _status_entries, status_workspace_provider
from tests import test_inventory as inventory_support

pytest_plugins = ("tests.test_inventory",)


def _unit_discovery(workspace, project):
    family = RESOURCE_REGISTRY.family("unit")
    placement = next(item for item in family.placements if item.plane is ResourcePlane.DESIRED)
    collection = RESOURCE_REGISTRY.collection(placement.collection)
    return discover_workspace_collection(
        collection,
        WorkspaceCollectionReadContext(
            workspace,
            project,
            "dev",
            family,
            placement,
            RESOURCE_REGISTRY.api_kinds,
            RESOURCE_REGISTRY.contracts_for(family.name, placement.contract_profile),
        ),
    )


def _observable_records(records):
    return tuple(
        (
            record.path,
            record.document,
            record.gvk,
            record.name,
            record.content_id,
            record.content_digest,
            record.media_type,
            record.local_identity,
            record.storage_qualified_name,
        )
        for record in records
    )


def test_git_and_memory_workspaces_discover_identical_collection_records(repository) -> None:
    reader = GitSnapshotReader.from_path(repository)
    planes = GitWorkspacePlaneSession(repository, reader)
    snapshot = planes.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev")
    memory = InMemoryWorkspace(
        snapshot.workspace.list_entries(), capabilities=snapshot.workspace.capabilities, mutable=False
    )

    git_records = _unit_discovery(snapshot.workspace, planes.project())
    memory_records = _unit_discovery(memory, planes.project())

    assert _observable_records(memory_records) == _observable_records(git_records)
    assert all(record.path == PurePosixPath(record.path.as_posix()) for record in git_records)


class MemoryPlaneProvider:
    """Independent conformance provider; it owns neither Git state nor Git blobs."""

    def __init__(
        self,
        project,
        source: InMemoryWorkspace,
        snapshots: dict[tuple[ResourcePlane, str, str | None], WorkspaceSnapshot],
    ):
        self._project = project
        self._source = source
        self._snapshots = snapshots

    def close(self) -> None:
        pass

    def project(self):
        return self._project

    def source(self) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(ResourcePlane.SOURCE, None, None, None, self._source, self._source.entry_content_ids())

    def snapshot(
        self,
        plane: ResourcePlane,
        reference: str,
        revision: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> WorkspaceSnapshot:
        selected = self._snapshots.get((plane, reference, revision))
        if revision is None:
            selected = self._snapshots.get((plane, reference, None))
        elif selected is None:
            current = self._snapshots.get((plane, reference, None))
            selected = current if current is not None and current.revision == revision else None
        if selected is None:
            if allow_missing and revision is None:
                empty = InMemoryWorkspace(mutable=False)
                return WorkspaceSnapshot(plane, reference, None, None, empty, empty.entry_content_ids())
            raise OperationError(f"{plane} ref {reference!r} does not exist")
        return selected


def _memory_workspace(documents: dict[str, dict[str, object]]) -> InMemoryWorkspace:
    return InMemoryWorkspace(
        tuple(WorkspaceEntry.file(key, json.dumps(value).encode()) for key, value in documents.items()), mutable=False
    )


def test_workspace_snapshot_rejects_inconsistent_or_mutable_content() -> None:
    immutable = _memory_workspace({"units/application.json": inventory_support.desired_terraform("application")})
    values = dict(immutable.entry_content_ids())
    snapshot = WorkspaceSnapshot(
        ResourcePlane.DESIRED,
        "desired/dev",
        "revision",
        SnapshotId("snapshot"),
        immutable,
        values,
    )
    values["units/changed.json"] = ContentId("sha256:" + "b" * 64)
    assert dict(snapshot.content_ids) == dict(immutable.entry_content_ids())

    with pytest.raises(TypeError, match="ResourcePlane"):
        WorkspaceSnapshot(
            cast(ResourcePlane, "desired"), "desired/dev", "revision", SnapshotId("snapshot"), immutable, {}
        )
    with pytest.raises(TypeError, match="ImmutableWorkspace"):
        WorkspaceSnapshot(
            ResourcePlane.DESIRED,
            "desired/dev",
            "revision",
            SnapshotId("snapshot"),
            cast(InMemoryWorkspace, object()),
            {},
        )
    with pytest.raises(ValueError, match="immutable"):
        WorkspaceSnapshot(
            ResourcePlane.DESIRED,
            "desired/dev",
            "revision",
            SnapshotId("snapshot"),
            InMemoryWorkspace(),
            {},
        )
    with pytest.raises(ValueError, match="both revision and snapshot_id"):
        WorkspaceSnapshot(
            ResourcePlane.DESIRED, "desired/dev", None, SnapshotId("snapshot"), immutable, immutable.entry_content_ids()
        )
    with pytest.raises(ValueError, match="source"):
        WorkspaceSnapshot(ResourcePlane.SOURCE, "source", None, None, immutable, immutable.entry_content_ids())
    with pytest.raises(ValueError, match="logical file"):
        WorkspaceSnapshot(
            ResourcePlane.DESIRED,
            "desired/dev",
            "revision",
            SnapshotId("snapshot"),
            immutable,
            {"missing.json": ContentId("sha256:" + "a" * 64)},
        )
    with pytest.raises(ValueError, match="empty content"):
        WorkspaceSnapshot(
            ResourcePlane.DESIRED,
            "desired/dev",
            None,
            None,
            immutable,
            immutable.entry_content_ids(),
        )
    for nonempty in (
        InMemoryWorkspace((WorkspaceEntry.directory("ghost"),), mutable=False),
        InMemoryWorkspace(
            (WorkspaceEntry.symlink("ghost", "target"),),
            capabilities=WorkspaceCapabilities(symlinks=True),
            mutable=False,
        ),
    ):
        with pytest.raises(ValueError, match="empty content"):
            WorkspaceSnapshot(
                ResourcePlane.OBSERVED, "observed/dev", None, None, nonempty, nonempty.entry_content_ids()
            )


def _memory_provider(*, duplicate: bool = False, missing_default: bool = False) -> MemoryPlaneProvider:
    project = validate_project_document(inventory_support.project_document(), Path("gitopsctr.yaml"))
    environment = inventory_support.environment_document("dev")
    if missing_default:
        environment["spec"] = {"refs": {"desired": "missing-desired", "observed": "missing-observed"}}
    source = _memory_workspace(
        {
            "gitopsctr.yaml": inventory_support.project_document(),
            "deployment/environments/dev/environment.json": environment,
        }
    )

    desired = inventory_support.desired_terraform("application")
    desired_documents = {"units/application.json": desired}
    if duplicate:
        desired_documents["units/application.yaml"] = desired
    desired_workspace = _memory_workspace(desired_documents)
    receipt = inventory_support.receipt(
        "application", str(desired_workspace.entry_content_ids()["units/application.json"])
    )
    observed_workspace = _memory_workspace({"units/application.json": receipt})
    return MemoryPlaneProvider(
        project,
        source,
        {
            (ResourcePlane.DESIRED, "gitopsctr/desired/dev", None): WorkspaceSnapshot(
                ResourcePlane.DESIRED,
                "gitopsctr/desired/dev",
                "memory-current",
                SnapshotId("memory-current"),
                desired_workspace,
                desired_workspace.entry_content_ids(),
            ),
            (ResourcePlane.OBSERVED, "gitopsctr/observed/dev", None): WorkspaceSnapshot(
                ResourcePlane.OBSERVED,
                "gitopsctr/observed/dev",
                "memory-observed",
                SnapshotId("memory-observed"),
                observed_workspace,
                observed_workspace.entry_content_ids(),
            ),
        },
    )


def _matrix_memory_provider(old_revision: str, current_revision: str) -> MemoryPlaneProvider:
    base = _memory_provider()
    old_workspace = _memory_workspace({"units/application.yaml": inventory_support.desired_terraform("application")})
    current_workspace = _memory_workspace(
        {
            "units/application.yaml": inventory_support.desired_terraform("application"),
            "units/current.yaml": inventory_support.desired_terraform("current"),
        }
    )
    snapshots = dict(base._snapshots)
    snapshots[(ResourcePlane.DESIRED, "gitopsctr/desired/matrix", old_revision)] = WorkspaceSnapshot(
        ResourcePlane.DESIRED,
        "gitopsctr/desired/matrix",
        old_revision,
        SnapshotId(old_revision),
        old_workspace,
        old_workspace.entry_content_ids(),
    )
    snapshots[(ResourcePlane.DESIRED, "gitopsctr/desired/matrix", None)] = WorkspaceSnapshot(
        ResourcePlane.DESIRED,
        "gitopsctr/desired/matrix",
        current_revision,
        SnapshotId(current_revision),
        current_workspace,
        current_workspace.entry_content_ids(),
    )
    return MemoryPlaneProvider(base._project, base._source, snapshots)


def test_resource_inspector_conformance_uses_independent_memory_planes(repository) -> None:
    provider = _memory_provider()
    command = ResourceInspectionCommand("units", environment="dev", output=InspectionOutputFormat.TABLE)
    table_result = inspect_workspace_provider(provider, RESOURCE_REGISTRY, command)
    assert table_result.tables[0].rows[0][4] == "CURRENT"

    reader = GitSnapshotReader.from_path(repository)
    git_result = inspect_workspace_provider(GitWorkspacePlaneSession(repository, reader), RESOURCE_REGISTRY, command)
    assert git_result.tables[0].rows[0][0] == table_result.tables[0].rows[0][0] == "application"
    assert git_result.tables[0].rows[0][4] == table_result.tables[0].rows[0][4] == "CURRENT"

    result = inspect_workspace_provider(
        provider,
        RESOURCE_REGISTRY,
        ResourceInspectionCommand("units", environment="dev", output=InspectionOutputFormat.JSON, as_list=True),
    )

    assert result.document is not None
    item = result.document["items"][0]
    assert item["provenance"] == {
        "environment": "dev",
        "plane": "desired",
        "ref": "gitopsctr/desired/dev",
        "revision": "memory-current",
        "path": "units/application.json",
    }

    historical = ResourceInspectionCommand(
        "units",
        environment="dev",
        desired_snapshot="memory-current",
        output=InspectionOutputFormat.TABLE,
    )
    assert inspect_workspace_provider(provider, RESOURCE_REGISTRY, historical).tables[0].rows[0][0] == "application"

    missing = _memory_provider(missing_default=True)
    default_result = inspect_workspace_provider(
        missing, RESOURCE_REGISTRY, ResourceInspectionCommand("units", environment="dev")
    )
    assert default_result.tables[0].rows == ()
    with pytest.raises(OperationError, match="missing-desired"):
        inspect_workspace_provider(
            missing,
            RESOURCE_REGISTRY,
            ResourceInspectionCommand("units", environment="dev", desired_reference="missing-desired"),
        )

    with pytest.raises(OperationError, match="duplicate logical"):
        inspect_workspace_provider(
            _memory_provider(duplicate=True), RESOURCE_REGISTRY, ResourceInspectionCommand("units", environment="dev")
        )


def test_status_uses_identical_logical_snapshot_semantics_for_git_and_memory(repository) -> None:
    command = StatusCommand("dev")
    git_provider = GitWorkspacePlaneSession(repository, GitSnapshotReader.from_path(repository))
    desired = git_provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev")
    observed = git_provider.snapshot(ResourcePlane.OBSERVED, "gitopsctr/observed/dev")
    memory = MemoryPlaneProvider(
        git_provider.project(),
        InMemoryWorkspace(git_provider.source().workspace.list_entries(), mutable=False),
        {
            (ResourcePlane.DESIRED, desired.reference, None): WorkspaceSnapshot(
                ResourcePlane.DESIRED,
                desired.reference,
                desired.revision,
                desired.snapshot_id,
                InMemoryWorkspace(desired.workspace.list_entries(), mutable=False),
                desired.workspace.entry_content_ids(),
            ),
            (ResourcePlane.OBSERVED, observed.reference, None): WorkspaceSnapshot(
                ResourcePlane.OBSERVED,
                observed.reference,
                observed.revision,
                observed.snapshot_id,
                InMemoryWorkspace(observed.workspace.list_entries(), mutable=False),
                observed.workspace.entry_content_ids(),
            ),
        },
    )
    git = status_workspace_provider(git_provider, RESOURCE_REGISTRY, command)
    memory_result = status_workspace_provider(memory, RESOURCE_REGISTRY, command)

    assert [(item.name, item.state, item.reason) for item in git.entries] == [
        (item.name, item.state, item.reason) for item in memory_result.entries
    ]
    assert git.entries[0].name == memory_result.entries[0].name == "application"
    assert git.entries[0].state == memory_result.entries[0].state == "CLEAN"
    assert [(entry.name, entry.state.value) for entry in git.entries] == [
        ("application", "CLEAN"),
        ("deleting", "WAIT"),
        ("external", "MATERIALIZED"),
        ("shared", "WAIT"),
    ]

    with pytest.raises(OperationError, match="missing-observed"):
        status_workspace_provider(
            _memory_provider(),
            RESOURCE_REGISTRY,
            StatusCommand("dev", observed_reference="missing-observed"),
        )
    with pytest.raises(OperationError, match="gitopsctr/observed/dev"):
        status_workspace_provider(
            _memory_provider(),
            RESOURCE_REGISTRY,
            StatusCommand("dev", observed_snapshot="missing-observed-snapshot"),
        )

    missing = status_workspace_provider(_memory_provider(missing_default=True), RESOURCE_REGISTRY, command)
    assert missing.desired_revision is None and missing.observed_revision is None


def test_status_git_memory_historical_and_custom_ref_matrix(repository) -> None:
    inventory_support.git(repository, "checkout", "desired")
    historical = inventory_support.git(repository, "rev-parse", "HEAD")
    inventory_support.write_json(repository / "units/candidate.json", inventory_support.desired_terraform("candidate"))
    current = inventory_support.commit(repository, "candidate desired")
    inventory_support.git(repository, "push", "origin", f"{current}:refs/heads/gitopsctr/candidate/dev")
    inventory_support.git(repository, "checkout", "main")

    git_provider = GitWorkspacePlaneSession(repository, GitSnapshotReader.from_path(repository))
    desired_current = git_provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/candidate/dev")
    desired_historical = git_provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/candidate/dev", historical)
    observed = git_provider.snapshot(ResourcePlane.OBSERVED, "gitopsctr/observed/dev")

    def copy(snapshot):
        workspace = InMemoryWorkspace(snapshot.workspace.list_entries(), mutable=False)
        return WorkspaceSnapshot(
            snapshot.plane,
            snapshot.reference,
            snapshot.revision,
            snapshot.snapshot_id,
            workspace,
            workspace.entry_content_ids(),
        )

    memory = MemoryPlaneProvider(
        git_provider.project(),
        InMemoryWorkspace(git_provider.source().workspace.list_entries(), mutable=False),
        {
            (ResourcePlane.DESIRED, "gitopsctr/candidate/dev", None): copy(desired_current),
            (ResourcePlane.DESIRED, "gitopsctr/candidate/dev", historical): copy(desired_historical),
            (ResourcePlane.OBSERVED, "gitopsctr/observed/dev", None): copy(observed),
            (ResourcePlane.OBSERVED, "gitopsctr/observed/dev", observed.revision): copy(observed),
        },
    )
    current_command = StatusCommand(
        "dev", desired_reference="gitopsctr/candidate/dev", observed_reference="gitopsctr/observed/dev"
    )
    historical_command = StatusCommand(
        "dev",
        desired_reference="gitopsctr/candidate/dev",
        desired_snapshot=historical,
        observed_reference="gitopsctr/observed/dev",
        observed_snapshot=observed.revision,
    )

    git_current = status_workspace_provider(git_provider, RESOURCE_REGISTRY, current_command)
    memory_current = status_workspace_provider(memory, RESOURCE_REGISTRY, current_command)
    git_historical = status_workspace_provider(git_provider, RESOURCE_REGISTRY, historical_command)
    memory_historical = status_workspace_provider(memory, RESOURCE_REGISTRY, historical_command)

    assert git_current == memory_current
    assert git_historical == memory_historical
    assert git_current.desired_reference == "gitopsctr/candidate/dev"
    assert git_current.desired_revision == current
    assert git_historical.desired_revision == historical
    assert git_current.observed_reference == git_historical.observed_reference == "gitopsctr/observed/dev"
    assert git_historical.observed_revision == observed.revision
    assert "candidate" in {entry.name for entry in git_current.entries}
    assert "candidate" not in {entry.name for entry in git_historical.entries}


def test_status_resolves_each_selected_plane_once_without_priming() -> None:
    base = _memory_provider()

    class CountingPlanes:
        def __init__(self) -> None:
            self.source_calls = 0
            self.snapshot_calls: list[tuple[ResourcePlane, str, str | None]] = []
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def project(self):
            return base.project()

        def source(self):
            self.source_calls += 1
            return base.source()

        def snapshot(self, plane, reference, revision=None, *, allow_missing=False):
            self.snapshot_calls.append((plane, reference, revision))
            return base.snapshot(plane, reference, revision, allow_missing=allow_missing)

    planes = CountingPlanes()
    result = status_workspace_provider(planes, RESOURCE_REGISTRY, StatusCommand("dev"))

    assert result.entries[0].name == "application"
    assert planes.source_calls == 1
    assert planes.snapshot_calls == [
        (ResourcePlane.DESIRED, "gitopsctr/desired/dev", None),
        (ResourcePlane.OBSERVED, "gitopsctr/observed/dev", None),
    ]
    assert planes.close_calls == 1


def test_status_reports_transition_and_opaque_cleanup_from_logical_desired_content() -> None:
    base = _memory_provider()
    selected_desired = base.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev")
    desired = InMemoryWorkspace(
        (
            *selected_desired.workspace.list_entries(),
            WorkspaceEntry.file(
                ".gitopsctr/transition-blocks.json", b'{"blocks":{"application":"transition pending"}}'
            ),
            WorkspaceEntry.file(".gitopsctr/cleanup/units/orphan.json", b"{}"),
        ),
        mutable=False,
    )
    observed = base.snapshot(ResourcePlane.OBSERVED, "gitopsctr/observed/dev")
    provider = MemoryPlaneProvider(
        base.project(),
        base.source().workspace,
        {
            (ResourcePlane.DESIRED, "gitopsctr/desired/dev", None): WorkspaceSnapshot(
                ResourcePlane.DESIRED,
                "gitopsctr/desired/dev",
                "transition",
                SnapshotId("transition"),
                desired,
                desired.entry_content_ids(),
            ),
            (ResourcePlane.OBSERVED, "gitopsctr/observed/dev", None): observed,
        },
    )

    result = status_workspace_provider(provider, RESOURCE_REGISTRY, StatusCommand("dev"))

    assert [(entry.name, entry.state.value, entry.reason) for entry in result.entries] == [
        ("application", "WAIT", "transition pending"),
        ("orphan", "WAIT", "desired inputs are not materialized"),
    ]


def test_status_closes_original_provider_once_when_plane_selection_fails() -> None:
    base = _memory_provider()

    class FailingPlanes:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def project(self):
            return base.project()

        def source(self):
            return base.source()

        def snapshot(self, plane, reference, revision=None, *, allow_missing=False):
            if plane is ResourcePlane.OBSERVED:
                raise OperationError("observed moved")
            return base.snapshot(plane, reference, revision, allow_missing=allow_missing)

    planes = FailingPlanes()
    with pytest.raises(OperationError, match="observed moved"):
        status_workspace_provider(planes, RESOURCE_REGISTRY, StatusCommand("dev"))
    assert planes.close_calls == 1


def test_status_closes_original_provider_once_when_project_construction_fails() -> None:
    class FailingProjectPlanes:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def project(self):
            raise OperationError("project unreadable")

        def source(self):
            raise AssertionError("source must not be read after project failure")

        def snapshot(self, plane, reference, revision=None, *, allow_missing=False):
            raise AssertionError("snapshot must not be read after project failure")

    planes = FailingProjectPlanes()
    with pytest.raises(OperationError, match="project unreadable"):
        status_workspace_provider(planes, RESOURCE_REGISTRY, StatusCommand("dev"))
    assert planes.close_calls == 1


def test_status_keeps_same_terminal_stack_units_by_qualified_identity_and_selector() -> None:
    desired = _memory_provider().snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev")
    evaluation = SimpleNamespace(
        units=(
            SimpleNamespace(
                unit=SimpleNamespace(name="image", qualified_name="application/image"),
                reconciliation=SimpleNamespace(value="CLEAN"),
                reason="application clean",
            ),
            SimpleNamespace(
                unit=SimpleNamespace(name="image", qualified_name="backend/image"),
                reconciliation=SimpleNamespace(value="READY"),
                reason="backend changed",
            ),
        )
    )

    entries = _status_entries(evaluation, (), desired, StatusCommand("dev"))
    selected = _status_entries(evaluation, (), desired, StatusCommand("dev", unit="backend/image"))

    assert [(entry.name, entry.state.value) for entry in entries] == [
        ("application/image", "CLEAN"),
        ("backend/image", "READY"),
    ]
    assert [(entry.name, entry.reason) for entry in selected] == [("backend/image", "backend changed")]


def test_git_and_memory_providers_share_current_and_historical_command_matrix(repository) -> None:
    inventory_support.git(repository, "checkout", "desired")
    old_revision = inventory_support.git(repository, "rev-parse", "HEAD")
    inventory_support.write_json(repository / "units/current.json", inventory_support.desired_terraform("current"))
    current_revision = inventory_support.commit(repository, "add matrix current unit")
    inventory_support.git(repository, "push", "origin", f"{current_revision}:refs/heads/gitopsctr/desired/matrix")
    inventory_support.git(repository, "checkout", "main")

    git_provider = GitWorkspacePlaneSession(repository, GitSnapshotReader.from_path(repository))
    memory_provider = _matrix_memory_provider(old_revision, current_revision)
    current = ResourceInspectionCommand(
        "units",
        name="application",
        environment="dev",
        desired_reference="gitopsctr/desired/matrix",
        output=InspectionOutputFormat.JSON,
        as_list=True,
    )
    historical = ResourceInspectionCommand(
        "units",
        name="application",
        environment="dev",
        desired_reference="gitopsctr/desired/matrix",
        desired_snapshot=old_revision,
        output=InspectionOutputFormat.JSON,
        as_list=True,
    )

    git_current = inspect_workspace_provider(git_provider, RESOURCE_REGISTRY, current).document
    memory_current = inspect_workspace_provider(memory_provider, RESOURCE_REGISTRY, current).document
    git_historical = inspect_workspace_provider(git_provider, RESOURCE_REGISTRY, historical).document
    memory_historical = inspect_workspace_provider(memory_provider, RESOURCE_REGISTRY, historical).document
    assert git_current == memory_current
    assert git_historical == memory_historical
    assert git_current is not None and git_historical is not None
    assert git_current["items"][0]["provenance"]["revision"] == current_revision
    assert git_historical["items"][0]["provenance"]["revision"] == old_revision

    git_snapshot = git_provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/matrix")
    memory_snapshot = memory_provider.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/matrix")
    git_record = next(
        record
        for record in _unit_discovery(
            git_snapshot.workspace,
            git_provider.project(),
        )
        if record.name == "application"
    )
    memory_record = next(
        record
        for record in _unit_discovery(
            memory_snapshot.workspace,
            memory_provider.project(),
        )
        if record.name == "application"
    )
    assert (git_record.path, git_record.document, git_record.media_type) == (
        memory_record.path,
        memory_record.document,
        memory_record.media_type,
    )
    # The matrix deliberately uses different YAML and JSON serializations;
    # logical entry identities remain byte- and path-exact.
    assert git_record.content_id != memory_record.content_id
    assert git_record.content_digest != memory_record.content_digest

    inventory_support.git(repository, "checkout", "desired")
    inventory_support.write_json(
        repository / "units/application.json", inventory_support.desired_terraform("application")
    )
    duplicate_revision = inventory_support.commit(repository, "add matrix duplicate format")
    inventory_support.git(repository, "push", "origin", f"{duplicate_revision}:refs/heads/gitopsctr/desired/duplicate")
    inventory_support.git(repository, "checkout", "main")
    duplicate_memory = _memory_provider(duplicate=True)
    duplicate_snapshot = duplicate_memory._snapshots[(ResourcePlane.DESIRED, "gitopsctr/desired/dev", None)]
    duplicate_memory._snapshots[(ResourcePlane.DESIRED, "gitopsctr/desired/duplicate", None)] = WorkspaceSnapshot(
        ResourcePlane.DESIRED,
        "gitopsctr/desired/duplicate",
        duplicate_snapshot.revision,
        duplicate_snapshot.snapshot_id,
        duplicate_snapshot.workspace,
        duplicate_snapshot.content_ids,
    )
    duplicate_command = ResourceInspectionCommand(
        "units", environment="dev", desired_reference="gitopsctr/desired/duplicate"
    )
    for provider in (git_provider, duplicate_memory):
        with pytest.raises(OperationError, match="duplicate logical"):
            inspect_workspace_provider(provider, RESOURCE_REGISTRY, duplicate_command)

    missing_environment = inventory_support.environment_document("dev")
    missing_environment["spec"] = {"refs": {"desired": "missing-desired", "observed": "missing-observed"}}
    inventory_support.write_json(repository / "deployment/environments/dev/environment.yaml", missing_environment)
    missing_command = ResourceInspectionCommand("units", environment="dev")
    for provider in (
        GitWorkspacePlaneSession(repository, GitSnapshotReader.from_path(repository)),
        _memory_provider(missing_default=True),
    ):
        assert inspect_workspace_provider(provider, RESOURCE_REGISTRY, missing_command).tables[0].rows == ()
        with pytest.raises(OperationError, match="missing-desired"):
            inspect_workspace_provider(
                provider,
                RESOURCE_REGISTRY,
                ResourceInspectionCommand("units", environment="dev", desired_reference="missing-desired"),
            )
