"""Independent integration coverage for imported and persisted template evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, cast

import pytest
import yaml

from gitopsctr.adapters.filesystem.unit_projection import FilesystemUnitProjectionHost
from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelector
from gitopsctr.application.apply_compilers import (
    CanonicalArtifactImportResolver,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    PendingTemplateReference,
    ProjectionCompilerError,
    TemplateResolutionSession,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    FrozenAuthoredDocument,
    HmacRootIncarnationIssuer,
    ProjectedDocument,
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
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry, entry_content_id
from gitopsctr.contracts import (
    ArtifactImport,
    DesiredOwnerReference,
    DesiredSource,
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    ProjectionObject,
    PromotionStackReference,
    StackActiveProjection,
    StackProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateInlineSpec,
    StackTemplateReference,
    StackTemplateRequestedFromInput,
    StackTemplateResolvedFromInput,
    StackTemplateSourceContext,
    StackTemplateUnitTemplate,
)
from gitopsctr.contrib.drivers.oci_images import OciImagesDesiredUnit
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.document import JsonObjectValue
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import GVK, JsonObject
from gitopsctr.resources import (
    CORE_API_VERSION,
    ResourceCatalog,
    ResourceMetadata,
    StackResource,
    UnitResource,
    desired_unit_binding_digest,
)
from gitopsctr.templates import TemplateObject

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
REVISION = "a" * 40
INPUT_HASH = "sha256:" + "b" * 64


def _context() -> ApplyProjectionContext:
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
    )


def _root_producer() -> UnitResource[Any]:
    driver = UNIT_DRIVERS["oci-images"]
    return UnitResource(
        GVK(driver.api_version, driver.kind),
        ResourceMetadata(name="image", uid="d1-image"),
        driver,
        OciImagesDesiredUnit(
            source=DesiredSource(path=".", revision=REVISION, driverVersion=driver.version, inputHash=INPUT_HASH)
        ),
    )


def _consumer() -> UnitResource[Any]:
    driver = UNIT_DRIVERS["terraform"]
    return UnitResource(
        GVK(driver.api_version, driver.kind),
        ResourceMetadata(name="deploy", uid="d1-deploy"),
        driver,
        TerraformDesiredUnit(
            source=DesiredSource(path=".", revision=REVISION, driverVersion=driver.version, inputHash=INPUT_HASH)
        ),
    )


def _projected(unit: UnitResource[Any]) -> ProjectedDocument:
    return ProjectedDocument(f"units/{unit.name}.json", CATALOG.serialize_unit(unit, profile="desired"))


def _json(document: JsonObject) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _entry(key: str, document: JsonObject, *, yaml_document: bool = False) -> WorkspaceEntry:
    raw = yaml.safe_dump(document, sort_keys=True).encode() if yaml_document else _json(document)
    return WorkspaceEntry.file(key, raw)


def _artifact() -> JsonObject:
    return {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": "image",
            "qualifiedName": "image",
            "driverVersion": UNIT_DRIVERS["oci-images"].version,
            "sourceRevision": REVISION,
            "inputHashVersion": 1,
            "inputHash": INPUT_HASH,
        },
        "images": {"application": {"uri": "registry.example/app@sha256:" + "c" * 64}},
    }


def _receipt(
    projected: ProjectedDocument,
    artifact_raw: bytes,
    *,
    unit_content_id: str | None = None,
) -> JsonObject:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Receipt",
        "metadata": {"name": "image"},
        "spec": {
            "subject": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "OciImages",
                "name": "image",
                "qualifiedName": "image",
            },
            "desired": {
                "unitContentId": unit_content_id
                or entry_content_id(WorkspaceEntry.file(projected.key, _json(projected.mutable_document()))).value
            },
        },
        "status": {
            "controller": {},
            "result": {},
            "artifacts": {
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": "artifacts/image/containers.json",
                    "digest": "sha256:" + hashlib.sha256(artifact_raw).hexdigest(),
                    "mediaType": "application/vnd.gitopsctr.container-images.v1+json",
                }
            },
        },
    }


def _session(*entries: WorkspaceEntry) -> tuple[TemplateResolutionSession, UnitResource[Any]]:
    producer = _root_producer()
    projected = _projected(producer)
    session = TemplateResolutionSession.begin(CATALOG, InMemoryWorkspace(entries, mutable=False))
    session.declare("image")
    session.record(producer, projected)
    return session, producer


def _current_session(*observed_entries: WorkspaceEntry) -> tuple[TemplateResolutionSession, UnitResource[Any]]:
    producer = _root_producer()
    projected = _projected(producer)
    unit_entry = WorkspaceEntry.file(projected.key, _json(projected.mutable_document()))
    return (
        TemplateResolutionSession.begin(
            CATALOG,
            InMemoryWorkspace(observed_entries, mutable=False),
            {projected.identity: projected},
            InMemoryWorkspace((unit_entry,), mutable=False),
        ),
        producer,
    )


def _artifact_reference() -> JsonObject:
    return {
        "fromArtifact": {
            "unit": "image",
            "name": "containers",
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "pointer": "/images/application/uri",
        }
    }


def test_existing_root_unit_receipt_and_artifact_references_keep_exact_fingerprints() -> None:
    producer = _root_producer()
    projected = _projected(producer)
    artifact = _artifact()
    artifact_raw = _json(artifact)
    receipt = _receipt(projected, artifact_raw)
    session, _ = _current_session(
        _entry("units/image.json", receipt),
        WorkspaceEntry.file("artifacts/image/containers.json", artifact_raw),
    )

    resolved_artifact = session.resolve(
        _artifact_reference(), "/terraform/variables/image", unit=_consumer(), context=_context()
    )
    resolved_receipt = session.resolve(
        {"fromReceipt": {"unit": "image", "pointer": ""}},
        "/inputs/image",
        unit=_consumer(),
        context=_context(),
    )

    assert resolved_artifact.value == "registry.example/app@sha256:" + "c" * 64
    assert resolved_artifact.artifacts["image/containers"] == "sha256:" + hashlib.sha256(artifact_raw).hexdigest()
    assert resolved_receipt.value == {}
    assert resolved_receipt.receipts["image"] == "sha256:" + hashlib.sha256(_json(receipt)).hexdigest()


def test_missing_and_stale_root_observations_remain_retryable_pending_evidence() -> None:
    producer = _root_producer()
    projected = _projected(producer)
    artifact = _artifact()
    artifact_raw = _json(artifact)
    stale = _receipt(projected, artifact_raw, unit_content_id="sha256:" + "0" * 64)

    missing, _ = _session()
    with pytest.raises(PendingTemplateReference, match="pending observed evidence"):
        missing.resolve(_artifact_reference(), "/image", unit=_consumer(), context=_context())

    stale_session, _ = _session(
        _entry("units/image.json", stale),
        WorkspaceEntry.file("artifacts/image/containers.json", artifact_raw),
    )
    with pytest.raises(PendingTemplateReference, match="stale"):
        stale_session.resolve(_artifact_reference(), "/image", unit=_consumer(), context=_context())

    stale_artifact = cast(dict[str, Any], _artifact())
    cast(dict[str, Any], stale_artifact["producer"])["sourceRevision"] = "d" * 40
    stale_artifact_raw = _json(cast(JsonObject, stale_artifact))
    fresh_receipt = _receipt(projected, stale_artifact_raw)
    artifact_session, _ = _session(
        _entry("units/image.json", fresh_receipt),
        WorkspaceEntry.file("artifacts/image/containers.json", stale_artifact_raw),
    )
    with pytest.raises(PendingTemplateReference, match="stale"):
        artifact_session.resolve(_artifact_reference(), "/image", unit=_consumer(), context=_context())


@pytest.mark.parametrize("duplicate", ("receipt", "artifact"))
def test_duplicate_json_yaml_observed_evidence_is_terminal_not_pending(duplicate: str) -> None:
    producer = _root_producer()
    projected = _projected(producer)
    artifact = _artifact()
    artifact_raw = _json(artifact)
    receipt = _receipt(projected, artifact_raw)
    entries = [
        _entry("units/image.json", receipt),
        WorkspaceEntry.file("artifacts/image/containers.json", artifact_raw),
    ]
    if duplicate == "receipt":
        entries.append(_entry("units/image.yaml", receipt, yaml_document=True))
    else:
        entries.append(_entry("artifacts/image/containers.yaml", artifact, yaml_document=True))
    session, _ = _session(*entries)

    with pytest.raises(ProjectionCompilerError, match="ambiguous") as failure:
        session.resolve(_artifact_reference(), "/image", unit=_consumer(), context=_context())
    assert not isinstance(failure.value, PendingTemplateReference)


def test_selected_local_candidate_freshness_is_not_replaced_by_a_stale_candidate() -> None:
    producer = _root_producer()
    projected = _projected(producer)
    stale = replace(
        producer,
        spec=OciImagesDesiredUnit(source=replace(producer.spec.source, inputHash="sha256:" + "e" * 64)),
    )
    artifact_raw = _json(_artifact())
    receipt = _receipt(projected, artifact_raw)
    current_entry = WorkspaceEntry.file(projected.key, _json(projected.mutable_document()))
    session = TemplateResolutionSession.begin(
        CATALOG,
        InMemoryWorkspace(
            (
                _entry("units/image.json", receipt),
                WorkspaceEntry.file("artifacts/image/containers.json", artifact_raw),
            ),
            mutable=False,
        ),
        {projected.identity: projected},
        InMemoryWorkspace((current_entry,), mutable=False),
    )
    session.declare("image")
    session.record(stale, _projected(stale))

    with pytest.raises(PendingTemplateReference, match="stale"):
        session.resolve(_artifact_reference(), "/image", unit=_consumer(), context=_context())


def _promotion_plane(channel: str, revision: str, workspace: InMemoryWorkspace) -> ExactPlane:
    snapshot = SnapshotId(f"git-commit:{revision}")
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _promoted_source() -> tuple[ExactPlane, ExactPlane]:
    driver = UNIT_DRIVERS["oci-images"]
    owner = DesiredOwnerReference(apiVersion=CORE_API_VERSION, kind="Stack", name="source", uid="d1-source")
    unit = UnitResource(
        GVK(driver.api_version, driver.kind),
        ResourceMetadata(name="image", uid="d1-source-image", ownerReferences=[owner]),
        driver,
        OciImagesDesiredUnit(
            source=DesiredSource(path=".", revision=REVISION, driverVersion=driver.version, inputHash=INPUT_HASH)
        ),
    )
    units = {
        "image": StackTemplateUnitTemplate(
            apiVersion=driver.api_version,
            kind=driver.kind,
            spec=TemplateObject({"source": {"path": "."}}),
        )
    }
    inline = StackTemplateInlineSpec(parameters=[], unitTemplates=units)
    template = StackResource(
        GVK(CORE_API_VERSION, "StackTemplate"),
        ResourceMetadata(name="source-template", uid="d1-source-template"),
        DesiredStackTemplateSpec(
            parameters=[],
            unitTemplates=units,
            contentDigest=inline.semantic_content_digest(),
            acquisition=StackTemplateAcquisition(
                documentDigest=inline.semantic_content_digest(),
                requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
                resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
            ),
            sourceContext=StackTemplateSourceContext(repository="https://example.test/source.git", revision=REVISION),
        ),
    )
    assert isinstance(template.spec, DesiredStackTemplateSpec)
    structural = StackProjection.build(
        stack_uid="d1-source",
        template_uid="d1-source-template",
        template_content_digest=template.spec.contentDigest,
        context_digest="sha256:" + "d" * 64,
        units={
            "image": StackProjectionUnit(
                apiVersion=driver.api_version,
                kind=driver.kind,
                spec=ProjectionObject({"source": {"path": ".", "revision": REVISION}}),
                dependsOn=[],
            )
        },
    )
    active = StackActiveProjection.build(
        source_projection_digest=structural.identity.projectionDigest,
        projection_context_digest=structural.identity.projectionContextDigest,
        units={
            "image": StackProjectionUnitBinding(
                apiVersion=driver.api_version,
                kind=driver.kind,
                name="image",
                uid="d1-source-image",
                desiredDigest=desired_unit_binding_digest(unit),
                sourceProjectionDigest=structural.identity.projectionDigest,
                projectionContextDigest=structural.identity.projectionContextDigest,
            )
        },
    )
    stack = StackResource(
        GVK(CORE_API_VERSION, "Stack"),
        ResourceMetadata(name="source", uid="d1-source"),
        DesiredStackSpec(
            templateRef=StackTemplateReference(
                name="source-template", uid="d1-source-template", contentDigest=template.spec.contentDigest
            ),
            parameters=JsonObjectValue({}),
            structuralProjection=structural,
            activeProjection=active,
        ),
    )
    unit_raw = _json(CATALOG.serialize_unit(unit, profile="desired"))
    artifact: JsonObject = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": driver.api_version,
            "kind": driver.kind,
            "name": "image",
            "qualifiedName": "source/image",
            "driverVersion": driver.version,
            "sourceRevision": REVISION,
            "inputHashVersion": 1,
            "inputHash": INPUT_HASH,
        },
        "images": {"application": {"uri": "registry.example/promoted@sha256:" + "e" * 64}},
    }
    artifact_raw = _json(artifact)
    artifact_digest = "sha256:" + hashlib.sha256(artifact_raw).hexdigest()
    receipt: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": "image"},
        "spec": {
            "subject": {
                "apiVersion": driver.api_version,
                "kind": driver.kind,
                "name": "image",
                "qualifiedName": "source/image",
            },
            "desired": {
                "unitContentId": entry_content_id(WorkspaceEntry.file("units/source/image.json", unit_raw)).value
            },
        },
        "status": {
            "controller": {},
            "result": {},
            "artifacts": {
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": "artifacts/source/image/containers.json",
                    "digest": artifact_digest,
                    "mediaType": "application/vnd.gitopsctr.container-images.v1+json",
                }
            },
        },
    }
    desired = InMemoryWorkspace(
        (
            _entry(
                "stack-templates/source-template.json", CATALOG.serialize_stack_resource(template, profile="desired")
            ),
            _entry("stacks/source.json", CATALOG.serialize_stack_resource(stack, profile="desired")),
            WorkspaceEntry.file("units/source/image.json", unit_raw),
        ),
        mutable=False,
    )
    observed = InMemoryWorkspace(
        (
            _entry("units/source/image.json", receipt),
            WorkspaceEntry.file("artifacts/source/image/containers.json", artifact_raw),
        ),
        mutable=False,
    )
    return (
        _promotion_plane("desired/staging", REVISION, desired),
        _promotion_plane("observed/staging", "b" * 40, observed),
    )


def _primary_source() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("main.tf", b"terraform {}\n"),), mutable=False)
    snapshot = SnapshotId("git-source:" + "c" * 40)
    retained = _issue_retained_source(
        RetainedSourceHandle("import-target-primary"),
        RetentionStoreId("import-target-store"),
        SourceSnapshotId(SourceId("target-source"), snapshot),
        workspace.content_id,
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "target-primary",
        SourceBindingRole.PRIMARY_AUTHORED,
        "main.tf",
        ContentId("sha256:" + "f" * 64),
    )
    workload = _issue_retained_source_descriptor(
        retained,
        "target-workload",
        SourceBindingRole.WORKLOAD,
        "main.tf",
        ContentId("sha256:" + "8" * 64),
    )
    return RetainedSourcePlane(
        retained,
        ExactPlane(
            HeadObservation.present(ChannelId("source/target"), snapshot, "target-source-incarnation"),
            workspace,
            SnapshotView(snapshot, workspace.content_id, workspace),
        ),
        (descriptor, workload),
    )


def test_real_stack_selected_unit_resolves_authenticated_promoted_artifact_and_persists_evidence() -> None:
    source_desired, source_observed = _promoted_source()
    target_desired = _promotion_plane("desired/dev", "c" * 40, InMemoryWorkspace(mutable=False))
    target_observed = _promotion_plane("observed/dev", "d" * 40, InMemoryWorkspace(mutable=False))
    promotion = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        source_desired,
        source_observed,
        target_desired,
        target_observed,
        ContentId("sha256:" + "9" * 64),
    )
    promotion_encoder = GitPromotionLineageEncoder(
        desired_refs={ChannelId("desired/staging"): "desired/staging", ChannelId("desired/dev"): "desired/dev"},
        observed_refs={
            ChannelId("observed/staging"): "observed/staging",
            ChannelId("observed/dev"): "observed/dev",
        },
        allowed_sources={EnvironmentId("dev"): frozenset((EnvironmentId("staging"),))},
    )
    primary = _primary_source()
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(
            b"kind: Project\n", b"kind: Environment\n", promotion_source=promotion
        ),
        primary_source=primary.descriptors[0],
        named_sources=(primary.descriptors[1],),
        root_identity_issuer=HmacRootIncarnationIssuer("import-test", "import-test-seed"),
    )
    template: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "StackTemplate",
        "metadata": {"name": "target-template"},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "deploy": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {
                        "source": {"path": ".", "inputs": ["main.tf"]},
                        "terraform": {
                            "backend": {},
                            "variables": {"image": _artifact_reference()},
                            "observeOutputs": [],
                            "checks": [],
                        },
                    },
                }
            },
        },
    }
    imported = ArtifactImport(
        unit="image",
        name="containers",
        apiVersion="artifact.gitopsctr.io/v1",
        kind="ContainerImages",
        fromPromotion=PromotionStackReference(stack="source"),
    )
    stack: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Stack",
        "metadata": {"name": "target"},
        "spec": {"template": "target-template", "artifactImports": [imported.to_dict()]},
    }
    source_encoder = GitSourceLineageEncoder({SourceId("target-source"): "https://example.test/target.git"})
    logical = CatalogLogicalUnitProjector(
        CATALOG,
        source_encoder,
        FilesystemUnitProjectionHost(CATALOG),
        source_selector=GitUnitSourceSelector(source_encoder, {"target/deploy": "target-workload"}),
    )
    compiler = CatalogStackProjectionCompiler(
        CATALOG,
        logical,
        promotion_encoder=promotion_encoder,
        source_encoder=source_encoder,
        artifact_import_resolver=CanonicalArtifactImportResolver(CATALOG),
    )

    delta = compiler.project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "1" * 64), template),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "2" * 64), stack),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (primary,),
        target_observed.workspace,
        context,
    )

    deploy = next(item for item in delta.writes if item.key == "units/target/deploy.json")
    desired = CATALOG.parse_unit(deploy.mutable_document(), profile="desired")
    assert isinstance(desired.spec, TerraformDesiredUnit)
    assert desired.spec.terraform is not None and desired.spec.terraform.variables is not None
    assert desired.spec.terraform.variables["image"] == "registry.example/promoted@sha256:" + "e" * 64
    assert desired.spec.resolvedInputs is not None
    assert desired.spec.resolvedInputs.importedArtifacts is not None
    assert desired.spec.resolvedInputs.importedArtifacts["image/containers"].startswith("sha256:")
    assert desired.spec.resolvedInputs.importedArtifactEvidence is not None
    assert "image/containers" in desired.spec.resolvedInputs.importedArtifactEvidence
    target_stack = next(item for item in delta.writes if item.key == "stacks/target.json")
    parsed_stack = CATALOG.parse_stack(target_stack.mutable_document(), profile="desired")
    assert isinstance(parsed_stack.spec, DesiredStackSpec)
    assert parsed_stack.spec.resolvedArtifactImports is not None
    assert parsed_stack.spec.resolvedArtifactImports["image/containers"].targetStackUid == parsed_stack.metadata.uid
