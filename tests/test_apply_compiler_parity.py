"""Focused controller-free compiler characterisation tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

import pytest

from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder, GitPromotionLineageError
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelector
from gitopsctr.application.apply_compilers import (
    CatalogStackProjectionCompiler,
    ProjectionCompilerError,
    TemplateResolutionSession,
    UnitProjection,
    UnitSourceSelectionRequest,
    _parse_promoted_artifact_receipt,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    FinalizedTombstone,
    FrozenAuthoredDocument,
    HmacRootIncarnationIssuer,
    RetainedSourcePlane,
    SourceBindingRole,
    WorkspaceProjectionContext,
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
from gitopsctr.contracts import (
    ArtifactImport,
    DesiredSource,
    DesiredStackSpec,
    PromotionStackReference,
    StackActiveProjection,
    StackProjectionUnitBinding,
)
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, StackResource, UnitResource, desired_unit_binding_digest

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)


class _UnitProjector:
    def project_unit(
        self, unit, *, metadata, previous, current_workspace, retained_sources, observed, context, session=None
    ):  # type: ignore[no-untyped-def]
        del previous, current_workspace, session
        assert unit.gvk.kind == "Terraform"
        desired = TerraformDesiredUnit(
            source=DesiredSource(path=".", revision="a" * 40, inputHash="sha256:" + "b" * 64),
        )
        return UnitProjection(UnitResource(unit.gvk, metadata, unit.driver, desired))


def _plane(channel: str, snapshot: str, workspace: InMemoryWorkspace) -> ExactPlane:
    identifier = SnapshotId(snapshot)
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), identifier, f"{snapshot}-incarnation"),
        workspace,
        SnapshotView(identifier, workspace.content_id, workspace),
    )


def _context() -> ApplyProjectionContext:
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(
            b"apiVersion: gitopsctr.io/v1\nkind: Project\n", b"kind: Environment\n"
        ),
        primary_source=_source().descriptors[0],
        root_identity_issuer=HmacRootIncarnationIssuer("test-root-issuer", "test-root-identity-seed"),
    )


def _git_plane(channel: str, revision: str, workspace: InMemoryWorkspace) -> ExactPlane:
    return _plane(channel, f"git-commit:{revision}", workspace)


def _promotion_encoder() -> GitPromotionLineageEncoder:
    return GitPromotionLineageEncoder(
        desired_refs={ChannelId("desired/staging"): "desired/staging", ChannelId("desired/dev"): "desired/dev"},
        observed_refs={ChannelId("observed/staging"): "observed/staging", ChannelId("observed/dev"): "observed/dev"},
        allowed_sources={EnvironmentId("dev"): frozenset((EnvironmentId("staging"),))},
    )


def _source() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("template.json", b"{}"),), mutable=False)
    snapshot = SnapshotId("git-source:" + "a" * 40)
    retained = _issue_retained_source(
        RetainedSourceHandle("retained"),
        RetentionStoreId("store"),
        SourceSnapshotId(SourceId("source"), snapshot),
        workspace.content_id,
    )
    plane = _plane("source", snapshot.value, workspace)
    descriptor = _issue_retained_source_descriptor(
        retained,
        "authored-primary",
        SourceBindingRole.PRIMARY_AUTHORED,
        "template.json",
        ContentId("sha256:" + "f" * 64),
    )
    return RetainedSourcePlane(retained, plane, (descriptor,))


def _source_encoder() -> GitSourceLineageEncoder:
    return GitSourceLineageEncoder({SourceId("source"): "."})


def _compiler(promotion: GitPromotionLineageEncoder | None = None) -> CatalogStackProjectionCompiler:
    return CatalogStackProjectionCompiler(CATALOG, _UnitProjector(), promotion, _source_encoder())


def _template() -> JsonObject:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "template"},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "database": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": "."}},
                }
            },
        },
    }


def _stack(name: str = "application") -> JsonObject:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": name},
        "spec": {"template": "template"},
    }


def _promotion_descriptor(source_workspace: InMemoryWorkspace):
    return _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        _git_plane("desired/staging", "1" * 40, source_workspace),
        _git_plane("observed/staging", "2" * 40, InMemoryWorkspace(mutable=False)),
        _git_plane("desired/dev", "3" * 40, InMemoryWorkspace(mutable=False)),
        _git_plane("observed/dev", "4" * 40, InMemoryWorkspace(mutable=False)),
        ContentId("sha256:" + "e" * 64),
    )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def test_inline_stack_projection_emits_qualified_owned_unit_and_context() -> None:
    compiler = _compiler()
    delta = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "2" * 64), _stack()),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )

    assert {item.key for item in delta.writes} == {
        "stack-templates/template.json",
        "stacks/application.json",
        "units/application/database.json",
    }
    child = next(item for item in delta.writes if item.key == "units/application/database.json")
    assert child.identity == ("unit.gitopsctr.io/v1", "Terraform", "application/database")
    metadata = child.document["metadata"]
    assert isinstance(metadata, Mapping)
    owners = metadata["ownerReferences"]
    assert isinstance(owners, tuple | list) and len(owners) == 1 and isinstance(owners[0], Mapping)
    assert owners[0]["name"] == "application"
    assert delta.payload_prefixes == (".gitopsctr/projection-contexts",)
    assert len(delta.payload_writes) == 1
    template = next(item for item in delta.writes if item.key == "stack-templates/template.json")
    spec = template.document["spec"]
    assert isinstance(spec, Mapping)
    acquisition = spec["acquisition"]
    assert isinstance(acquisition, Mapping)
    # The digest is the frozen authored YAML/JSON byte identity, never a
    # canonical reserialization performed by structural projection.
    assert acquisition["documentDigest"] == "sha256:" + "1" * 64


def test_same_name_finalized_tombstone_issues_a_new_root_incarnation() -> None:
    compiler = _compiler()
    initial = compiler.project(
        (FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )
    first = initial.writes[0]
    first_metadata = first.document["metadata"]
    assert isinstance(first_metadata, Mapping) and isinstance(first_metadata["uid"], str)
    recreated_context = replace(
        _context(),
        finalized_tombstones=(
            FinalizedTombstone("gitopsctr.io/v1", "StackTemplate", "template", first_metadata["uid"]),
        ),
    )
    recreated = compiler.project(
        (FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        recreated_context,
    )
    recreated_metadata = recreated.writes[0].document["metadata"]
    assert isinstance(recreated_metadata, Mapping)
    assert recreated_metadata["uid"] != first_metadata["uid"]


def test_template_update_prunes_obsolete_owned_fanout() -> None:
    compiler = _compiler()
    initial = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "2" * 64), _stack()),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )
    current = {item.identity: item for item in initial.writes}
    current_workspace = InMemoryWorkspace(
        tuple(
            WorkspaceEntry.file(
                item.key,
                json.dumps(item.document, default=dict, sort_keys=True, separators=(",", ":")).encode(),
            )
            for item in initial.writes
        ),
        mutable=False,
    )
    edited = _template()
    edited["spec"] = {
        "parameters": [],
        "unitTemplates": {
            "cache": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            }
        },
    }
    delta = compiler.project(
        (FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "3" * 64), edited),),
        current,
        current_workspace,
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )

    assert "units/application/database.json" in delta.deletes
    assert "units/application/cache.json" in {item.key for item in delta.writes}
    assert "stacks/application.json" in {item.key for item in delta.writes}


def test_repository_template_requires_exact_retained_descriptor() -> None:
    document = _template()
    document["spec"] = {"source": {"fromGit": {"repository": "other", "revision": "main", "path": "template.json"}}}
    compiler = _compiler()
    try:
        compiler.project(
            (FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), document),),
            {},
            InMemoryWorkspace(mutable=False),
            (_source(),),
            InMemoryWorkspace(mutable=False),
            _context(),
        )
    except ProjectionCompilerError as exc:
        assert "retained source descriptor" in str(exc)
    else:
        raise AssertionError("missing descriptor should fail closed")


def test_multiple_selected_stacks_share_one_projection_context_payload() -> None:
    compiler = _compiler()
    delta = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),
            FrozenAuthoredDocument.from_change("one", ContentId("sha256:" + "2" * 64), _stack("application")),
            FrozenAuthoredDocument.from_change("two", ContentId("sha256:" + "3" * 64), _stack("worker")),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )

    assert len(delta.payload_writes) == 1


def test_template_fanout_preserves_carried_stack_projection_context() -> None:
    compiler = _compiler()
    original_context = _context()
    initial = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "2" * 64), _stack()),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        original_context,
    )
    current = {item.identity: item for item in initial.writes}
    current_workspace = InMemoryWorkspace(
        tuple(
            WorkspaceEntry.file(
                item.key,
                json.dumps(item.document, default=dict, sort_keys=True, separators=(",", ":")).encode(),
            )
            for item in initial.writes
        ),
        mutable=False,
    )
    changed_context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(b"different-project", b"different-environment"),
        primary_source=_source().descriptors[0],
        root_identity_issuer=HmacRootIncarnationIssuer("test-root-issuer", "test-root-identity-seed"),
    )
    edited = _template()
    edited["spec"] = {
        "parameters": [],
        "unitTemplates": {
            "cache": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            }
        },
    }
    delta = compiler.project(
        (FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "4" * 64), edited),),
        current,
        current_workspace,
        (_source(),),
        InMemoryWorkspace(mutable=False),
        changed_context,
    )
    original_stack = current[("gitopsctr.io/v1", "Stack", "application")]
    carried = next(item for item in delta.writes if item.key == "stacks/application.json")
    original_spec = original_stack.document["spec"]
    carried_spec = carried.document["spec"]
    assert isinstance(original_spec, Mapping) and isinstance(carried_spec, Mapping)
    original_projection = original_spec["structuralProjection"]
    carried_projection = carried_spec["structuralProjection"]
    assert isinstance(original_projection, Mapping) and isinstance(carried_projection, Mapping)
    original_identity = original_projection["identity"]
    carried_identity = carried_projection["identity"]
    assert isinstance(original_identity, Mapping) and isinstance(carried_identity, Mapping)
    assert carried_identity["projectionContextDigest"] == original_identity["projectionContextDigest"]
    assert not delta.payload_writes


def test_promoted_template_uses_verified_git_lineage_and_preserves_noop() -> None:
    compiler = _compiler()
    initial = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), _template()),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "2" * 64), _stack()),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        _context(),
    )
    initial_documents = {item.key: item for item in initial.writes}
    source_stack = CATALOG.parse_stack(
        initial_documents["stacks/application.json"].mutable_document(), profile="desired"
    )
    source_unit = CATALOG.parse_unit(
        initial_documents["units/application/database.json"].mutable_document(), profile="desired"
    )
    assert isinstance(source_stack, StackResource)
    assert isinstance(source_stack.spec, DesiredStackSpec)
    source_projection = source_stack.spec.structuralProjection.identity
    source_stack_active = StackActiveProjection.build(
        source_projection_digest=source_projection.projectionDigest,
        projection_context_digest=source_projection.projectionContextDigest,
        units={
            "database": StackProjectionUnitBinding(
                apiVersion=source_unit.gvk.api_version,
                kind=source_unit.gvk.kind,
                name=source_unit.name,
                uid=source_unit.metadata.uid or "",
                desiredDigest=desired_unit_binding_digest(source_unit),
                sourceProjectionDigest=source_projection.projectionDigest,
                projectionContextDigest=source_projection.projectionContextDigest,
            )
        },
    )
    assert source_unit.metadata.uid is not None
    active_stack = StackResource(
        source_stack.gvk, source_stack.metadata, replace(source_stack.spec, activeProjection=source_stack_active)
    )
    source_workspace = InMemoryWorkspace(
        tuple(
            WorkspaceEntry.file(
                item.key,
                json.dumps(
                    _plain(CATALOG.serialize_stack_resource(active_stack, profile="desired")), sort_keys=True
                ).encode()
                if item.key == "stacks/application.json"
                else json.dumps(_plain(item.document), sort_keys=True).encode(),
            )
            for item in initial.writes
        ),
        mutable=False,
    )
    descriptor = _promotion_descriptor(source_workspace)
    context = _context()
    frozen = context.projection_context
    assert frozen is not None
    context = ApplyProjectionContext(
        context.environment_id,
        context.desired_channel,
        context.observed_channel,
        context.candidate_channel,
        context.policy,
        projection_context=WorkspaceProjectionContext(
            frozen.project_document,
            frozen.environment_document,
            promotion_source=descriptor,
        ),
        primary_source=_source().descriptors[0],
        root_identity_issuer=HmacRootIncarnationIssuer("test-root-issuer", "test-root-identity-seed"),
    )
    promoted: JsonObject = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "template"},
        "spec": {"source": {"fromPromotion": {"stack": "application"}}},
    }
    compiler = _compiler(_promotion_encoder())
    delta = compiler.project(
        (FrozenAuthoredDocument.from_change("promotion", ContentId("sha256:" + "9" * 64), promoted),),
        {},
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        context,
    )
    desired = delta.writes[0].document["spec"]
    assert isinstance(desired, Mapping)
    acquired = desired["acquisition"]
    assert isinstance(acquired, Mapping)
    resolved = acquired["resolvedSource"]
    assert isinstance(resolved, Mapping)
    promotion = resolved["fromPromotion"]
    assert isinstance(promotion, Mapping)
    assert promotion["desiredRef"] == "desired/staging"
    assert promotion["desiredRevision"] == "1" * 40

    current = {item.identity: item for item in delta.writes}
    repeated = compiler.project(
        (FrozenAuthoredDocument.from_change("promotion", ContentId("sha256:" + "9" * 64), promoted),),
        current,
        InMemoryWorkspace(mutable=False),
        (_source(),),
        InMemoryWorkspace(mutable=False),
        context,
    )
    assert repeated.writes[0] is current[("gitopsctr.io/v1", "StackTemplate", "template")]


def test_git_promotion_encoder_rejects_policy_foreign_and_tampered_evidence() -> None:
    descriptor = _promotion_descriptor(InMemoryWorkspace(mutable=False))
    blocked = GitPromotionLineageEncoder(
        desired_refs=_promotion_encoder().desired_refs,
        observed_refs=_promotion_encoder().observed_refs,
        allowed_sources={EnvironmentId("dev"): frozenset()},
    )
    try:
        blocked.encode(descriptor)
    except GitPromotionLineageError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("policy conflict should fail")

    foreign = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        _plane("desired/staging", "foreign", InMemoryWorkspace(mutable=False)),
        _git_plane("observed/staging", "2" * 40, InMemoryWorkspace(mutable=False)),
        _git_plane("desired/dev", "3" * 40, InMemoryWorkspace(mutable=False)),
        _git_plane("observed/dev", "4" * 40, InMemoryWorkspace(mutable=False)),
        ContentId("sha256:" + "e" * 64),
    )
    try:
        _promotion_encoder().encode(foreign)
    except GitPromotionLineageError as exc:
        assert "git-commit" in str(exc)
    else:
        raise AssertionError("foreign snapshot should fail")

    object.__setattr__(descriptor, "source_environment", EnvironmentId("other"))
    try:
        _promotion_encoder().encode(descriptor)
    except TypeError as exc:
        assert "modified" in str(exc)
    else:
        raise AssertionError("tampered descriptor should fail")


def test_git_promotion_encoder_rejects_noncanonical_git_refs() -> None:
    for reference in ("a..b", "topic.lock", "refs//heads/main", ".hidden", "main@{1}"):
        try:
            GitPromotionLineageEncoder(
                desired_refs={ChannelId("desired/staging"): reference, ChannelId("desired/dev"): "desired/dev"},
                observed_refs={
                    ChannelId("observed/staging"): "observed/staging",
                    ChannelId("observed/dev"): "observed/dev",
                },
                allowed_sources={EnvironmentId("dev"): frozenset((EnvironmentId("staging"),))},
            )
        except GitPromotionLineageError:
            continue
        raise AssertionError(f"invalid Git ref {reference!r} should fail")


def test_git_unit_source_selector_uses_exact_workload_revision_and_binding() -> None:
    primary = _source()
    workload = _issue_retained_source_descriptor(
        primary.retained,
        "workload-app",
        SourceBindingRole.WORKLOAD,
        "workloads/app",
        ContentId("sha256:" + "c" * 64),
    )
    plane = RetainedSourcePlane(primary.retained, primary.plane, (primary.descriptors[0], workload))
    document: JsonObject = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": "app"},
        "spec": {"source": {"path": ".", "revision": "a" * 40}},
    }
    unit = CATALOG.parse_unit(document, profile="authored")
    selector = GitUnitSourceSelector(_source_encoder(), {"app": "workload-app"})
    selected = selector.select(
        UnitSourceSelectionRequest(
            "app",
            unit,
            {"path": ".", "revision": "a" * 40},
            None,
            primary.descriptors[0],
            (workload,),
            (plane,),
        )
    )
    assert selected.descriptor is workload
    assert selected.plane is plane


def test_template_resolution_session_reads_exact_observed_receipt() -> None:
    observed = InMemoryWorkspace(
        (
            WorkspaceEntry.file(
                "units/producer.json",
                json.dumps(
                    {
                        "apiVersion": "gitopsctr.io/v1",
                        "kind": "Receipt",
                        "metadata": {"name": "producer"},
                        "status": {"result": {"outputs": {"value": "ready"}}},
                    }
                ).encode(),
            ),
        ),
        mutable=False,
    )
    unit = CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "consumer"},
            "spec": {"source": {"path": "."}},
        },
        profile="authored",
    )
    session = TemplateResolutionSession.begin(CATALOG, observed)
    with pytest.raises(Exception, match="not selected"):
        session.resolve(
            {"fromReceipt": {"unit": "producer", "pointer": "/outputs/value"}},
            "/inputs/value",
            unit=unit,
            context=_context(),
        )


def test_promoted_artifact_receipt_mismatch_is_a_projection_error() -> None:
    """Promoted evidence never leaks contracts/resource parsing failures."""

    unit = CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "producer"},
            "spec": {"source": {"path": "."}},
        },
        profile="authored",
    )
    artifact = ArtifactImport(
        unit="producer",
        name="output",
        apiVersion="artifact.gitopsctr.io/v1",
        kind="Expected",
        fromPromotion=PromotionStackReference(stack="source"),
    )
    with pytest.raises(ProjectionCompilerError, match="descriptor does not identify"):
        _parse_promoted_artifact_receipt(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Receipt",
                "metadata": {"name": "producer"},
                "spec": {
                    "subject": {
                        "apiVersion": "unit.gitopsctr.io/v1",
                        "kind": "Terraform",
                        "name": "producer",
                        "qualifiedName": "source/producer",
                    },
                    "desired": {"unitContentId": "sha256:" + "a" * 64},
                },
                "status": {
                    "controller": {},
                    "result": {},
                    "artifacts": {
                        "output": {
                            "apiVersion": "artifact.gitopsctr.io/v1",
                            "kind": "Other",
                            "path": "artifacts/source/producer/output.json",
                            "digest": "sha256:" + "b" * 64,
                            "mediaType": "application/json",
                        }
                    },
                },
            },
            unit=unit,
            qualified_name="source/producer",
            artifact=artifact,
        )
