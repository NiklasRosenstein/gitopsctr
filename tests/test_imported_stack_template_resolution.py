"""Stack-level consumption and persistence of authenticated artifact imports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.application.apply_compilers import (
    ArtifactImportRequest,
    ArtifactImportResolution,
    CatalogApplyDocumentValidator,
    CatalogStackProjectionCompiler,
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
from gitopsctr.contracts import DesiredSource, DesiredStackSpec, ResolvedArtifactImport
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.document import JsonObjectValue, ResolvedJsonObjectValue
from gitopsctr.driver import reference_fingerprints
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import ResourceCatalog, UnitResource
from gitopsctr.templates import TemplateValue, dump_template_value

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
SOURCE_REVISION = "a" * 40
OBSERVED_REVISION = "b" * 40
TARGET_REVISION = "c" * 40


def _plane(channel: str, revision: str, workspace: InMemoryWorkspace) -> ExactPlane:
    snapshot = SnapshotId(f"git-commit:{revision}")
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _promotion_source():  # type: ignore[no-untyped-def]
    empty = InMemoryWorkspace(mutable=False)
    return _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        _plane("desired/staging", SOURCE_REVISION, empty),
        _plane("observed/staging", OBSERVED_REVISION, empty),
        _plane("desired/dev", TARGET_REVISION, empty),
        _plane("observed/dev", TARGET_REVISION, empty),
        ContentId("sha256:" + "1" * 64),
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


def _primary() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("main.tf", b"terraform {}\n"),), mutable=False)
    snapshot = SnapshotId("git-source:" + TARGET_REVISION)
    retained = _issue_retained_source(
        RetainedSourceHandle("primary"),
        RetentionStoreId("primary-store"),
        SourceSnapshotId(SourceId("primary-source"), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "primary-authored",
        SourceBindingRole.PRIMARY_AUTHORED,
        "main.tf",
        ContentId("sha256:" + "2" * 64),
    )
    plane = ExactPlane(
        HeadObservation.present(ChannelId("source"), snapshot, "source-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )
    return RetainedSourcePlane(retained, plane, (descriptor,))


@dataclass(frozen=True)
class _ImportedResolver:
    artifact_document: JsonObject

    def resolve(self, request: ArtifactImportRequest) -> ArtifactImportResolution:
        lineage = request.lineage
        assert request.target_stack.metadata.uid is not None
        return ArtifactImportResolution(
            request,
            ResolvedArtifactImport(
                sourceStack="source",
                sourceStackUid="d1-source",
                sourceUnit=request.artifact_import.unit,
                sourceUnitUid="d1-source-image",
                sourceDesiredRevision=lineage.source_desired_revision,
                sourceObservedRevision=lineage.source_observed_revision,
                receiptUnitContentId="sha256:" + "3" * 64,
                artifactName=request.artifact_import.name,
                apiVersion=request.artifact_import.apiVersion,
                kind=request.artifact_import.kind,
                artifactDigest="sha256:" + "4" * 64,
                targetStackUid=request.target_stack.metadata.uid,
                artifactDocument=JsonObjectValue(self.artifact_document),
            ),
        )


@dataclass(frozen=True)
class _ResolvingProjector:
    def project_unit(
        self,
        unit,
        *,
        metadata,
        previous,
        current_workspace,
        retained_sources,
        observed,
        context,
        session=None,
    ):  # type: ignore[no-untyped-def]
        del previous, current_workspace, retained_sources, observed
        assert isinstance(session, TemplateResolutionSession)
        resolved = session.resolve(
            dump_template_value(cast(TemplateValue, unit.spec.inputs)),
            "/inputs",
            unit=unit,
            context=context,
        )
        assert isinstance(resolved.value, dict)
        source = DesiredSource(
            path=".",
            revision=TARGET_REVISION,
            driverVersion=unit.driver.version,
            inputHash="sha256:" + "5" * 64,
        )
        return UnitProjection(
            UnitResource(
                unit.gvk,
                metadata,
                unit.driver,
                TerraformDesiredUnit(
                    source=source,
                    inputs=ResolvedJsonObjectValue(resolved.value),
                    resolvedInputs=reference_fingerprints(resolved),
                ),
            )
        )


def test_stack_consumes_imported_artifact_and_persists_exact_evidence() -> None:
    artifact: JsonObject = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "images": {"application": {"uri": "registry.example/app@sha256:" + "6" * 64}},
    }
    template: JsonObject = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "template"},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "deploy": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {
                        "source": {"path": "."},
                        "inputs": {
                            "image": {
                                "fromArtifact": {
                                    "unit": "image",
                                    "name": "containers",
                                    "apiVersion": "artifact.gitopsctr.io/v1",
                                    "kind": "ContainerImages",
                                    "pointer": "/images/application/uri",
                                }
                            }
                        },
                    },
                }
            },
        },
    }
    stack: JsonObject = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "target"},
        "spec": {
            "template": "template",
            "artifactImports": [
                {
                    "unit": "image",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "fromPromotion": {"stack": "source"},
                }
            ],
        },
    }
    primary = _primary()
    promotion_source = _promotion_source()
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(
            b"kind: Project\n", b"kind: Environment\n", promotion_source=promotion_source
        ),
        primary_source=primary.descriptors[0],
        root_identity_issuer=HmacRootIncarnationIssuer("imported-stack", "imported-stack-seed"),
    )
    compiler = CatalogStackProjectionCompiler(
        CATALOG,
        _ResolvingProjector(),
        promotion_encoder=_promotion_encoder(),
        source_encoder=GitSourceLineageEncoder({SourceId("primary-source"): "."}),
        artifact_import_resolver=_ImportedResolver(artifact),
    )
    delta = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "7" * 64), template),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "8" * 64), stack),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (primary,),
        InMemoryWorkspace(mutable=False),
        context,
    )
    stack_resource = CATALOG.parse_stack(
        next(item.mutable_document() for item in delta.writes if item.key == "stacks/target.json"),
        profile="desired",
    )
    unit = CATALOG.parse_unit(
        next(item.mutable_document() for item in delta.writes if item.key == "units/target/deploy.json"),
        profile="desired",
    )
    assert isinstance(stack_resource.spec, DesiredStackSpec)
    assert stack_resource.spec.resolvedArtifactImports is not None
    evidence = stack_resource.spec.resolvedArtifactImports["image/containers"]
    assert evidence.artifactDocument == artifact
    assert unit.spec.inputs == {"image": "registry.example/app@sha256:" + "6" * 64}
    assert unit.spec.resolvedInputs is not None
    assert unit.spec.resolvedInputs.importedArtifacts == {"image/containers": "sha256:" + "4" * 64}
    assert unit.spec.resolvedInputs.importedArtifactEvidence == {"image/containers": evidence.to_dict()}

    workspace = InMemoryWorkspace(
        tuple(WorkspaceEntry.file(item.key, json.dumps(item.document, default=dict).encode()) for item in delta.writes),
        mutable=False,
    )
    CatalogApplyDocumentValidator(CATALOG).validate_workspace(workspace)
