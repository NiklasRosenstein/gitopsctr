"""High-value production compiler coverage outside the parity fixtures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

import pytest

from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    CatalogUnitProjectionCompiler,
    PendingTemplateReference,
    ProjectionCompilerError,
    TemplateResolutionSession,
    UnitProjection,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    FrozenAuthoredDocument,
    HmacRootIncarnationIssuer,
    RetainedSourcePlane,
    SourceBindingRole,
    WorkspaceProjectionContext,
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
from gitopsctr.contracts import DesiredSource, DesiredStackSpec
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, ResourceMetadata, UnitResource, desired_unit_binding_digest

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
_SHA = "a" * 40


class _Projector:
    """Small logical host that lets the real Stack compiler own expansion."""

    def project_unit(
        self, unit, *, metadata, previous, current_workspace, retained_sources, observed, context, session=None
    ):  # type: ignore[no-untyped-def]
        del previous, current_workspace, retained_sources, observed, context, session
        return UnitProjection(
            UnitResource(
                unit.gvk,
                metadata,
                unit.driver,
                TerraformDesiredUnit(source=DesiredSource(path=".", revision=_SHA, inputHash="sha256:" + "b" * 64)),
            )
        )


class _CapturingHost:
    request: object | None = None

    def project(self, request):  # type: ignore[no-untyped-def]
        self.request = request
        assert request.source is not None
        return UnitProjection(
            UnitResource(
                request.unit.gvk,
                request.metadata,
                request.unit.driver,
                TerraformDesiredUnit(source=request.source),
            )
        )


def _plane(channel: str, snapshot: SnapshotId, workspace: InMemoryWorkspace) -> ExactPlane:
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, "incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _context(*, named_sources=(), primary_source=None):  # type: ignore[no-untyped-def]
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(b"kind: Project\n", b"kind: Environment\n"),
        primary_source=primary_source,
        named_sources=named_sources,
        root_identity_issuer=HmacRootIncarnationIssuer("coverage", "coverage-root-seed"),
    )


def _template(*, source: object | None = None, unit_templates: Mapping[str, object] | None = None) -> JsonObject:
    spec: dict[str, object] = {
        "parameters": [],
        "unitTemplates": unit_templates
        or {
            "unit": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            }
        },
    }
    if source is not None:
        spec = {"source": source}
    return cast(
        JsonObject,
        {"apiVersion": "gitopsctr.io/v1", "kind": "StackTemplate", "metadata": {"name": "template"}, "spec": spec},
    )


def _stack(*, units: list[str] | None = None) -> JsonObject:
    spec: dict[str, object] = {"template": "template"}
    if units is not None:
        spec["units"] = units
    return cast(
        JsonObject, {"apiVersion": "gitopsctr.io/v1", "kind": "Stack", "metadata": {"name": "app"}, "spec": spec}
    )


def _retained_template(raw: bytes) -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("templates/template.json", raw),), mutable=False)
    snapshot = SnapshotId("git-source:" + _SHA)
    retained = _issue_retained_source(
        RetainedSourceHandle("coverage-template"),
        RetentionStoreId("coverage-store"),
        SourceSnapshotId(SourceId("coverage-source"), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "template",
        SourceBindingRole.STACK_TEMPLATE,
        "templates/template.json",
        ContentId("sha256:" + "c" * 64),
    )
    primary = _issue_retained_source_descriptor(
        retained,
        "primary-source",
        SourceBindingRole.PRIMARY_AUTHORED,
        "templates/template.json",
        ContentId("sha256:" + "e" * 64),
    )
    return RetainedSourcePlane(retained, _plane("source", snapshot, workspace), (descriptor, primary))


def _compiler() -> CatalogStackProjectionCompiler:
    return CatalogStackProjectionCompiler(
        CATALOG,
        _Projector(),
        source_encoder=GitSourceLineageEncoder({SourceId("coverage-source"): "https://example.test/templates.git"}),
    )


def _frozen(name: str, document: JsonObject, digit: str) -> FrozenAuthoredDocument:
    return FrozenAuthoredDocument.from_change(name, ContentId("sha256:" + digit * 64), document)


def test_git_template_acquisition_uses_exact_retained_bytes_and_fences_digest() -> None:
    retained_document = _template()
    raw = json.dumps(retained_document, separators=(",", ":")).encode()
    source = _retained_template(raw)
    request = _template(
        source={
            "fromGit": {
                "repository": "https://example.test/templates.git",
                "revision": "main",
                "path": "templates/template.json",
            }
        }
    )
    delta = _compiler().project(
        (_frozen("git-template", request, "1"), _frozen("stack", _stack(), "2")),
        {},
        InMemoryWorkspace(mutable=False),
        (source,),
        InMemoryWorkspace(mutable=False),
        _context(named_sources=(source.descriptors[0],)),
    )
    template = next(item for item in delta.writes if item.key == "stack-templates/template.json")
    spec = cast(Mapping[str, object], template.document["spec"])
    acquisition = cast(Mapping[str, object], spec["acquisition"])
    assert acquisition["documentDigest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    resolved_source = cast(Mapping[str, object], acquisition["resolvedSource"])
    assert cast(Mapping[str, object], resolved_source["fromGit"])["revision"] == _SHA
    assert any(item.key == "units/app/unit.json" for item in delta.writes)

    bad_request = _template(
        source={
            "fromGit": {
                "repository": "https://example.test/templates.git",
                "revision": "main",
                "path": "templates/template.json",
                "documentDigest": "sha256:" + "d" * 64,
            }
        }
    )
    with pytest.raises(ProjectionCompilerError, match="documentDigest"):
        _compiler().project(
            (_frozen("git-template", bad_request, "3"),),
            {},
            InMemoryWorkspace(mutable=False),
            (source,),
            InMemoryWorkspace(mutable=False),
            _context(named_sources=(source.descriptors[0],)),
        )


def test_stack_selection_rejects_unknown_or_unselected_dependencies() -> None:
    units = {
        "database": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "spec": {"source": {"path": "."}}},
        "api": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "dependsOn": ["database"],
            "spec": {"source": {"path": "."}},
        },
    }
    template = _template(unit_templates=units)
    compiler = _compiler()
    source = _retained_template(b"{}")
    for selected, expected in ((["missing"], "unknown Unit"), (["api"], "omits dependencies")):
        with pytest.raises(ProjectionCompilerError, match=expected):
            compiler.project(
                (_frozen("template", template, "4"), _frozen("stack", _stack(units=selected), "5")),
                {},
                InMemoryWorkspace(mutable=False),
                (source,),
                InMemoryWorkspace(mutable=False),
                _context(primary_source=source.descriptors[1]),
            )


def test_catalog_logical_projector_selects_issued_source_and_hashes_real_workspace_inputs() -> None:
    source = _retained_template(b"terraform {}\n")
    unit = CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "service"},
            "spec": {"source": {"path": ".", "inputs": ["**"]}},
        },
        profile="authored",
    )
    host = _CapturingHost()
    projector = CatalogLogicalUnitProjector(
        CATALOG,
        GitSourceLineageEncoder({SourceId("coverage-source"): "https://example.test/templates.git"}),
        host,
    )
    output = projector.project_unit(
        unit,
        metadata=ResourceMetadata(name="service", uid="d1-service"),
        previous=None,
        current_workspace=InMemoryWorkspace(mutable=False),
        retained_sources=(source,),
        observed=InMemoryWorkspace(mutable=False),
        context=_context(primary_source=source.descriptors[1]),
    )
    assert host.request is not None
    assert output.unit.spec.source.revision == _SHA
    assert output.unit.spec.source.inputHash is not None
    assert output.unit.spec.source.inputHash.startswith("sha256:")


def test_catalog_logical_projector_rejects_unissued_or_missing_primary_source_evidence() -> None:
    source = _retained_template(b"terraform {}\n")
    unit = CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "service"},
            "spec": {"source": {"path": "."}},
        },
        profile="authored",
    )
    projector = CatalogLogicalUnitProjector(
        CATALOG,
        GitSourceLineageEncoder({SourceId("coverage-source"): "https://example.test/templates.git"}),
        _CapturingHost(),
    )
    with pytest.raises(ProjectionCompilerError, match="one explicit primary"):
        projector.project_unit(
            unit,
            metadata=ResourceMetadata(name="service", uid="d1-service"),
            previous=None,
            current_workspace=InMemoryWorkspace(mutable=False),
            retained_sources=(source,),
            observed=InMemoryWorkspace(mutable=False),
            context=_context(),
        )


def test_unit_projection_compiler_emits_typed_unit_and_preserves_semantic_noop_document() -> None:
    document = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": "standalone"},
        "spec": {"source": {"path": "."}},
    }
    compiler = CatalogUnitProjectionCompiler(CATALOG, _Projector())
    initial = compiler.project(
        (_frozen("unit", cast(JsonObject, document), "7"),),
        {},
        InMemoryWorkspace(mutable=False),
        (),
        InMemoryWorkspace(mutable=False),
        _context(),
    )
    assert initial.writes[0].key == "units/standalone.json"
    repeated = compiler.project(
        (_frozen("unit", cast(JsonObject, document), "7"),),
        {initial.writes[0].identity: initial.writes[0]},
        InMemoryWorkspace(mutable=False),
        (),
        InMemoryWorkspace(mutable=False),
        _context(),
    )
    assert repeated.writes[0] is initial.writes[0]


def test_catalog_validator_accepts_real_desired_stack_template_workspace() -> None:
    source = _retained_template(b"terraform {}\n")
    delta = _compiler().project(
        (_frozen("template", _template(), "8"),),
        {},
        InMemoryWorkspace(mutable=False),
        (source,),
        InMemoryWorkspace(mutable=False),
        _context(primary_source=source.descriptors[1]),
    )
    workspace = InMemoryWorkspace(
        tuple(WorkspaceEntry.file(item.key, json.dumps(item.document, default=dict).encode()) for item in delta.writes),
        mutable=False,
    )
    CatalogApplyDocumentValidator(CATALOG).validate_workspace(workspace)


def test_stack_compiler_emits_exact_active_projection_for_concrete_units() -> None:
    source = _retained_template(b"terraform {}\n")
    delta = _compiler().project(
        (
            _frozen("template", _template(), "8"),
            _frozen("stack", _stack(), "9"),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (source,),
        InMemoryWorkspace(mutable=False),
        _context(primary_source=source.descriptors[1]),
    )
    workspace = InMemoryWorkspace(
        tuple(WorkspaceEntry.file(item.key, json.dumps(item.document, default=dict).encode()) for item in delta.writes),
        mutable=False,
    )
    CatalogApplyDocumentValidator(CATALOG).validate_workspace(workspace)
    stack = CATALOG.parse_stack(
        next(item.mutable_document() for item in delta.writes if item.key == "stacks/app.json"),
        profile="desired",
    )
    unit = CATALOG.parse_unit(
        next(item.mutable_document() for item in delta.writes if item.key == "units/app/unit.json"),
        profile="desired",
    )
    assert isinstance(stack.spec, DesiredStackSpec)
    assert stack.spec.activeProjection is not None
    assert (
        stack.spec.activeProjection.sourceProjectionDigest == stack.spec.structuralProjection.identity.projectionDigest
    )
    binding = stack.spec.activeProjection.units["unit"]
    assert binding.uid == unit.metadata.uid
    assert binding.desiredDigest == desired_unit_binding_digest(unit)


def test_stack_compiler_never_activates_partially_resolved_units() -> None:
    class _BlockingProjector(_Projector):
        def project_unit(self, unit, **kwargs):  # type: ignore[no-untyped-def]
            if unit.name == "blocked":
                raise PendingTemplateReference("blocked has no current receipt")
            return super().project_unit(unit, **kwargs)

    source = _retained_template(b"terraform {}\n")
    compiler = CatalogStackProjectionCompiler(
        CATALOG,
        _BlockingProjector(),
        source_encoder=GitSourceLineageEncoder({SourceId("coverage-source"): "https://example.test/templates.git"}),
    )
    with pytest.raises(ProjectionCompilerError, match="blocked or cyclic"):
        compiler.project(
            (
                _frozen(
                    "template",
                    _template(
                        unit_templates={
                            "ready": {
                                "apiVersion": "unit.gitopsctr.io/v1",
                                "kind": "Terraform",
                                "spec": {"source": {"path": "."}},
                            },
                            "blocked": {
                                "apiVersion": "unit.gitopsctr.io/v1",
                                "kind": "Terraform",
                                "spec": {"source": {"path": "."}},
                            },
                        }
                    ),
                    "8",
                ),
                _frozen("stack", _stack(), "9"),
            ),
            {},
            InMemoryWorkspace(mutable=False),
            (source,),
            InMemoryWorkspace(mutable=False),
            _context(primary_source=source.descriptors[1]),
        )


def test_template_session_fingerprints_artifacts_and_distinguishes_pending_from_unknown() -> None:
    artifact = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "Output",
        "metadata": {"name": "value"},
        "spec": {"answer": 42},
    }
    observed = InMemoryWorkspace(
        (WorkspaceEntry.file("artifacts/producer/value.json", json.dumps(artifact).encode()),), mutable=False
    )
    consumer = CATALOG.parse_unit(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "consumer"},
            "spec": {"source": {"path": "."}},
        },
        profile="authored",
    )
    session = TemplateResolutionSession.begin(CATALOG, observed)
    session.declare("producer")
    with pytest.raises(PendingTemplateReference, match="pending projection"):
        session.resolve(
            {
                "fromArtifact": {
                    "unit": "producer",
                    "name": "value",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "Output",
                    "pointer": "/spec/answer",
                }
            },
            "/input",
            unit=consumer,
            context=_context(),
        )
    session.declare("pending")
    with pytest.raises(PendingTemplateReference, match="pending"):
        session.resolve(
            {"fromReceipt": {"unit": "pending", "pointer": ""}}, "/input", unit=consumer, context=_context()
        )
    with pytest.raises(Exception) as unknown:
        session.resolve(
            {"fromReceipt": {"unit": "unknown", "pointer": ""}}, "/input", unit=consumer, context=_context()
        )
    assert not isinstance(unknown.value, PendingTemplateReference)
    with pytest.raises(Exception, match="must start"):
        session.resolve(
            {
                "fromArtifact": {
                    "unit": "producer",
                    "name": "value",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "Output",
                    "pointer": "not-a-pointer",
                }
            },
            "/input",
            unit=consumer,
            context=_context(),
        )


def test_catalog_validator_rejects_candidate_symlinks_and_noncanonical_resource_paths() -> None:
    validator = CatalogApplyDocumentValidator(CATALOG)
    with pytest.raises(ProjectionCompilerError, match="symbolic links"):
        validator.validate_workspace(
            InMemoryWorkspace((WorkspaceEntry.symlink("units/link", "target"),), mutable=False)
        )
    with pytest.raises(ProjectionCompilerError, match="non-canonical"):
        validator.validate_workspace(
            InMemoryWorkspace((WorkspaceEntry.file("units/app/nested/unit.json", b"{}"),), mutable=False)
        )
    with pytest.raises(ProjectionCompilerError):
        validator.validate_authored(
            {"apiVersion": "invalid", "kind": "Terraform", "metadata": {"name": "bad"}, "spec": {}}
        )
