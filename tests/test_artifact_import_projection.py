"""Canonical promoted-artifact evidence tests for the pure Stack compiler."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest
import yaml

from gitopsctr.adapters.git.promotion_lineage import GitPromotionLineageEncoder
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.application.apply_compilers import (
    ArtifactImportRequest,
    ArtifactImportResolution,
    CanonicalArtifactImportResolver,
    CatalogStackProjectionCompiler,
    ProjectionCompilerError,
    PromotionLineage,
    UnitProjection,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ExactPlane,
    FrozenAuthoredDocument,
    HmacRootIncarnationIssuer,
    PromotionSourceDescriptor,
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
    StackSpec,
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
SOURCE_STACK = "source"
SOURCE_UNIT = "unit"
SOURCE_UID = "d1-source"
SOURCE_UNIT_UID = "d1-source-unit"
TARGET_UID = "d1-target"
TEMPLATE_UID = "d1-source-template"
DESIRED_REVISION = "a" * 40
OBSERVED_REVISION = "b" * 40
TARGET_DESIRED_REVISION = "c" * 40
TARGET_OBSERVED_REVISION = "d" * 40


class _NoopUnitProjector:
    def project_unit(self, unit: UnitResource[Any], *, metadata: ResourceMetadata, **_kwargs: object) -> UnitProjection:
        return UnitProjection(
            UnitResource(
                unit.gvk,
                metadata,
                unit.driver,
                TerraformDesiredUnit(
                    source=DesiredSource(path=".", revision=TARGET_DESIRED_REVISION, inputHash="sha256:" + "a" * 64)
                ),
            )
        )


@dataclass(frozen=True)
class _Fixture:
    desired: InMemoryWorkspace
    observed: InMemoryWorkspace
    descriptor: PromotionSourceDescriptor
    target: StackResource
    artifact_import: ArtifactImport
    lineage: PromotionLineage


def _json(document: JsonObject) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _document_bytes(document: JsonObject, extension: str) -> bytes:
    if extension == "json":
        return _json(document)
    return yaml.safe_dump(document, sort_keys=True).encode()


def _plane(channel: str, revision: str, workspace: InMemoryWorkspace) -> ExactPlane:
    snapshot = SnapshotId(f"git-commit:{revision}")
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )


def _template() -> StackResource:
    units = {
        SOURCE_UNIT: StackTemplateUnitTemplate(
            apiVersion="unit.gitopsctr.io/v1",
            kind="OciImages",
            spec=TemplateObject({"source": {"path": "."}}),
        )
    }
    inline = StackTemplateInlineSpec(parameters=[], unitTemplates=units)
    digest = inline.semantic_content_digest()
    spec = DesiredStackTemplateSpec(
        parameters=[],
        unitTemplates=units,
        contentDigest=digest,
        acquisition=StackTemplateAcquisition(
            documentDigest=digest,
            requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        ),
        sourceContext=StackTemplateSourceContext(
            repository="https://example.test/source.git", revision=DESIRED_REVISION
        ),
    )
    return StackResource(
        GVK(CORE_API_VERSION, "StackTemplate"), ResourceMetadata(name="source-template", uid=TEMPLATE_UID), spec
    )


def _source_stack(
    template: StackResource,
    unit: UnitResource[OciImagesDesiredUnit],
    *,
    deleted: bool = False,
    active: bool = True,
    include_active_binding: bool = True,
    active_binding_mutation: Callable[[StackProjectionUnitBinding], StackProjectionUnitBinding] | None = None,
) -> StackResource:
    assert isinstance(template.spec, DesiredStackTemplateSpec)
    projection = StackProjection.build(
        stack_uid=SOURCE_UID,
        template_uid=TEMPLATE_UID,
        template_content_digest=template.spec.contentDigest,
        context_digest="sha256:" + "e" * 64,
        units={
            SOURCE_UNIT: StackProjectionUnit(
                apiVersion=unit.gvk.api_version,
                kind=unit.gvk.kind,
                spec=ProjectionObject({"source": {"path": ".", "revision": DESIRED_REVISION}}),
                dependsOn=[],
            )
        },
    )
    binding = StackProjectionUnitBinding(
        apiVersion=unit.gvk.api_version,
        kind=unit.gvk.kind,
        name=unit.name,
        uid=unit.metadata.uid or "",
        desiredDigest=desired_unit_binding_digest(unit),
        sourceProjectionDigest=projection.identity.projectionDigest,
        projectionContextDigest=projection.identity.projectionContextDigest,
    )
    if active_binding_mutation is not None:
        binding = active_binding_mutation(binding)
    active_projection = None
    if active:
        active_projection = StackActiveProjection.build(
            source_projection_digest=projection.identity.projectionDigest,
            projection_context_digest=projection.identity.projectionContextDigest,
            units={SOURCE_UNIT: binding} if include_active_binding else {},
        )
    metadata = ResourceMetadata(name=SOURCE_STACK, uid=SOURCE_UID)
    if deleted:
        # A source document does not need a full deletion lifecycle for this
        # proof: its desired metadata is parsed by the catalog before the
        # resolver rejects its deletion marker.
        from gitopsctr.contracts import DeletionMetadata

        metadata = replace(metadata, deletion=DeletionMetadata(generation=1, resourceDigest="sha256:" + "0" * 64))
    return StackResource(
        GVK(CORE_API_VERSION, "Stack"),
        metadata,
        DesiredStackSpec(
            templateRef=StackTemplateReference(
                name=template.name, uid=TEMPLATE_UID, contentDigest=template.spec.contentDigest
            ),
            parameters=JsonObjectValue({}),
            structuralProjection=projection,
            activeProjection=active_projection,
        ),
    )


def _source_unit(*, owner: DesiredOwnerReference | None = None) -> UnitResource[OciImagesDesiredUnit]:
    owner = owner or DesiredOwnerReference(apiVersion=CORE_API_VERSION, kind="Stack", name=SOURCE_STACK, uid=SOURCE_UID)
    return UnitResource(
        GVK("unit.gitopsctr.io/v1", "OciImages"),
        ResourceMetadata(name=SOURCE_UNIT, uid=SOURCE_UNIT_UID, ownerReferences=[owner]),
        UNIT_DRIVERS["oci-images"],
        OciImagesDesiredUnit(
            source=DesiredSource(
                path=".",
                revision=DESIRED_REVISION,
                driverVersion=UNIT_DRIVERS["oci-images"].version,
                inputHash="sha256:" + "f" * 64,
            )
        ),
    )


def _import() -> ArtifactImport:
    return ArtifactImport(
        unit=SOURCE_UNIT,
        name="containers",
        apiVersion="artifact.gitopsctr.io/v1",
        kind="ContainerImages",
        fromPromotion=PromotionStackReference(stack=SOURCE_STACK),
    )


def _lineage(descriptor: PromotionSourceDescriptor) -> PromotionLineage:
    # This typed value is intentionally constructed by the same Git adapter
    # used by the production compiler rather than a fake evidence object.
    return _encoder().encode(descriptor)


def _encoder() -> GitPromotionLineageEncoder:
    return GitPromotionLineageEncoder(
        desired_refs={
            ChannelId("desired/staging"): "desired/staging",
            ChannelId("desired/dev"): "desired/dev",
        },
        observed_refs={
            ChannelId("observed/staging"): "observed/staging",
            ChannelId("observed/dev"): "observed/dev",
        },
        allowed_sources={EnvironmentId("dev"): frozenset((EnvironmentId("staging"),))},
    )


def _primary_source() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace((WorkspaceEntry.file("source.json", b"{}"),), mutable=False)
    snapshot = SnapshotId("git-source:" + TARGET_DESIRED_REVISION)
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
        "source.json",
        ContentId("sha256:" + "4" * 64),
    )
    plane = ExactPlane(
        HeadObservation.present(ChannelId("source"), snapshot, "source-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )
    return RetainedSourcePlane(retained, plane, (descriptor,))


def _fixture(
    *,
    source_stack_deleted: bool = False,
    source_stack_active: bool = True,
    template_deleted: bool = False,
    owner: DesiredOwnerReference | None = None,
    include_active_binding: bool = True,
    active_binding_mutation: Callable[[StackProjectionUnitBinding], StackProjectionUnitBinding] | None = None,
    receipt_mutation: Callable[[dict[str, Any]], None] | None = None,
    artifact_mutation: Callable[[dict[str, Any]], object] | None = None,
    document_format: str = "json",
    ambiguity: str | None = None,
    include_unit: bool = True,
    include_receipt: bool = True,
    include_artifact: bool = True,
    bait: bool = False,
) -> _Fixture:
    if document_format not in {"json", "yaml", "yml"}:
        raise ValueError(f"unsupported test document format {document_format!r}")
    extension = f".{document_format}"
    template = _template()
    if template_deleted:
        from gitopsctr.contracts import DeletionMetadata

        template = replace(
            template,
            metadata=replace(
                template.metadata, deletion=DeletionMetadata(generation=1, resourceDigest="sha256:" + "0" * 64)
            ),
        )
    unit = _source_unit(owner=owner)
    stack = _source_stack(
        template,
        unit,
        deleted=source_stack_deleted,
        active=source_stack_active,
        include_active_binding=include_active_binding,
        active_binding_mutation=active_binding_mutation,
    )
    stack_document = CATALOG.serialize_stack_resource(stack, profile="desired")
    template_document = CATALOG.serialize_stack_resource(template, profile="desired")
    unit_document = CATALOG.serialize_unit(unit, profile="desired")
    unit_key = f"units/{SOURCE_STACK}/{SOURCE_UNIT}{extension}"
    unit_bytes = _document_bytes(unit_document, document_format)
    artifact_document: dict[str, Any] = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": SOURCE_UNIT,
            "qualifiedName": f"{SOURCE_STACK}/{SOURCE_UNIT}",
            "driverVersion": UNIT_DRIVERS["oci-images"].version,
            "sourceRevision": DESIRED_REVISION,
            "inputHashVersion": 1,
            "inputHash": "sha256:" + "f" * 64,
        },
        "images": {"application": {"uri": "registry.example/app@sha256:" + "1" * 64}},
    }
    if artifact_mutation is not None:
        artifact_mutation(artifact_document)
    artifact_key = f"artifacts/{SOURCE_STACK}/{SOURCE_UNIT}/containers{extension}"
    artifact_bytes = _document_bytes(cast(JsonObject, artifact_document), document_format)
    artifact_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    receipt: dict[str, Any] = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": SOURCE_UNIT},
        "spec": {
            "subject": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "OciImages",
                "name": SOURCE_UNIT,
                "qualifiedName": f"{SOURCE_STACK}/{SOURCE_UNIT}",
            },
            "desired": {"unitContentId": entry_content_id(WorkspaceEntry.file(unit_key, unit_bytes)).value},
        },
        "status": {
            "controller": {},
            "result": {},
            "artifacts": {
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": artifact_key,
                    "digest": artifact_digest,
                    "mediaType": (
                        "application/vnd.gitopsctr.container-images.v1+json"
                        if document_format == "json"
                        else "application/vnd.gitopsctr.container-images.v1+yaml"
                    ),
                }
            },
        },
    }
    if receipt_mutation is not None:
        receipt_mutation(receipt)
    stack_key = f"stacks/source{extension}"
    template_key = f"stack-templates/source-template{extension}"
    receipt_key = f"units/{SOURCE_STACK}/{SOURCE_UNIT}{extension}"
    desired_entries = [
        WorkspaceEntry.file(stack_key, _document_bytes(stack_document, document_format)),
        WorkspaceEntry.file(template_key, _document_bytes(template_document, document_format)),
    ]
    if include_unit:
        desired_entries.append(WorkspaceEntry.file(unit_key, unit_bytes))
    observed_entries: list[WorkspaceEntry] = []
    if include_receipt:
        observed_entries.append(
            WorkspaceEntry.file(receipt_key, _document_bytes(cast(JsonObject, receipt), document_format))
        )
    if include_artifact:
        observed_entries.append(WorkspaceEntry.file(artifact_key, artifact_bytes))
    if ambiguity is not None:
        alternate_format = "yaml" if document_format == "json" else "json"
        alternate_extension = f".{alternate_format}"
        ambiguous = {
            "stack": (
                desired_entries,
                f"stacks/source{alternate_extension}",
                stack_document,
            ),
            "template": (
                desired_entries,
                f"stack-templates/source-template{alternate_extension}",
                template_document,
            ),
            "unit": (
                desired_entries,
                f"units/{SOURCE_STACK}/{SOURCE_UNIT}{alternate_extension}",
                unit_document,
            ),
            "receipt": (
                observed_entries,
                f"units/{SOURCE_STACK}/{SOURCE_UNIT}{alternate_extension}",
                cast(JsonObject, receipt),
            ),
            "artifact": (
                observed_entries,
                f"artifacts/{SOURCE_STACK}/{SOURCE_UNIT}/containers{alternate_extension}",
                cast(JsonObject, artifact_document),
            ),
        }.get(ambiguity)
        if ambiguous is None:
            raise ValueError(f"unsupported ambiguity fixture {ambiguity!r}")
        entries, key, document = ambiguous
        entries.append(WorkspaceEntry.file(key, _document_bytes(document, alternate_format)))
    if bait:
        desired_entries.extend(
            (
                WorkspaceEntry.file("archive/stacks/source.yaml", b"kind: Stack\nmetadata:\n  name: source\n"),
                WorkspaceEntry.file("stack-templates/archive/source-template.json", b"{}"),
                WorkspaceEntry.file("archive/units/source/unit.json", b"{}"),
            )
        )
        observed_entries.extend(
            (
                WorkspaceEntry.file("archive/artifacts/source/unit/containers.yaml", b"kind: Wrong\n"),
                WorkspaceEntry.file("archive/units/source/unit.json", b"{}"),
            )
        )
    desired = InMemoryWorkspace(tuple(desired_entries), mutable=False)
    observed = InMemoryWorkspace(tuple(observed_entries), mutable=False)
    target_desired = InMemoryWorkspace(mutable=False)
    target_observed = InMemoryWorkspace(mutable=False)
    descriptor = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        _plane("desired/staging", DESIRED_REVISION, desired),
        _plane("observed/staging", OBSERVED_REVISION, observed),
        _plane("desired/dev", TARGET_DESIRED_REVISION, target_desired),
        _plane("observed/dev", TARGET_OBSERVED_REVISION, target_observed),
        ContentId("sha256:" + "1" * 64),
    )
    target = StackResource(
        GVK(CORE_API_VERSION, "Stack"),
        ResourceMetadata(name="target", uid=TARGET_UID),
        StackSpec(template="target-template"),
    )
    return _Fixture(desired, observed, descriptor, target, _import(), _lineage(descriptor))


def _request(fixture: _Fixture) -> ArtifactImportRequest:
    return ArtifactImportRequest(fixture.artifact_import, fixture.target, fixture.descriptor, fixture.lineage)


def _resolve(fixture: _Fixture) -> ArtifactImportResolution:
    return CanonicalArtifactImportResolver(CATALOG).resolve(_request(fixture))


@pytest.mark.parametrize("document_format", ("json", "yaml", "yml"))
def test_canonical_artifact_import_accepts_one_document_format_and_ignores_noncanonical_bait(
    document_format: str,
) -> None:
    fixture = _fixture(bait=True, document_format=document_format)
    resolution = _resolve(fixture)

    evidence = resolution.resolved
    assert evidence.sourceStack == SOURCE_STACK
    assert evidence.sourceStackUid == SOURCE_UID
    assert evidence.sourceUnitUid == SOURCE_UNIT_UID
    assert evidence.sourceDesiredRevision == DESIRED_REVISION
    assert evidence.sourceObservedRevision == OBSERVED_REVISION
    assert evidence.artifactDocument == {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": SOURCE_UNIT,
            "qualifiedName": f"{SOURCE_STACK}/{SOURCE_UNIT}",
            "driverVersion": UNIT_DRIVERS["oci-images"].version,
            "sourceRevision": DESIRED_REVISION,
            "inputHashVersion": 1,
            "inputHash": "sha256:" + "f" * 64,
        },
        "images": {"application": {"uri": "registry.example/app@sha256:" + "1" * 64}},
    }


@pytest.mark.parametrize("ambiguous", ("stack", "template", "unit", "receipt", "artifact"))
def test_canonical_artifact_import_rejects_duplicate_document_formats(ambiguous: str) -> None:
    with pytest.raises(ProjectionCompilerError, match="ambiguous document formats"):
        _resolve(_fixture(ambiguity=ambiguous))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (lambda receipt: receipt["spec"]["desired"].__setitem__("unitContentId", "sha256:" + "0" * 64), "stale"),
        (
            lambda receipt: receipt["status"]["artifacts"]["containers"].__setitem__("path", "artifacts/other.json"),
            "descriptor",
        ),
        (
            lambda receipt: receipt["status"]["artifacts"]["containers"].__setitem__("digest", "sha256:" + "0" * 64),
            "bytes",
        ),
        (
            lambda receipt: receipt["status"]["artifacts"]["containers"].__setitem__(
                "mediaType", "application/octet-stream"
            ),
            "media type",
        ),
    ),
)
def test_canonical_artifact_import_rejects_stale_or_wrong_receipt_descriptors(mutation, expected: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProjectionCompilerError, match=expected):
        _resolve(_fixture(receipt_mutation=mutation))


def test_canonical_artifact_import_rejects_a_receipt_descriptor_with_the_wrong_gvk() -> None:
    with pytest.raises(ProjectionCompilerError, match="ContainerImages"):
        _resolve(
            _fixture(
                receipt_mutation=lambda receipt: receipt["status"]["artifacts"]["containers"].__setitem__(
                    "kind", "Other"
                )
            )
        )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (lambda artifact: artifact.pop("producer"), "invalid promoted artifact document"),
        (lambda artifact: artifact.__setitem__("producer", {}), "invalid promoted artifact document"),
        (lambda artifact: artifact.pop("images"), "invalid promoted artifact document"),
        (lambda artifact: artifact.__setitem__("kind", "Other"), "invalid promoted artifact document"),
    ),
)
def test_canonical_artifact_import_validates_the_registered_artifact_contract(
    mutation: Callable[[dict[str, Any]], object], expected: str
) -> None:
    with pytest.raises(ProjectionCompilerError, match=expected):
        _resolve(_fixture(artifact_mutation=mutation))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("apiVersion", "other.io/v1"),
        ("kind", "Other"),
        ("name", "other"),
        ("qualifiedName", "other/unit"),
        ("driverVersion", 999),
        ("sourceRevision", "0" * 40),
        ("inputHashVersion", 2),
        ("inputHash", "sha256:" + "0" * 64),
    ),
)
def test_canonical_artifact_import_rejects_stale_or_foreign_producer_identity(field: str, value: object) -> None:
    def mutate(artifact: dict[str, Any]) -> None:
        cast(dict[str, Any], artifact["producer"])[field] = value

    with pytest.raises(ProjectionCompilerError, match="producer identity|invalid promoted artifact document"):
        _resolve(_fixture(artifact_mutation=mutate))


@pytest.mark.parametrize(
    "owner",
    (
        DesiredOwnerReference(apiVersion="wrong.io/v1", kind="Stack", name=SOURCE_STACK, uid=SOURCE_UID),
        DesiredOwnerReference(apiVersion=CORE_API_VERSION, kind="Other", name=SOURCE_STACK, uid=SOURCE_UID),
        DesiredOwnerReference(apiVersion=CORE_API_VERSION, kind="Stack", name="other", uid=SOURCE_UID),
        DesiredOwnerReference(apiVersion=CORE_API_VERSION, kind="Stack", name=SOURCE_STACK, uid="d1-other"),
    ),
)
def test_canonical_artifact_import_requires_the_full_source_stack_owner_identity(owner: DesiredOwnerReference) -> None:
    with pytest.raises(ProjectionCompilerError, match="ownership"):
        _resolve(_fixture(owner=owner))


def test_canonical_artifact_import_rejects_an_orphaned_unit_outside_the_active_projection() -> None:
    with pytest.raises(ProjectionCompilerError, match="not in the Stack active projection"):
        _resolve(_fixture(include_active_binding=False))


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("apiVersion", "other.io/v1", "exact Stack active projection binding"),
        ("kind", "Other", "exact Stack active projection binding"),
        ("name", "other", "exact Stack active projection binding"),
        ("uid", "d1-other", "exact Stack active projection binding"),
        ("desiredDigest", "sha256:" + "0" * 64, "exact Stack active projection binding"),
        ("sourceProjectionDigest", "sha256:" + "1" * 64, "exact Stack active projection binding"),
        ("projectionContextDigest", "sha256:" + "2" * 64, "exact Stack active projection binding"),
    ),
)
def test_canonical_artifact_import_requires_an_exact_active_unit_binding(
    field: str, value: object, expected: str
) -> None:
    def mutate(binding: StackProjectionUnitBinding) -> StackProjectionUnitBinding:
        changes = {field: value}
        if field == "projectionContextDigest":
            # DesiredStackSpec itself rejects a context mismatch against the
            # current source projection. A stale source digest keeps the
            # document model-valid so the resolver must still verify both.
            changes["sourceProjectionDigest"] = "sha256:" + "3" * 64
        return replace(binding, **changes)

    with pytest.raises(ProjectionCompilerError, match=expected):
        _resolve(_fixture(active_binding_mutation=mutate))


@pytest.mark.parametrize(
    ("unavailable", "expected"),
    (
        ("stack-deleted", "deleting"),
        ("stack-inactive", "inactive"),
        ("template-deleted", "deleting"),
        ("unit", "missing canonical"),
        ("receipt", "missing canonical"),
        ("artifact", "missing canonical"),
    ),
)
def test_canonical_artifact_import_fails_closed_for_unavailable_source_state(unavailable: str, expected: str) -> None:
    fixture = {
        "stack-deleted": lambda: _fixture(source_stack_deleted=True),
        "stack-inactive": lambda: _fixture(source_stack_active=False),
        "template-deleted": lambda: _fixture(template_deleted=True),
        "unit": lambda: _fixture(include_unit=False),
        "receipt": lambda: _fixture(include_receipt=False),
        "artifact": lambda: _fixture(include_artifact=False),
    }[unavailable]()
    with pytest.raises(ProjectionCompilerError, match=expected):
        _resolve(fixture)


def test_stack_compiler_persists_canonical_artifact_import_evidence() -> None:
    fixture = _fixture()
    primary = _primary_source()
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(
            b"apiVersion: gitopsctr.io/v1\nkind: Project\n",
            b"apiVersion: gitopsctr.io/v1\nkind: Environment\n",
            promotion_source=fixture.descriptor,
        ),
        primary_source=primary.descriptors[0],
        root_identity_issuer=HmacRootIncarnationIssuer("test", "artifact-import-tests"),
    )
    target_template: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "StackTemplate",
        "metadata": {"name": "target-template"},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "render": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": "."}},
                }
            },
        },
    }
    target_stack: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Stack",
        "metadata": {"name": "target"},
        "spec": {
            "template": "target-template",
            "artifactImports": [fixture.artifact_import.to_dict()],
        },
    }
    delta = CatalogStackProjectionCompiler(
        CATALOG,
        _NoopUnitProjector(),
        promotion_encoder=_encoder(),
        source_encoder=GitSourceLineageEncoder({SourceId("primary-source"): "."}),
        artifact_import_resolver=CanonicalArtifactImportResolver(CATALOG),
    ).project(
        (
            FrozenAuthoredDocument.from_change("template", ContentId("sha256:" + "2" * 64), target_template),
            FrozenAuthoredDocument.from_change("stack", ContentId("sha256:" + "3" * 64), target_stack),
        ),
        {},
        InMemoryWorkspace(mutable=False),
        (primary,),
        InMemoryWorkspace(mutable=False),
        context,
    )

    stack_entry = next(entry for entry in delta.writes if entry.key == "stacks/target.json")
    parsed = CATALOG.parse_stack(stack_entry.mutable_document(), profile="desired")
    assert isinstance(parsed.spec, DesiredStackSpec)
    assert parsed.spec.resolvedArtifactImports is not None
    evidence = parsed.spec.resolvedArtifactImports["unit/containers"]
    assert evidence.sourceStackUid == SOURCE_UID
    assert evidence.receiptUnitContentId == _resolve(fixture).resolved.receiptUnitContentId
    assert evidence.targetStackUid == parsed.metadata.uid


def test_duplicate_artifact_imports_are_rejected_before_aggregation() -> None:
    imported = _import()
    with pytest.raises(ValueError, match="unique"):
        StackSpec(template="target-template", artifactImports=[imported, imported])


def test_artifact_import_boundary_rejects_tampered_request_resolution_and_promotion_evidence() -> None:
    fixture = _fixture()
    request = _request(fixture)
    object.__setattr__(request, "artifact_import", replace(fixture.artifact_import, name="other"))
    with pytest.raises(TypeError, match="modified"):
        CanonicalArtifactImportResolver(CATALOG).resolve(request)

    fixture = _fixture()
    resolution = _resolve(fixture)
    artifact = cast(dict[str, Any], resolution.resolved.artifactDocument)
    images = cast(dict[str, Any], artifact["images"])
    cast(dict[str, Any], images["application"])["uri"] = "registry.example/other:latest"
    with pytest.raises(TypeError, match="modified"):
        resolution._validate()

    fixture = _fixture()
    object.__setattr__(fixture.descriptor, "source_environment", EnvironmentId("other"))
    with pytest.raises(TypeError, match="modified"):
        _resolve(fixture)

    fixture = _fixture()
    request = _request(fixture)
    object.__setattr__(request, "lineage", replace(request.lineage, source_desired_ref="other"))
    with pytest.raises(TypeError, match="modified"):
        CanonicalArtifactImportResolver(CATALOG).resolve(request)
