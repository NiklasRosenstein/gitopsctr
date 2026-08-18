"""Historical workload evidence through the production apply coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from dulwich.repo import Repo

from gitopsctr.adapters.git.apply import GitApplySourceEvidenceProvider
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application.apply import (
    ApplyCommand,
    AuthoredChangeSet,
    _issue_authored_document,
    _issue_authored_source_acquisition,
)
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    CatalogUnitProjectionCompiler,
    UnitProjection,
    UnitProjectionRequest,
)
from gitopsctr.application.apply_orchestration import (
    ApplyCoordinator,
    ApplyEnvironmentConfiguration,
    HmacApplyPublicationIdentityIssuer,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionPolicy,
    HmacRootIncarnationIssuer,
    RetainedSourceDescriptor,
    SourceBindingRole,
    WorkspaceProjectionContext,
    _issue_retained_source_descriptor,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    PublicationOutcomeState,
    RetainedSource,
    SnapshotId,
    SourceId,
)
from gitopsctr.application.sources import SourceError, SourceRequest, SourceSnapshot
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, UnitResource

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
SOURCE_ID = SourceId("default-git-source")
DESIRED = ChannelId("desired/dev")
OBSERVED = ChannelId("observed/dev")
CANDIDATE = ChannelId("candidate/dev")
REVISION_A = "a" * 40
REVISION_B = "b" * 40
REVISION_C = "c" * 40


@dataclass
class _RecordingSourceRepository:
    delegate: MemorySourceRepository
    retained: list[RetainedSource] = field(default_factory=list)
    released: list[RetainedSource] = field(default_factory=list)
    fail_selectors: dict[str, str] = field(default_factory=dict)

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        failure = self.fail_selectors.get(request.selector)
        if failure is not None:
            raise SourceError(failure)
        return self.delegate.resolve(request)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        retained = self.delegate.retain(source)
        self.retained.append(retained)
        return retained

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        return self.delegate.recover(retained)

    def release(self, retained: RetainedSource) -> None:
        self.delegate.release(retained)
        self.released.append(retained)

    def reissue(self, locator):  # type: ignore[no-untyped-def]
        return self.delegate.reissue(locator)

    def close(self) -> None:
        self.delegate.close()


@dataclass(frozen=True)
class _EnvironmentResolver:
    primary: RetainedSourceDescriptor

    def resolve(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyEnvironmentConfiguration:
        del command, changes
        return ApplyEnvironmentConfiguration(
            DESIRED,
            OBSERVED,
            CANDIDATE,
            ApplyProjectionPolicy(),
            WorkspaceProjectionContext(b"kind: Project\n", b"kind: Environment\n"),
            self.primary,
        )

    def close(self) -> None:
        """No owned resources."""


@dataclass
class _CapturingHost:
    selected: dict[str, tuple[SnapshotId, bytes]] = field(default_factory=dict)

    def project(self, request: UnitProjectionRequest) -> UnitProjection:
        assert request.source is not None and request.selected_source is not None
        specification = request.unit.driver.unit_contract.dump(request.unit.spec)
        assert isinstance(specification, dict)
        source = specification.get("source")
        assert isinstance(source, dict)
        path = source.get("path")
        assert isinstance(path, str)
        plane = request.selected_source.plane
        self.selected[request.qualified_name] = (
            request.selected_source.retained.source_snapshot_id.snapshot_id,
            plane.workspace.read(f"{path}/main.tf"),
        )
        return UnitProjection(
            UnitResource(
                request.unit.gvk,
                request.metadata,
                request.unit.driver,
                TerraformDesiredUnit(source=request.source),
            )
        )


def _resource(name: str, kind: str, spec: dict[str, object]) -> JsonObject:
    return cast(
        JsonObject,
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": kind,
            "metadata": {"name": name},
            "spec": spec,
        },
    )


def _changes(
    source: _RecordingSourceRepository,
    current: SourceSnapshot,
    unit_revisions: tuple[tuple[str, str], ...],
) -> tuple[AuthoredChangeSet, RetainedSourceDescriptor]:
    retained = source.retain(current)
    acquisition = _issue_authored_source_acquisition(current, retained)
    primary = _issue_retained_source_descriptor(
        retained,
        "authored",
        SourceBindingRole.PRIMARY_AUTHORED,
        "stacks.yaml",
        ContentId("selector-current"),
    )
    templates = {
        name: {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "spec": {"source": {"path": name, "revision": revision, "inputs": ["**/*.tf"]}},
        }
        for name, revision in unit_revisions
    }
    documents = (
        _issue_authored_document(
            "template",
            _resource("shared", "StackTemplate", {"parameters": [], "unitTemplates": templates}),
            ContentId("sha256:" + "1" * 64),
        ),
        _issue_authored_document(
            "stack",
            _resource("bundle", "Stack", {"template": "shared"}),
            ContentId("sha256:" + "2" * 64),
        ),
    )
    return AuthoredChangeSet(documents, current.source_snapshot_id, acquisition), primary


def _repository() -> tuple[_RecordingSourceRepository, SourceSnapshot, SourceSnapshot]:
    delegate = MemorySourceRepository(SOURCE_ID)
    historical = delegate.install(
        SnapshotId(f"git-source:{REVISION_A}"),
        InMemoryWorkspace(
            (
                WorkspaceEntry.file("old/main.tf", b"historical-old"),
                WorkspaceEntry.file("current/main.tf", b"historical-current"),
            ),
            mutable=False,
        ),
    )
    current = delegate.install(
        SnapshotId(f"git-source:{REVISION_B}"),
        InMemoryWorkspace(
            (
                WorkspaceEntry.file("old/main.tf", b"current-old"),
                WorkspaceEntry.file("current/main.tf", b"current-current"),
            ),
            mutable=False,
        ),
    )
    delegate.set_selector(REVISION_A, historical.source_snapshot_id.snapshot_id)
    delegate.set_selector(REVISION_B, current.source_snapshot_id.snapshot_id)
    return _RecordingSourceRepository(delegate), historical, current


def _coordinator(
    root: Path,
    source: _RecordingSourceRepository,
    primary: RetainedSourceDescriptor,
    host: _CapturingHost,
) -> tuple[ApplyCoordinator, GitPublicationStore]:
    root.mkdir()
    Repo.init_bare(root).close()
    store = GitPublicationStore(root, source)
    lineage = GitSourceLineageEncoder({SOURCE_ID: "."})
    logical = CatalogLogicalUnitProjector(CATALOG, lineage, host)
    return (
        ApplyCoordinator(
            GitSnapshotReader.from_path(root),
            store,
            source,
            _EnvironmentResolver(primary),
            CatalogApplyDocumentValidator(CATALOG),
            CatalogUnitProjectionCompiler(CATALOG, logical),
            CatalogStackProjectionCompiler(CATALOG, logical, source_encoder=lineage),
            HmacRootIncarnationIssuer("historical-coordinator", "root-seed"),
            HmacApplyPublicationIdentityIssuer("historical-coordinator", "publication-seed"),
            GitApplySourceEvidenceProvider(source, SOURCE_ID),
        ),
        store,
    )


def _command() -> ApplyCommand:
    return ApplyCommand(
        EnvironmentId("dev"),
        ("stacks.yaml",),
        DESIRED,
        OBSERVED,
        CANDIDATE,
        SourceRequest(SOURCE_ID, REVISION_B),
    )


def test_coordinator_projects_stack_children_from_distinct_exact_retained_revisions(tmp_path: Path) -> None:
    source, _historical, current = _repository()
    changes, primary = _changes(source, current, (("old", REVISION_A), ("current", REVISION_B)))
    host = _CapturingHost()
    repository = tmp_path / "authority.git"
    coordinator, _store = _coordinator(repository, source, primary, host)

    result = coordinator.apply(_command(), changes)

    assert result.publication_outcome is not None
    assert result.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert result.snapshot_id is not None
    assert host.selected == {
        "bundle/current": (SnapshotId(f"git-source:{REVISION_B}"), b"current-current"),
        "bundle/old": (SnapshotId(f"git-source:{REVISION_A}"), b"historical-old"),
    }
    assert result.publication is not None
    assert {
        change.retained_source.source_snapshot_id.snapshot_id for change in result.publication.source_ownership_changes
    } == {
        SnapshotId(f"git-source:{REVISION_A}"),
        SnapshotId(f"git-source:{REVISION_B}"),
    }
    assert (
        GitSnapshotReader.from_path(repository)
        .open_snapshot(result.snapshot_id)
        .workspace.read("units/bundle/old.json")
    )
    assert not source.released


def test_missing_later_revision_releases_authored_and_already_retained_history(tmp_path: Path) -> None:
    source, _historical, current = _repository()
    source.fail_selectors[REVISION_C] = "historical revision is missing"
    changes, primary = _changes(source, current, (("a-old", REVISION_A), ("z-missing", REVISION_C)))
    coordinator, _store = _coordinator(tmp_path / "authority.git", source, primary, _CapturingHost())

    with pytest.raises(SourceError, match="historical revision is missing"):
        coordinator.apply(_command(), changes)

    assert set(source.released) == set(source.retained)
    assert len(source.retained) == 2


def test_ambiguous_duplicate_exact_plane_fails_closed_and_releases_every_handle(tmp_path: Path) -> None:
    source, historical, current = _repository()
    source.delegate.set_selector(REVISION_C, historical.source_snapshot_id.snapshot_id)
    changes, primary = _changes(source, current, (("old-a", REVISION_A), ("old-c", REVISION_C)))
    coordinator, _store = _coordinator(tmp_path / "authority.git", source, primary, _CapturingHost())

    with pytest.raises(ValueError, match="repeat a source snapshot"):
        coordinator.apply(_command(), changes)

    assert set(source.released) == set(source.retained)
    assert len(source.retained) == 3
