"""Production source-policy and logical input-hash coverage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder, GitPromotionLineageError
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder, GitSourceLineageError
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelectionError, GitUnitSourceSelector
from gitopsctr.application.apply_compilers import (
    ProjectionCompilerError,
    RoleBoundUnitSourceSelector,
    UnitSourceSelectionRequest,
    WorkspaceUnitInputHasher,
)
from gitopsctr.application.apply_projection import (
    ExactPlane,
    PromotionSourceDescriptor,
    RetainedSourceDescriptor,
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
from gitopsctr.controller import hash_source_inputs
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resources import ResourceCatalog, UnitResource

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
SOURCE = SourceId("workload-source")
REV_A = "a" * 40
REV_B = "b" * 40


def _workspace(*entries: WorkspaceEntry) -> InMemoryWorkspace:
    return InMemoryWorkspace(entries, mutable=False)


def _plane(channel: str, snapshot: SnapshotId, workspace: InMemoryWorkspace) -> ExactPlane:
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _retained(
    binding: str,
    revision: str,
    *,
    role: SourceBindingRole = SourceBindingRole.WORKLOAD,
    source: SourceId = SOURCE,
    tag: str = "",
) -> RetainedSourcePlane:
    workspace = _workspace(WorkspaceEntry.file("src/main.tf", f"{binding}:{revision}:{tag}".encode()))
    snapshot = SnapshotId(f"git-source:{revision}")
    retained = _issue_retained_source(
        RetainedSourceHandle(f"{binding}-{revision}-{tag}"),
        RetentionStoreId(f"store-{binding}-{revision}-{tag}"),
        SourceSnapshotId(source, snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        binding,
        role,
        f"workloads/{binding}",
        ContentId("sha256:" + "1" * 64),
    )
    return RetainedSourcePlane(
        retained, _plane(f"source-{binding}-{tag or revision[:6]}", snapshot, workspace), (descriptor,)
    )


def _unit() -> UnitResource[Any]:
    return CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "app"},
            "spec": {"source": {"path": "src", "revision": REV_B, "inputs": ["**/*.tf"]}},
        },
        profile="authored",
    )


def _request(
    *,
    named: tuple[RetainedSourceDescriptor, ...],
    planes: tuple[RetainedSourcePlane, ...],
    revision: str | None = REV_B,
    prior: DesiredSource | None = None,
    primary: RetainedSourceDescriptor | None = None,
    qualified_name: str = "app",
) -> UnitSourceSelectionRequest:
    unit = _unit()
    return UnitSourceSelectionRequest(
        qualified_name,
        unit,
        {"path": "src"} if revision is None else {"path": "src", "revision": revision},
        prior,
        primary,
        named,
        planes,
    )


def _source_encoder() -> GitSourceLineageEncoder:
    return GitSourceLineageEncoder({SOURCE: "https://example.test/workloads.git"})


def test_git_unit_source_selector_uses_exact_binding_and_historical_revision() -> None:
    historical, current, other = (
        _retained("app-workload", REV_A),
        _retained("app-workload", REV_B),
        _retained("other-workload", REV_B),
    )
    selector = GitUnitSourceSelector(_source_encoder(), {"app": "app-workload"})

    selected = selector.select(
        _request(
            named=(historical.descriptors[0], current.descriptors[0], other.descriptors[0]),
            planes=(historical, current, other),
        )
    )
    assert selected.descriptor is current.descriptors[0]
    assert selected.plane is current

    selected_prior = selector.select(
        _request(
            named=(historical.descriptors[0], current.descriptors[0]),
            planes=(historical, current),
            revision=None,
            prior=DesiredSource(path="src", revision=REV_A),
        )
    )
    assert selected_prior.descriptor is historical.descriptors[0]


def test_git_selector_uses_storage_qualified_name_for_same_leaf_stack_children() -> None:
    application_current = _retained("application-workload", REV_B)
    worker_current = _retained("worker-workload", REV_B)
    historical = _retained("application-workload", REV_A)
    selector = GitUnitSourceSelector(
        _source_encoder(),
        {"application/app": "application-workload", "worker/app": "worker-workload"},
    )

    application = selector.select(
        _request(
            named=(historical.descriptors[0], application_current.descriptors[0], worker_current.descriptors[0]),
            planes=(historical, application_current, worker_current),
            qualified_name="application/app",
        )
    )
    worker = selector.select(
        _request(
            named=(historical.descriptors[0], application_current.descriptors[0], worker_current.descriptors[0]),
            planes=(historical, application_current, worker_current),
            qualified_name="worker/app",
        )
    )
    historical_application = selector.select(
        _request(
            named=(historical.descriptors[0], application_current.descriptors[0]),
            planes=(historical, application_current),
            revision=None,
            prior=DesiredSource(path="src", revision=REV_A),
            qualified_name="application/app",
        )
    )

    assert application.plane is application_current
    assert worker.plane is worker_current
    assert historical_application.plane is historical


def test_git_unit_source_selector_fails_closed_for_missing_ambiguous_foreign_and_tampered_evidence() -> None:
    current = _retained("app-workload", REV_B)
    selector = GitUnitSourceSelector(_source_encoder(), {"app": "app-workload"})
    with pytest.raises(GitUnitSourceSelectionError, match="unavailable"):
        selector.select(_request(named=(current.descriptors[0],), planes=()))
    with pytest.raises(GitUnitSourceSelectionError, match="no configured"):
        GitUnitSourceSelector(_source_encoder(), {}).select(
            _request(named=(current.descriptors[0],), planes=(current,))
        )

    duplicate = _retained("app-workload", REV_B, tag="duplicate")
    with pytest.raises(GitUnitSourceSelectionError, match="ambiguous"):
        selector.select(
            _request(
                named=(current.descriptors[0], duplicate.descriptors[0]),
                planes=(current, duplicate),
            )
        )
    object.__setattr__(current.descriptors[0], "binding_key", "tampered")
    with pytest.raises(TypeError, match="modified"):
        selector.select(_request(named=(current.descriptors[0],), planes=(current,)))


def test_role_bound_source_selector_requires_one_recovered_explicit_source() -> None:
    primary = _retained("primary", REV_A, role=SourceBindingRole.PRIMARY_AUTHORED)
    selected = RoleBoundUnitSourceSelector().select(
        _request(named=(), planes=(primary,), primary=primary.descriptors[0], revision=None)
    )
    assert selected.plane is primary
    with pytest.raises(ProjectionCompilerError, match="one explicit"):
        RoleBoundUnitSourceSelector().select(_request(named=(), planes=()))
    one = _retained("one", REV_A)
    two = _retained("two", REV_B)
    with pytest.raises(ProjectionCompilerError, match="one explicit"):
        RoleBoundUnitSourceSelector().select(
            _request(named=(one.descriptors[0], two.descriptors[0]), planes=(one, two), revision=REV_A)
        )
    historical = _retained("one", REV_A, tag="historical")
    with pytest.raises(ProjectionCompilerError, match="one explicit"):
        RoleBoundUnitSourceSelector().select(
            _request(
                named=(historical.descriptors[0], one.descriptors[0]),
                planes=(historical, one),
                revision=REV_A,
            )
        )
    object.__setattr__(one.descriptors[0], "binding_key", "tampered")
    with pytest.raises(TypeError, match="modified"):
        RoleBoundUnitSourceSelector().select(_request(named=(one.descriptors[0],), planes=(one,), revision=REV_A))


@pytest.mark.parametrize("qualified_name", (" worker/app", "worker\\app", "worker/../app", "worker/other"))
def test_unit_source_selection_request_rejects_noncanonical_or_foreign_qualified_name(qualified_name: str) -> None:
    with pytest.raises(ProjectionCompilerError, match="qualified|canonical"):
        _request(named=(), planes=(), qualified_name=qualified_name)


def test_git_source_lineage_enforces_repository_snapshot_plane_and_issuance() -> None:
    source = _retained("app-workload", REV_A)
    assert _source_encoder().encode(source.descriptors[0], source).revision == REV_A
    with pytest.raises(GitSourceLineageError, match="no configured"):
        GitSourceLineageEncoder({}).encode(source.descriptors[0], source)
    foreign = _retained("app-workload", REV_A, source=SourceId("foreign"))
    with pytest.raises(GitSourceLineageError, match="SourceId"):
        _source_encoder().encode(foreign.descriptors[0], foreign)
    with pytest.raises(GitSourceLineageError, match="not bound"):
        _source_encoder().encode(source.descriptors[0], foreign)
    object.__setattr__(source.descriptors[0], "workspace_key", "tampered")
    with pytest.raises(TypeError, match="modified"):
        _source_encoder().encode(source.descriptors[0], source)


def _promotion_descriptor(
    *,
    source_snapshot: str = REV_A,
    target_snapshot: str = REV_B,
) -> PromotionSourceDescriptor:
    source_desired = _plane("desired/staging", SnapshotId(f"git-commit:{source_snapshot}"), _workspace())
    source_observed = _plane("observed/staging", SnapshotId(f"git-commit:{source_snapshot}"), _workspace())
    target_desired = _plane("desired/dev", SnapshotId(f"git-commit:{target_snapshot}"), _workspace())
    target_observed = _plane("observed/dev", SnapshotId(f"git-commit:{target_snapshot}"), _workspace())
    return _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        source_desired,
        source_observed,
        target_desired,
        target_observed,
        ContentId("sha256:" + "2" * 64),
    )


def _promotion_encoder() -> GitPromotionLineageEncoder:
    return GitPromotionLineageEncoder(
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


def test_git_promotion_lineage_enforces_policy_mappings_snapshots_refs_and_issuance() -> None:
    descriptor = _promotion_descriptor()
    lineage = _promotion_encoder().encode(descriptor)
    assert lineage.source_desired_revision == REV_A
    assert lineage.target_observed_revision == REV_B
    with pytest.raises(GitPromotionLineageError, match="not allowed"):
        GitPromotionLineageEncoder(
            _promotion_encoder().desired_refs,
            _promotion_encoder().observed_refs,
            {EnvironmentId("dev"): set()},
        ).encode(descriptor)
    with pytest.raises(GitPromotionLineageError, match="no source desired"):
        GitPromotionLineageEncoder(
            {ChannelId("desired/dev"): "desired/dev"},
            _promotion_encoder().observed_refs,
            {EnvironmentId("dev"): {EnvironmentId("staging")}},
        ).encode(descriptor)
    with pytest.raises(GitPromotionLineageError, match="exact git-commit"):
        _promotion_encoder().encode(_promotion_descriptor(source_snapshot="foreign"))
    for invalid_ref in ("a..b", "topic.lock", "refs//heads/main", ".hidden", "main@{1}"):
        with pytest.raises(GitPromotionLineageError, match="invalid ref"):
            GitPromotionLineageEncoder(
                {ChannelId("desired/staging"): invalid_ref},
                _promotion_encoder().observed_refs,
                {EnvironmentId("dev"): {EnvironmentId("staging")}},
            )
    object.__setattr__(descriptor, "source_environment", EnvironmentId("other"))
    with pytest.raises(TypeError, match="modified"):
        _promotion_encoder().encode(descriptor)


def test_workspace_unit_input_hasher_matches_legacy_globs_and_preserves_entry_identity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    entries = (
        WorkspaceEntry.file("infra/main.tf", b"root\n"),
        WorkspaceEntry.file("infra/modules/app/main.tf", b"nested\n", executable=True),
        WorkspaceEntry.file("infra/README.md", b"ignored\n"),
    )
    workspace = _workspace(*entries)
    source_root = tmp_path / "source"
    for entry in entries:
        destination = source_root / entry.key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.content or b"")
        if entry.executable:
            destination.chmod(0o755)
    unit = _unit()
    source = {"path": "infra", "inputs": ["**/*.tf"], "revision": REV_B}
    specification = deepcopy(unit.driver.unit_contract.dump(unit.spec))
    assert isinstance(specification, dict)
    source_specification = specification.get("source")
    assert isinstance(source_specification, dict)
    source_specification.pop("revision", None)
    identity = {
        "kind": "unit",
        "driver": unit.driver_name,
        "driverVersion": unit.driver.version,
        "specification": specification,
    }
    legacy = hash_source_inputs(source_root, "infra", ["**/*.tf"], identity)
    logical = WorkspaceUnitInputHasher().hash(unit, workspace, source)
    assert logical == legacy

    changed_mode = _workspace(
        WorkspaceEntry.file("infra/main.tf", b"root\n"),
        WorkspaceEntry.file("infra/modules/app/main.tf", b"nested\n"),
        WorkspaceEntry.file("infra/README.md", b"ignored\n"),
    )
    assert WorkspaceUnitInputHasher().hash(unit, changed_mode, source) != logical
    with pytest.raises(ProjectionCompilerError, match="does not exist"):
        WorkspaceUnitInputHasher().hash(unit, workspace, {"path": "infra", "inputs": ["missing.tf"]})
    with pytest.raises(ProjectionCompilerError, match="stay inside"):
        WorkspaceUnitInputHasher().hash(unit, workspace, {"path": "../unsafe"})
