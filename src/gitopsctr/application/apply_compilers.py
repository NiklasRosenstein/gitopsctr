"""Controller-free production compilers for desired apply projections.

The compilers in this module are deliberately parameterised by the one
effect-free Unit projection capability.  Catalog parsing, Stack expansion,
identity assignment, retained-source lookup, and workspace deltas stay here;
drivers which need to resolve templates or emit materialisation payloads do so
through the narrow injected ``LogicalUnitProjector`` contract.  No compiler
opens a checkout, resolves a Git selector, creates a temporary directory, or
uses a clock/UUID.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionError,
    CandidateTransformation,
    FrozenAuthoredDocument,
    IssuedRootIdentity,
    PayloadPrefixReplacement,
    ProjectedDocument,
    PromotionSourceDescriptor,
    RetainedSourceDescriptor,
    RetainedSourcePlane,
    RootIdentityRequest,
    SourceBindingRole,
)
from gitopsctr.application.model import ContentId, EnvironmentId
from gitopsctr.application.workspace import ImmutableWorkspace, WorkspaceEntry, WorkspaceEntryKind, entry_content_id
from gitopsctr.artifacts import require_artifact_api
from gitopsctr.contracts import (
    ArtifactDescriptor,
    ArtifactImport,
    DesiredOwnerReference,
    DesiredSource,
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    ProjectionObject,
    ReceiptDocument,
    ResolvedArtifactImport,
    StackActiveProjection,
    StackProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackSpec,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateGitSpec,
    StackTemplateInlineSpec,
    StackTemplatePromotionSpec,
    StackTemplateReference,
    StackTemplateRequestedFromGit,
    StackTemplateRequestedFromInput,
    StackTemplateRequestedFromPromotion,
    StackTemplateResolvedFromGit,
    StackTemplateResolvedFromGitSource,
    StackTemplateResolvedFromInput,
    StackTemplateResolvedFromPromotion,
    StackTemplateResolvedFromPromotionSource,
    StackTemplateResource,
    StackTemplateSourceContext,
    scope_stack_template_resources,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError, ReferenceUnavailable
from gitopsctr.operational import (
    DESIRED_TRANSITION_BLOCKS_PATH,
    load_workspace_transition_blocks,
    validate_workspace_unit_materialization,
)
from gitopsctr.resolution import FingerprintedValue, ResolutionContext, TemplateResolution, resolve_template
from gitopsctr.resource_api import ApiError, JsonObject
from gitopsctr.resources import (
    CORE_API_VERSION,
    DesiredGraphResource,
    ResourceCatalog,
    ResourceMetadata,
    StackResource,
    UnitResource,
    desired_unit_binding_digest,
    validate_desired_resource_graph,
)
from gitopsctr.templates import TemplateValue, dump_template_value

_PARTITION_LABEL = "gitopsctr.io/partition"
_CONTEXT_PREFIX = ".gitopsctr/projection-contexts"


class ProjectionCompilerError(ApplyProjectionError):
    """A catalog-valid input cannot be compiled into a closed candidate."""


class PendingTemplateReference(ReferenceUnavailable):
    """A reference names a producer selected in this operation but not ready."""


class PendingObservedEvidence(PendingTemplateReference):
    """A projected producer needs a future observation before its consumer can exist."""


@dataclass(frozen=True, slots=True)
class _StackCompilation:
    projected: ProjectedDocument
    units: tuple[tuple[UnitResource[Any], UnitProjection], ...]
    active_identities: frozenset[tuple[str, str, str]]
    blocks: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CatalogApplyDocumentValidator:
    """Production catalog/driver validator for pure apply candidates."""

    catalog: ResourceCatalog

    def validate_authored(self, document: JsonObject) -> None:
        try:
            self._parse(document, "authored")
        except ProjectionCompilerError:
            metadata = document.get("metadata")
            if not isinstance(metadata, Mapping) or "uid" not in metadata:
                raise
            # A canonical desired resource may be reapplied. Its
            # controller-owned metadata/projection is parsed strictly but
            # replaced by the current root incarnation and a fresh projection.
            self._parse(document, "desired")

    def validate_desired(self, document: JsonObject) -> None:
        self._parse(document, "desired")

    def validate_graph(self, documents: Mapping[tuple[str, str, str], ProjectedDocument]) -> None:
        graph: dict[tuple[str, str, str], DesiredGraphResource] = {}
        for identity, projected in documents.items():
            parsed = self._parse(projected.mutable_document(), "desired")
            graph[identity] = parsed
        try:
            validate_desired_resource_graph(graph)
        except (ApiError, TypeError, ValueError) as exc:
            raise ProjectionCompilerError(str(exc)) from exc

    def validate_workspace(self, workspace: ImmutableWorkspace) -> None:
        if workspace.is_mutable:
            raise ProjectionCompilerError("candidate validation requires an immutable workspace view")
        documents: dict[tuple[str, str, str], ProjectedDocument] = {}
        for entry in workspace.list_entries():
            if entry.kind is WorkspaceEntryKind.SYMLINK:
                raise ProjectionCompilerError(f"desired workspace cannot contain symbolic links: {entry.key!r}")
            if entry.key.split("/", 1)[0] in {"units", "stacks", "stack-templates"} and not _canonical_resource_key(
                entry.key
            ):
                raise ProjectionCompilerError(f"desired workspace has a non-canonical resource key: {entry.key!r}")
            if not _canonical_resource_key(entry.key):
                continue
            document = _decode_source_document(workspace.read(entry.key), entry.key)
            projected = ProjectedDocument(entry.key, document)
            if projected.identity in documents:
                raise ProjectionCompilerError(f"desired workspace repeats resource {projected.identity!r}")
            documents[projected.identity] = projected
        self.validate_graph(documents)
        for projected in documents.values():
            parsed = self._parse(projected.mutable_document(), "desired")
            if isinstance(parsed, UnitResource):
                try:
                    validate_workspace_unit_materialization(workspace, projected.identity[2], parsed)
                except (OperationError, TypeError, ValueError) as exc:
                    raise ProjectionCompilerError(str(exc)) from exc

    def _parse(self, document: JsonObject, profile: str) -> StackResource | UnitResource[Any]:
        kind = document.get("kind")
        if kind == "Stack":
            value = _catalog(self.catalog.parse_stack, document, profile)
        elif kind == "StackTemplate":
            value = _catalog(self.catalog.parse_stack_template, document, profile)
        else:
            value = _catalog(self.catalog.parse_unit, document, profile)
        assert isinstance(value, (StackResource, UnitResource))
        return value


@dataclass(frozen=True, slots=True)
class UnitProjection:
    """Desired typed Unit and payload delta returned by a driver projection port."""

    unit: UnitResource[Any]
    payload_writes: tuple[WorkspaceEntry, ...] = ()
    payload_deletes: tuple[str, ...] = ()
    payload_prefixes: tuple[str, ...] = ()
    payload_replacements: tuple[PayloadPrefixReplacement, ...] = ()


class LogicalUnitProjector(Protocol):
    """Resolve one catalog-parsed Unit without storage or publication access.

    Implementations receive every exact retained workspace they may consult and
    must return a desired typed Unit whose metadata is exactly ``metadata``.
    Payload files are explicit logical workspace entries; implementations never
    receive a path or a mutable candidate workspace.
    """

    def project_unit(
        self,
        unit: UnitResource[Any],
        *,
        metadata: ResourceMetadata,
        previous: ProjectedDocument | None,
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
        session: TemplateResolutionSession | None = None,
    ) -> UnitProjection: ...


@dataclass(frozen=True, slots=True)
class UnitProjectionRequest:
    """Closed application input for effectful driver materialization hosts."""

    unit: UnitResource[Any]
    metadata: ResourceMetadata
    previous: ProjectedDocument | None
    current_workspace: ImmutableWorkspace
    source: DesiredSource | None
    selected_source: RetainedSourcePlane | None
    qualified_name: str
    environment_id: EnvironmentId
    resolve_template: Callable[[object, str], TemplateResolution]


class UnitProjectionHost(Protocol):
    """Adapter boundary for driver resolution/materialization into logical entries."""

    def project(self, request: UnitProjectionRequest) -> UnitProjection: ...


@dataclass(frozen=True, slots=True)
class ArtifactImportRequest:
    """Closed promotion evidence required to resolve one Stack artifact import."""

    artifact_import: ArtifactImport
    target_stack: StackResource
    promotion_source: PromotionSourceDescriptor
    lineage: PromotionLineage

    def __post_init__(self) -> None:
        _ARTIFACT_IMPORT_REQUEST_BINDINGS[id(self)] = _artifact_import_request_binding(self)
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.artifact_import, ArtifactImport):
            raise TypeError("artifact import request requires an ArtifactImport")
        if not isinstance(self.target_stack, StackResource) or self.target_stack.gvk.kind != "Stack":
            raise TypeError("artifact import request target must be a StackResource")
        if not isinstance(self.target_stack.metadata.uid, str) or not self.target_stack.metadata.uid:
            raise ProjectionCompilerError("artifact import request target Stack requires an issued UID")
        if not isinstance(self.promotion_source, PromotionSourceDescriptor):
            raise TypeError("artifact import request requires an issued PromotionSourceDescriptor")
        self.promotion_source._validate()
        _validate_promotion_lineage(self.lineage, self.promotion_source)
        if _ARTIFACT_IMPORT_REQUEST_BINDINGS.get(id(self)) != _artifact_import_request_binding(self):
            raise TypeError("artifact import request was modified after construction")


def _artifact_import_request_binding(request: ArtifactImportRequest) -> tuple[object, ...]:
    artifact = request.artifact_import
    target = request.target_stack
    lineage = request.lineage
    return (
        artifact.unit,
        artifact.name,
        artifact.apiVersion,
        artifact.kind,
        artifact.fromPromotion.stack,
        target.gvk.api_version,
        target.gvk.kind,
        target.metadata.name,
        target.metadata.uid,
        id(request.promotion_source),
        request.promotion_source,
        lineage.source_environment,
        lineage.source_desired_ref,
        lineage.source_desired_revision,
        lineage.source_observed_ref,
        lineage.source_observed_revision,
        lineage.target_desired_ref,
        lineage.target_desired_revision,
        lineage.target_observed_ref,
        lineage.target_observed_revision,
        lineage.lineage_evidence,
    )


_ARTIFACT_IMPORT_REQUEST_BINDINGS: dict[int, tuple[object, ...]] = {}
_ARTIFACT_IMPORT_RESOLUTION_BINDINGS: dict[int, tuple[object, ...]] = {}


@dataclass(frozen=True, slots=True)
class ArtifactImportResolution:
    """Resolver output bound to one closed request and its promotion lineage."""

    request: ArtifactImportRequest
    resolved: ResolvedArtifactImport

    def __post_init__(self) -> None:
        _ARTIFACT_IMPORT_RESOLUTION_BINDINGS[id(self)] = _artifact_import_resolution_binding(self)
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.request, ArtifactImportRequest):
            raise TypeError("artifact import resolution requires an ArtifactImportRequest")
        self.request._validate()
        if not isinstance(self.resolved, ResolvedArtifactImport):
            raise TypeError("artifact import resolution requires a ResolvedArtifactImport")
        imported = self.request.artifact_import
        target = self.request.target_stack
        if (
            self.resolved.sourceStack != imported.fromPromotion.stack
            or self.resolved.artifactName != imported.name
            or self.resolved.apiVersion != imported.apiVersion
            or self.resolved.kind != imported.kind
            or self.resolved.targetStackUid != target.metadata.uid
            or self.resolved.sourceDesiredRevision != self.request.lineage.source_desired_revision
            or self.resolved.sourceObservedRevision != self.request.lineage.source_observed_revision
        ):
            raise ProjectionCompilerError("artifact resolver output does not bind its exact request and lineage")
        if _ARTIFACT_IMPORT_RESOLUTION_BINDINGS.get(id(self)) != _artifact_import_resolution_binding(self):
            raise TypeError("artifact import resolution was modified after construction")


def _artifact_import_resolution_binding(resolution: ArtifactImportResolution) -> tuple[object, ...]:
    resolved = resolution.resolved
    return (
        _artifact_import_request_binding(resolution.request),
        resolved.sourceStack,
        resolved.sourceStackUid,
        resolved.sourceUnit,
        resolved.sourceUnitUid,
        resolved.sourceDesiredRevision,
        resolved.sourceObservedRevision,
        resolved.receiptUnitContentId,
        resolved.artifactName,
        resolved.apiVersion,
        resolved.kind,
        resolved.artifactDigest,
        resolved.targetStackUid,
        _canonical(_plain(resolved.artifactDocument)),
    )


class ArtifactImportResolver(Protocol):
    """Resolve one authenticated promoted artifact into durable Stack evidence."""

    def resolve(self, request: ArtifactImportRequest) -> ArtifactImportResolution: ...


@dataclass(frozen=True, slots=True)
class _PromotedArtifactReceipt:
    """The receipt fields that authenticate one imported artifact.

    This deliberately excludes controller result state: artifact imports bind
    only the source Unit identity/content and its persisted artifact descriptor.
    """

    unit_content_id: str
    descriptor: ArtifactDescriptor


def _parse_promoted_artifact_receipt(
    document: JsonObject,
    *,
    unit: UnitResource[Any],
    qualified_name: str,
    artifact: ArtifactImport,
) -> _PromotedArtifactReceipt:
    """Parse closed receipt evidence without leaking document-layer errors."""

    try:
        # Artifact imports authenticate the persisted receipt envelope and its
        # descriptor, not a controller-owned driver result contract.
        receipt = ReceiptDocument.from_dict(cast(Mapping[str, Any], _plain(document)))
    except (OperationError, KeyError, TypeError, ValueError) as exc:
        raise ProjectionCompilerError(f"invalid promoted artifact receipt: {exc}") from exc
    if (
        receipt.metadata.name != unit.name
        or receipt.spec.subject.apiVersion != unit.gvk.api_version
        or receipt.spec.subject.kind != unit.gvk.kind
        or receipt.spec.subject.name != unit.name
        or receipt.spec.subject.qualifiedName != qualified_name
    ):
        raise ProjectionCompilerError("promoted artifact receipt targets a foreign Unit")
    descriptor_document = (receipt.status.artifacts or {}).get(artifact.name)
    try:
        descriptor = (
            ArtifactDescriptor.from_dict(cast(dict[str, object], descriptor_document))
            if isinstance(descriptor_document, dict)
            else None
        )
    except (OperationError, KeyError, TypeError, ValueError) as exc:
        raise ProjectionCompilerError(f"invalid promoted artifact receipt descriptor: {exc}") from exc
    if descriptor is None or descriptor.apiVersion != artifact.apiVersion or descriptor.kind != artifact.kind:
        raise ProjectionCompilerError(
            f"promoted artifact receipt descriptor does not identify requested {artifact.apiVersion}/{artifact.kind}"
        )
    return _PromotedArtifactReceipt(receipt.spec.desired.unitContentId, descriptor)


@dataclass(frozen=True, slots=True)
class CanonicalArtifactImportResolver:
    """Resolve promoted artifacts only from canonical issued plane entries."""

    catalog: ResourceCatalog

    def resolve(self, request: ArtifactImportRequest) -> ArtifactImportResolution:
        request._validate()
        source = request.promotion_source.source_desired.workspace
        observed = request.promotion_source.source_observed.workspace
        imported = request.artifact_import
        stack = _plane_stack(self.catalog, source, imported.fromPromotion.stack)
        if (
            stack.metadata.deletion is not None
            or not isinstance(stack.spec, DesiredStackSpec)
            or stack.spec.activeProjection is None
        ):
            raise ProjectionCompilerError("promoted artifact source Stack is deleting or inactive")
        if stack.metadata.uid is None:
            raise ProjectionCompilerError("promoted artifact source Stack has no UID")
        template = _plane_stack_template(self.catalog, source, stack.spec.templateRef.name)
        if template.metadata.deletion is not None:
            raise ProjectionCompilerError("promoted artifact source StackTemplate is deleting")
        unit_entry = _canonical_plane_entry(source, f"units/{stack.name}/{imported.unit}")
        unit_key = unit_entry.key
        unit = _catalog(
            self.catalog.parse_unit, _decode_source_document(unit_entry.content or b"", unit_key), "desired"
        )
        assert isinstance(unit, UnitResource)
        owner = unit.metadata.ownerReferences
        if (
            unit.metadata.uid is None
            or not isinstance(owner, list)
            or len(owner) != 1
            or owner[0].apiVersion != CORE_API_VERSION
            or owner[0].kind != "Stack"
            or owner[0].name != stack.name
            or owner[0].uid != stack.metadata.uid
        ):
            raise ProjectionCompilerError("promoted artifact source Unit ownership does not match source Stack")
        assert stack.spec.activeProjection is not None
        binding = stack.spec.activeProjection.units.get(imported.unit)
        structural = stack.spec.structuralProjection.identity
        if binding is None:
            raise ProjectionCompilerError("promoted artifact source Unit is not in the Stack active projection")
        if (
            binding.apiVersion != unit.gvk.api_version
            or binding.kind != unit.gvk.kind
            or binding.name != unit.name
            or binding.uid != unit.metadata.uid
            or binding.desiredDigest != desired_unit_binding_digest(unit)
            or binding.sourceProjectionDigest != structural.projectionDigest
            or binding.projectionContextDigest != structural.projectionContextDigest
        ):
            raise ProjectionCompilerError(
                "promoted artifact source Unit does not match its exact Stack active projection binding"
            )
        receipt_entry = _canonical_plane_entry(observed, f"units/{stack.name}/{imported.unit}")
        receipt_key = receipt_entry.key
        receipt = _parse_promoted_artifact_receipt(
            _decode_source_document(receipt_entry.content or b"", receipt_key),
            unit=unit,
            qualified_name=f"{stack.name}/{imported.unit}",
            artifact=imported,
        )
        if receipt.unit_content_id != entry_content_id(unit_entry).value:
            raise ProjectionCompilerError("promoted artifact receipt is stale for source desired Unit")
        artifact_entry = _canonical_plane_entry(observed, f"artifacts/{stack.name}/{imported.unit}/{imported.name}")
        artifact_key = artifact_entry.key
        artifact_raw = artifact_entry.content or b""
        artifact_document = _decode_source_document(artifact_raw, artifact_key)
        digest = _digest(artifact_raw)
        artifact_kind = unit.driver.artifact_outputs.get(imported.name)
        if artifact_kind is None:
            raise ProjectionCompilerError(f"promoted source Unit does not produce artifact {imported.name!r}")
        if (
            artifact_kind.gvk.api_version != imported.apiVersion
            or artifact_kind.gvk.kind != imported.kind
            or receipt.descriptor.apiVersion != artifact_kind.gvk.api_version
            or receipt.descriptor.kind != artifact_kind.gvk.kind
        ):
            raise ProjectionCompilerError("promoted artifact contract identity does not match its producer")
        try:
            artifact_api = require_artifact_api(artifact_kind)
        except (TypeError, ValueError) as exc:
            raise ProjectionCompilerError(f"promoted artifact contract is invalid: {exc}") from exc
        expected_media_type = f"{artifact_api.media_type}+{'json' if artifact_key.endswith('.json') else 'yaml'}"
        if receipt.descriptor.mediaType != expected_media_type:
            raise ProjectionCompilerError("promoted artifact receipt descriptor has the wrong media type")
        if receipt.descriptor.digest != digest or receipt.descriptor.path != artifact_key:
            raise ProjectionCompilerError("promoted artifact bytes do not match its authenticated receipt descriptor")
        try:
            typed_artifact = self.catalog.parse_artifact(
                artifact_api, artifact_document, f"promoted {unit.driver_name} artifact {imported.name}"
            )
            artifact_document = artifact_api.dump(typed_artifact)
        except (OperationError, KeyError, TypeError, ValueError) as exc:
            raise ProjectionCompilerError(f"invalid promoted artifact document: {exc}") from exc
        metadata = artifact_document.get("metadata")
        producer = artifact_document.get("producer")
        desired_source = getattr(unit.spec, "source", None)
        qualified_name = f"{stack.name}/{imported.unit}"
        if not isinstance(metadata, Mapping) or metadata.get("name") != imported.name:
            raise ProjectionCompilerError("promoted artifact document does not match requested GVK/name")
        if (
            not isinstance(producer, Mapping)
            or not isinstance(desired_source, DesiredSource)
            or producer.get("apiVersion") != unit.driver.api_version
            or producer.get("kind") != unit.driver.kind
            or producer.get("name") != unit.name
            or producer.get("qualifiedName") != qualified_name
            or producer.get("driverVersion") != unit.driver.version
            or producer.get("sourceRevision") != desired_source.revision
            or producer.get("inputHashVersion") != 1
            or producer.get("inputHash") != desired_source.inputHash
        ):
            raise ProjectionCompilerError("promoted artifact document has stale producer identity")
        if request.target_stack.metadata.uid is None:
            raise ProjectionCompilerError("target Stack has no issued UID")
        resolved = ResolvedArtifactImport(
            sourceStack=stack.name,
            sourceStackUid=stack.metadata.uid,
            sourceUnit=imported.unit,
            sourceUnitUid=unit.metadata.uid,
            sourceDesiredRevision=request.lineage.source_desired_revision,
            sourceObservedRevision=request.lineage.source_observed_revision,
            receiptUnitContentId=receipt.unit_content_id,
            artifactName=imported.name,
            apiVersion=imported.apiVersion,
            kind=imported.kind,
            artifactDigest=digest,
            targetStackUid=request.target_stack.metadata.uid,
            artifactDocument=JsonObjectValue(artifact_document),
        )
        return ArtifactImportResolution(request, resolved)


@dataclass(frozen=True, slots=True)
class UnitSourceSelection:
    """One exact retained plane chosen by an explicit source policy."""

    descriptor: RetainedSourceDescriptor
    plane: RetainedSourcePlane


@dataclass(frozen=True, slots=True)
class UnitSourceSelectionRequest:
    """Generic source facts supplied to a Unit source-selection policy."""

    qualified_name: str
    unit: UnitResource[Any]
    source_request: Mapping[str, object]
    prior_source: DesiredSource | None
    primary: RetainedSourceDescriptor | None
    named: tuple[RetainedSourceDescriptor, ...]
    retained_sources: tuple[RetainedSourcePlane, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.qualified_name, str)
            or not self.qualified_name
            or self.qualified_name != self.qualified_name.strip()
            or "\\" in self.qualified_name
            or any(ord(character) < 32 or ord(character) == 127 for character in self.qualified_name)
        ):
            raise ProjectionCompilerError("Unit source selection requires a canonical storage-qualified Unit name")
        segments = self.qualified_name.split("/")
        if any(segment in {"", ".", ".."} for segment in segments) or segments[-1] != self.unit.name:
            raise ProjectionCompilerError("Unit source selection qualified name must match the selected Unit")


class UnitSourceSelector(Protocol):
    """Choose exact retained source evidence without interpreting Git IDs."""

    def select(self, request: UnitSourceSelectionRequest) -> UnitSourceSelection: ...


@dataclass(frozen=True, slots=True)
class RoleBoundUnitSourceSelector:
    """Fail-closed default for explicit primary and single workload evidence.

    Source systems with ancestry/availability policy supply their own selector;
    this default intentionally never infers a revision from a checkout.
    """

    def select(self, request: UnitSourceSelectionRequest) -> UnitSourceSelection:
        for descriptor in (*request.named, *((request.primary,) if request.primary is not None else ())):
            descriptor._validate()
        for plane in request.retained_sources:
            plane._validate()
        qualified = tuple(
            item
            for item in request.named
            if item.role is SourceBindingRole.WORKLOAD and item.binding_key == request.qualified_name
        )
        # An adapter-issued qualified workload binding is always more specific
        # than the operation's authored-source binding.  This includes external
        # templates whose Unit source omits an explicit revision and inherits
        # the exact acquired template snapshot.
        descriptors = qualified or ((request.primary,) if request.primary is not None else ())
        descriptors = tuple(item for item in descriptors if item is not None)
        if len(descriptors) != 1:
            raise ProjectionCompilerError("Unit source selection requires one explicit primary or workload source")
        descriptor = descriptors[0]
        planes = tuple(plane for plane in request.retained_sources if descriptor in plane.descriptors)
        if len(planes) != 1:
            raise ProjectionCompilerError("selected Unit source has no exact recovered workspace")
        return UnitSourceSelection(descriptor, planes[0])


class UnitTemplateResolver(Protocol):
    """Resolve typed driver templates using only explicit projection facts."""

    def resolve(
        self,
        value: object,
        pointer: str,
        *,
        unit: UnitResource[Any],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> TemplateResolution: ...


@dataclass(frozen=True, slots=True)
class ProjectedUnitCandidate:
    """Exact typed desired Unit evidence available to one resolution session."""

    qualified_name: str
    projected: ProjectedDocument
    unit: UnitResource[Any]
    content_id: ContentId
    selected: bool


@dataclass(slots=True)
class TemplateResolutionSession:
    """Operation-local resolver over exact projected and observed evidence."""

    observed: ImmutableWorkspace
    catalog: ResourceCatalog
    candidates: dict[str, ProjectedUnitCandidate]
    known_candidates: set[str]
    imported_artifacts: dict[str, ArtifactImportResolution]

    @classmethod
    def begin(
        cls,
        catalog: ResourceCatalog,
        observed: ImmutableWorkspace,
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument] | None = None,
        current_workspace: ImmutableWorkspace | None = None,
        imported_artifacts: Mapping[str, ArtifactImportResolution] | None = None,
    ) -> TemplateResolutionSession:
        session = cls(observed, catalog, {}, set(), dict(imported_artifacts or {}))
        for key, resolution in session.imported_artifacts.items():
            resolution._validate()
            expected_key = f"{resolution.resolved.sourceUnit}/{resolution.resolved.artifactName}"
            if key != expected_key:
                raise ProjectionCompilerError("imported artifact resolution has a non-canonical Unit/name key")
        if (current_desired is None) != (current_workspace is None):
            raise ProjectionCompilerError("template resolution current desired documents require their exact workspace")
        if current_desired is not None and current_workspace is not None:
            session._seed_current(current_desired, current_workspace)
        return session

    def declare(self, qualified_name: str) -> None:
        self.known_candidates.add(qualified_name)
        # A selected Unit must be projected in this operation before its old
        # observation can satisfy a dependent.  This prevents stale current
        # desired state from winning over the evolving candidate.
        self.candidates.pop(qualified_name, None)

    def record(self, unit: UnitResource[Any], projected: ProjectedDocument) -> None:
        qualified_name = projected.identity[2]
        if qualified_name in self.candidates:
            raise ProjectionCompilerError(f"template resolution session repeats Unit {qualified_name!r}")
        expected_identity = (unit.gvk.api_version, unit.gvk.kind, qualified_name)
        if projected.identity != expected_identity or projected.mutable_document() != self.catalog.serialize_unit(
            unit, profile="desired"
        ):
            raise ProjectionCompilerError("template resolution candidate does not bind its exact typed Unit")
        entry = WorkspaceEntry.file(projected.key, _canonical(projected.document))
        self.candidates[qualified_name] = ProjectedUnitCandidate(
            qualified_name, projected, unit, entry_content_id(entry), True
        )

    def resolve(
        self, value: object, pointer: str, *, unit: UnitResource[Any], context: ApplyProjectionContext
    ) -> TemplateResolution:
        def receipt(target: object) -> FingerprintedValue:
            candidate = self._candidate(getattr(target, "unit", None), "receipt")
            typed, raw = self._receipt(candidate)
            result = candidate.unit.driver.result_contract.dump(typed.status.result)
            return FingerprintedValue(_json_pointer(result, getattr(target, "pointer", "")), _digest(raw))

        def artifact(target: object) -> FingerprintedValue:
            name = getattr(target, "unit", None)
            artifact_name = getattr(target, "name", None)
            if isinstance(name, str) and name not in self.known_candidates:
                imported = self._imported_artifact(name, artifact_name, target)
                if imported is not None:
                    return imported
            candidate = self._candidate(name, "artifact")
            typed_receipt, _ = self._receipt(candidate)
            document, raw = self._artifact(candidate, typed_receipt, target, artifact_name)
            return FingerprintedValue(_json_pointer(document, getattr(target, "pointer", "")), _digest(raw))

        def unavailable(_target: object) -> FingerprintedValue:
            raise ReferenceUnavailable(f"reference for Unit {unit.name!r} is unavailable")

        def environment(pointer: str) -> FingerprintedValue:
            projection = context.projection_context
            if projection is None:
                raise ReferenceUnavailable("Environment resource is unavailable")
            document = _decode_source_document(projection.environment_document, "environment.yaml")
            try:
                normalized = self.catalog.normalize_environment(document, context.environment_id.value)
            except (OperationError, TypeError, ValueError) as exc:
                raise ReferenceUnavailable(f"Environment resource is invalid: {exc}") from exc
            raw = _canonical(normalized)
            return FingerprintedValue(_json_pointer(normalized, pointer), _digest(raw))

        return resolve_template(
            value,
            ResolutionContext(
                receipt,
                artifact,
                unavailable,
                environment=environment,
                unit=unit.name,
                dry=context.dry_run,
            ),
            pointer,
        )

    def _candidate(self, name: object, kind: str) -> ProjectedUnitCandidate:
        if not isinstance(name, str) or name not in self.known_candidates:
            raise ReferenceUnavailable(f"{kind} producer {name!r} is not selected")
        candidate = self.candidates.get(name)
        if candidate is None:
            raise PendingTemplateReference(f"{kind} producer {name!r} is pending projection")
        return candidate

    def _receipt(self, candidate: ProjectedUnitCandidate) -> tuple[Any, bytes]:
        try:
            document, raw, _ = self._observed_resource(f"units/{candidate.qualified_name}")
        except _ObservedResourceAbsent:
            if candidate.selected:
                raise PendingObservedEvidence(
                    f"receipt producer {candidate.qualified_name!r} is pending observed evidence"
                ) from None
            raise ReferenceUnavailable(
                f"receipt producer {candidate.qualified_name!r} has no observed evidence"
            ) from None
        try:
            receipt = self.catalog.parse_receipt(document)
        except (OperationError, TypeError, ValueError) as exc:
            raise ReferenceUnavailable(
                f"receipt producer {candidate.qualified_name!r} has invalid observed evidence: {exc}"
            ) from exc
        subject = receipt.spec.subject
        if (
            receipt.gvk != candidate.unit.gvk
            or receipt.name != candidate.unit.name
            or subject.apiVersion != candidate.unit.gvk.api_version
            or subject.kind != candidate.unit.gvk.kind
            or subject.name != candidate.unit.name
            or subject.qualifiedName != candidate.qualified_name
        ):
            raise ReferenceUnavailable(
                f"receipt producer {candidate.qualified_name!r} has a foreign typed Unit identity"
            )
        if receipt.spec.desired.unitContentId != candidate.content_id.value:
            if candidate.selected:
                raise PendingObservedEvidence(f"receipt is stale: {candidate.qualified_name}")
            raise ReferenceUnavailable(f"receipt producer {candidate.qualified_name!r} is stale for its current Unit")
        return receipt, raw

    def _artifact(
        self,
        candidate: ProjectedUnitCandidate,
        receipt: Any,
        target: object,
        artifact_name: object,
    ) -> tuple[JsonObject, bytes]:
        if not isinstance(artifact_name, str):
            raise ReferenceUnavailable("artifact reference has no valid name")
        requested_api_version = getattr(target, "apiVersion", None)
        requested_kind = getattr(target, "kind", None)
        artifact_kind = candidate.unit.driver.artifact_outputs.get(artifact_name)
        if (
            artifact_kind is None
            or artifact_kind.gvk.api_version != requested_api_version
            or artifact_kind.gvk.kind != requested_kind
        ):
            raise ReferenceUnavailable(
                f"Unit {candidate.qualified_name!r} does not produce requested artifact {artifact_name!r}"
            )
        descriptor = (receipt.status.artifacts or {}).get(artifact_name)
        if descriptor is None:
            raise PendingTemplateReference(
                f"artifact producer {candidate.qualified_name!r} has no current {artifact_name!r} descriptor"
            )
        base_key = f"artifacts/{candidate.qualified_name}/{artifact_name}"
        try:
            document, raw, key = self._observed_resource(base_key)
        except _ObservedResourceAbsent:
            if candidate.selected:
                raise PendingTemplateReference(
                    f"artifact producer {candidate.qualified_name!r} is pending {artifact_name!r} evidence"
                ) from None
            raise ReferenceUnavailable(
                f"artifact producer {candidate.qualified_name!r} has no {artifact_name!r} evidence"
            ) from None
        if (
            descriptor.path != key
            or descriptor.apiVersion != artifact_kind.gvk.api_version
            or descriptor.kind != artifact_kind.gvk.kind
            or descriptor.digest != _digest(raw)
        ):
            raise ReferenceUnavailable(
                f"artifact producer {candidate.qualified_name!r} has unauthenticated {artifact_name!r} evidence"
            )
        artifact_api = require_artifact_api(artifact_kind)
        expected_media_type = f"{artifact_api.media_type}+{'json' if key.endswith('.json') else 'yaml'}"
        if descriptor.mediaType != expected_media_type:
            raise ReferenceUnavailable(
                f"artifact producer {candidate.qualified_name!r} has the wrong {artifact_name!r} media type"
            )
        try:
            resource = self.catalog.parse_artifact(
                artifact_api, document, f"persisted {candidate.unit.driver_name} artifact {artifact_name}"
            )
            typed = artifact_api.dump(resource)
        except (OperationError, TypeError, ValueError) as exc:
            raise ReferenceUnavailable(
                f"artifact producer {candidate.qualified_name!r} has invalid {artifact_name!r} evidence: {exc}"
            ) from exc
        metadata = typed.get("metadata")
        producer = typed.get("producer")
        source = getattr(candidate.unit.spec, "source", None)
        if not isinstance(metadata, Mapping) or metadata.get("name") != artifact_name:
            raise ReferenceUnavailable(f"artifact {artifact_name!r} has the wrong resource identity")
        if (
            not isinstance(producer, Mapping)
            or not isinstance(source, DesiredSource)
            or producer.get("apiVersion") != candidate.unit.gvk.api_version
            or producer.get("kind") != candidate.unit.gvk.kind
            or producer.get("name") != candidate.unit.name
            or producer.get("qualifiedName") != candidate.qualified_name
            or producer.get("driverVersion") != candidate.unit.driver.version
            or producer.get("inputHashVersion") != 1
        ):
            raise ReferenceUnavailable(f"artifact {artifact_name!r} has a foreign producer identity")
        if producer.get("sourceRevision") != source.revision or producer.get("inputHash") != source.inputHash:
            if candidate.selected:
                raise PendingTemplateReference(
                    f"artifact producer {candidate.qualified_name!r} has stale {artifact_name!r} evidence"
                )
            raise ReferenceUnavailable(
                f"artifact producer {candidate.qualified_name!r} has stale current {artifact_name!r} evidence"
            )
        return typed, raw

    def _imported_artifact(self, unit_name: str, artifact_name: object, target: object) -> FingerprintedValue | None:
        if not isinstance(artifact_name, str):
            return None
        resolution = self.imported_artifacts.get(f"{unit_name}/{artifact_name}")
        if resolution is None:
            return None
        resolution._validate()
        resolved = resolution.resolved
        if (
            resolved.sourceUnit != unit_name
            or resolved.artifactName != artifact_name
            or resolved.apiVersion != getattr(target, "apiVersion", None)
            or resolved.kind != getattr(target, "kind", None)
        ):
            raise ReferenceUnavailable("imported artifact evidence does not match the requested artifact")
        evidence = cast(JsonObject, _plain(resolved.to_dict()))
        return FingerprintedValue(
            _json_pointer(resolved.artifactDocument, getattr(target, "pointer", "")),
            resolved.artifactDigest,
            imported=True,
            evidence=evidence,
        )

    def _seed_current(
        self,
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
    ) -> None:
        entries = {entry.key: entry for entry in current_workspace.list_entries()}
        for projected in current_desired.values():
            if projected.identity[1] in {"Stack", "StackTemplate"}:
                continue
            entry = entries.get(projected.key)
            if entry is None or entry.kind is not WorkspaceEntryKind.FILE:
                raise ProjectionCompilerError(
                    f"current Unit {projected.identity[2]!r} has no exact desired workspace entry"
                )
            document = _decode_source_document(entry.content or b"", projected.key)
            if _canonical(document) != _canonical(projected.document):
                raise ProjectionCompilerError(
                    f"current Unit {projected.identity[2]!r} does not match its exact desired workspace bytes"
                )
            parsed = _catalog(self.catalog.parse_unit, document, "desired")
            if not isinstance(parsed, UnitResource):
                continue
            qualified_name = projected.identity[2]
            self.known_candidates.add(qualified_name)
            self.candidates[qualified_name] = ProjectedUnitCandidate(
                qualified_name, projected, parsed, entry_content_id(entry), False
            )

    def _observed_resource(self, base_key: str) -> tuple[JsonObject, bytes, str]:
        keys = tuple(f"{base_key}{suffix}" for suffix in (".json", ".yaml", ".yml"))
        entries = [entry for entry in self.observed.list_entries() if entry.key in keys]
        if not entries:
            raise _ObservedResourceAbsent(f"observed reference {base_key!r} is unavailable")
        if len(entries) != 1:
            raise ProjectionCompilerError(f"observed reference {base_key!r} is ambiguous")
        raw = self.observed.read(entries[0].key)
        return _decode_source_document(raw, entries[0].key), raw, entries[0].key


class _ObservedResourceAbsent(ReferenceUnavailable):
    """An exact observed resource is absent rather than ambiguous or corrupt."""


@dataclass(frozen=True, slots=True)
class ClosedTemplateResolver:
    """Production-safe default resolver for literals; external references fail closed.

    Composition can replace this narrow pure port with a resolver backed by
    issued receipts, artifact imports, and promotion planes.  It never guesses
    those facts from a checkout or controller state.
    """

    def resolve(
        self,
        value: object,
        pointer: str,
        *,
        unit: UnitResource[Any],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> TemplateResolution:
        del observed, context

        def unavailable(_target: object) -> Any:
            raise ReferenceUnavailable(f"template references are unavailable for Unit {unit.name!r}")

        try:
            return resolve_template(
                value,
                ResolutionContext(unavailable, unavailable, unavailable, unit=unit.name),
                pointer,
            )
        except OperationError as exc:
            raise ProjectionCompilerError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SessionTemplateResolver:
    """Expose one operation-local session through the Unit resolver port."""

    session: TemplateResolutionSession

    def resolve(
        self,
        value: object,
        pointer: str,
        *,
        unit: UnitResource[Any],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> TemplateResolution:
        if observed is not self.session.observed:
            raise ProjectionCompilerError("template resolution session observed workspace mismatch")
        return self.session.resolve(value, pointer, unit=unit, context=context)


@dataclass(frozen=True, slots=True)
class WorkspaceUnitInputHasher:
    """Legacy-compatible input identity over an immutable logical workspace."""

    def hash(self, unit: UnitResource[Any], workspace: ImmutableWorkspace, source: Mapping[str, object]) -> str:
        path = source.get("path")
        inputs_value = source.get("inputs")
        if not isinstance(path, str):
            raise ProjectionCompilerError(f"Unit {unit.name!r} source path is invalid")
        inputs = [path] if inputs_value is None else inputs_value
        if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
            raise ProjectionCompilerError(f"Unit {unit.name!r} source inputs are invalid")
        root = "." if inputs_value is None else path
        root_prefix = "" if root == "." else f"{_safe_relative(root, 'source path')}/"
        selected: dict[str, WorkspaceEntry] = {}
        entries = workspace.list_entries()
        for pattern in inputs:
            relative = _safe_relative(pattern, "source input")
            matches = _workspace_input_matches(entries, root_prefix, relative)
            if not matches:
                raise ProjectionCompilerError(f"source input does not exist: {path}/{pattern}")
            selected.update(matches)
        specification = _plain(unit.driver.unit_contract.dump(unit.spec))
        if isinstance(specification, dict) and isinstance(specification.get("source"), dict):
            specification = dict(specification)
            source_spec = dict(cast(dict[str, object], specification["source"]))
            source_spec.pop("revision", None)
            specification["source"] = source_spec
        payload = {
            "inputHashVersion": 1,
            "kind": "unit",
            "driver": unit.driver_name,
            "driverVersion": unit.driver.version,
            "specification": specification,
            "files": [_workspace_input_entry(name, entry) for name, entry in sorted(selected.items())],
        }
        return f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CatalogLogicalUnitProjector:
    """Authoritative catalog/driver logical Unit projector.

    The projector resolves driver models from an issued primary retained source
    and an injected pure template resolver.  Its required host owns any
    effectful driver materialization and returns explicit logical payload
    entries, so physical paths never enter this application module.
    """

    catalog: ResourceCatalog
    source_encoder: SourceLineageEncoder
    host: UnitProjectionHost
    source_selector: UnitSourceSelector = RoleBoundUnitSourceSelector()
    template_resolver: UnitTemplateResolver = ClosedTemplateResolver()
    input_hasher: WorkspaceUnitInputHasher = WorkspaceUnitInputHasher()

    def project_unit(
        self,
        unit: UnitResource[Any],
        *,
        metadata: ResourceMetadata,
        previous: ProjectedDocument | None,
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
        session: TemplateResolutionSession | None = None,
    ) -> UnitProjection:
        authored = _catalog(self.catalog.parse_unit, self.catalog.serialize_unit(unit, profile="authored"), "authored")
        assert isinstance(authored, UnitResource)
        qualified_name = (
            metadata.name if metadata.ownerReferences is None else f"{metadata.ownerReferences[0].name}/{metadata.name}"
        )
        source, primary = _desired_unit_source(
            authored,
            previous,
            retained_sources,
            context,
            self.source_encoder,
            self.source_selector,
            self.input_hasher,
            qualified_name,
        )
        request = UnitProjectionRequest(
            authored,
            metadata,
            previous,
            current_workspace,
            source,
            primary,
            qualified_name,
            context.environment_id,
            lambda value, pointer: (
                SessionTemplateResolver(session) if session is not None else self.template_resolver
            ).resolve(value, pointer, unit=authored, observed=observed, context=context),
        )
        try:
            return self.host.project(request)
        except (OperationError, TypeError, ValueError) as exc:
            raise ProjectionCompilerError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class PromotionLineage:
    """The temporary Git-shaped fields required by the current public schema.

    This is intentionally a small, immutable bridge.  The application never
    derives these spellings from opaque snapshots; a source-specific encoder
    must validate and supply them from issued promotion evidence.
    """

    source_environment: EnvironmentId
    source_desired_ref: str
    source_desired_revision: str
    source_observed_ref: str
    source_observed_revision: str
    target_desired_ref: str
    target_desired_revision: str
    target_observed_ref: str
    target_observed_revision: str
    lineage_evidence: ContentId


class PromotionLineageEncoder(Protocol):
    """Translate issued generic promotion evidence into the legacy schema seam."""

    def encode(self, descriptor: PromotionSourceDescriptor) -> PromotionLineage: ...


@dataclass(frozen=True, slots=True)
class SourceLineage:
    """The Git-shaped source fields required by the transitional public schema."""

    repository: str
    revision: str


class SourceLineageEncoder(Protocol):
    """Translate one issued source descriptor without leaking Git into application."""

    def encode(self, descriptor: RetainedSourceDescriptor, plane: RetainedSourcePlane) -> SourceLineage: ...


@dataclass(frozen=True, slots=True)
class CatalogUnitProjectionCompiler:
    """Project ordinary Units through the authoritative catalog and driver port."""

    catalog: ResourceCatalog
    projector: LogicalUnitProjector

    def project(
        self,
        documents: tuple[FrozenAuthoredDocument, ...],
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> CandidateTransformation:
        writes: list[ProjectedDocument] = []
        payload_writes: list[WorkspaceEntry] = []
        payload_deletes: list[str] = []
        replacements: list[PayloadPrefixReplacement] = []
        prefixes: set[str] = set()
        for frozen in documents:
            raw_metadata = frozen.document.get("metadata")
            canonical_desired = isinstance(raw_metadata, Mapping) and "uid" in raw_metadata
            authored = _catalog(
                self.catalog.parse_unit,
                frozen.document,
                "desired" if canonical_desired else "authored",
            )
            assert isinstance(authored, UnitResource)
            previous = current_desired.get((authored.gvk.api_version, authored.gvk.kind, authored.name))
            metadata = _root_metadata(
                authored.gvk.api_version, authored.gvk.kind, authored.name, frozen.content_id, previous, context
            )
            projected = (
                UnitProjection(authored.with_metadata(metadata))
                if canonical_desired
                else self.projector.project_unit(
                    authored,
                    metadata=metadata,
                    previous=previous,
                    current_workspace=current_workspace,
                    retained_sources=retained_sources,
                    observed=observed,
                    context=context,
                )
            )
            _validate_projected_unit(self.catalog, projected, authored, metadata)
            document = self.catalog.serialize_unit(projected.unit, profile="desired")
            candidate = ProjectedDocument(f"units/{authored.name}.json", document)
            if previous is not None and _canonical(candidate.document) == _canonical(previous.document):
                candidate = previous
            writes.append(candidate)
            payload_writes.extend(projected.payload_writes)
            payload_deletes.extend(projected.payload_deletes)
            prefixes.update(projected.payload_prefixes)
            replacements.extend(projected.payload_replacements)
        return CandidateTransformation(
            tuple(writes),
            payload_writes=tuple(payload_writes),
            payload_deletes=tuple(payload_deletes),
            payload_prefixes=tuple(sorted(prefixes)),
            payload_replacements=tuple(replacements),
        )


@dataclass(frozen=True, slots=True)
class CatalogStackProjectionCompiler:
    """Pure catalog-based Stack/StackTemplate compiler.

    Repository templates are read only from an adapter-issued retained source
    descriptor.  Promotion selectors fail closed until composition supplies
    their own durable lineage compiler; that is intentional because the base
    projection context contains opaque snapshots, not Git revision evidence.
    """

    catalog: ResourceCatalog
    unit_projector: LogicalUnitProjector
    promotion_encoder: PromotionLineageEncoder | None = None
    source_encoder: SourceLineageEncoder | None = None
    artifact_import_resolver: ArtifactImportResolver | None = None

    def project(
        self,
        documents: tuple[FrozenAuthoredDocument, ...],
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> CandidateTransformation:
        authored_templates: dict[str, tuple[StackResource, FrozenAuthoredDocument]] = {}
        authored_stacks: dict[str, tuple[StackResource, FrozenAuthoredDocument]] = {}
        for frozen in documents:
            document = frozen.document
            kind = document.get("kind")
            metadata = document.get("metadata")
            canonical_desired = isinstance(metadata, Mapping) and "uid" in metadata
            if kind == "StackTemplate":
                parsed = _catalog(
                    self.catalog.parse_stack_template,
                    document,
                    "desired" if canonical_desired else "authored",
                )
                assert isinstance(parsed, StackResource)
                if isinstance(parsed.spec, DesiredStackTemplateSpec):
                    parsed = StackResource(
                        parsed.gvk,
                        ResourceMetadata(name=parsed.name),
                        StackTemplateInlineSpec(
                            parameters=list(parsed.spec.parameters),
                            unitTemplates=dict(parsed.spec.unitTemplates),
                        ),
                    )
                authored_templates[parsed.name] = (parsed, frozen)
            elif kind == "Stack":
                parsed = _catalog(
                    self.catalog.parse_stack,
                    document,
                    "desired" if canonical_desired else "authored",
                )
                assert isinstance(parsed, StackResource)
                if isinstance(parsed.spec, DesiredStackSpec):
                    parsed = StackResource(
                        parsed.gvk,
                        ResourceMetadata(name=parsed.name),
                        StackSpec(
                            template=parsed.spec.templateRef.name,
                            parameters=parsed.spec.parameters,
                            units=parsed.spec.units,
                            artifactImports=parsed.spec.artifactImports,
                        ),
                    )
                authored_stacks[parsed.name] = (parsed, frozen)
            else:
                raise ProjectionCompilerError("Stack compiler received a non-Stack document")

        current_templates, current_stacks = _current_stacks(self.catalog, current_desired)
        desired_templates = dict(current_templates)
        writes: list[ProjectedDocument] = []
        for name, (authored, frozen) in sorted(authored_templates.items()):
            previous = current_desired.get((CORE_API_VERSION, "StackTemplate", name))
            metadata = _root_metadata(CORE_API_VERSION, "StackTemplate", name, frozen.content_id, previous, context)
            desired = self._template(authored, frozen.content_id, metadata, retained_sources, context)
            desired_templates[name] = desired
            writes.append(
                ProjectedDocument(
                    f"stack-templates/{name}.json", self.catalog.serialize_stack_resource(desired, profile="desired")
                )
            )

        desired_stacks = dict(current_stacks)
        for name, (authored, frozen) in sorted(authored_stacks.items()):
            previous = current_desired.get((CORE_API_VERSION, "Stack", name))
            metadata = _root_metadata(CORE_API_VERSION, "Stack", name, frozen.content_id, previous, context)
            assert isinstance(authored.spec, StackSpec)
            desired_stacks[name] = StackResource(authored.gvk, metadata, authored.spec)

        reproject = set(authored_stacks)
        changed_templates = set(authored_templates)
        for name, stack in current_stacks.items():
            if name in reproject or not isinstance(stack.spec, DesiredStackSpec):
                continue
            if stack.metadata.deletion is None and stack.spec.templateRef.name in changed_templates:
                reproject.add(name)

        generated: set[tuple[str, str, str]] = set()
        context_writes: dict[str, WorkspaceEntry] = {}
        payload_writes: list[WorkspaceEntry] = []
        payload_deletes: list[str] = []
        replacements: list[PayloadPrefixReplacement] = []
        prefixes: set[str] = {_CONTEXT_PREFIX}
        transition_blocks = load_workspace_transition_blocks(current_workspace)
        for name in sorted(reproject):
            stack = desired_stacks[name]
            if stack.metadata.deletion is not None:
                continue
            compiled = self._stack(
                stack,
                desired_templates,
                current_desired,
                current_workspace,
                retained_sources,
                observed,
                context,
                previous_stack=current_stacks.get(name),
            )
            writes.append(compiled.projected)
            generated.update(compiled.active_identities)
            transition_blocks = {
                qualified_name: reason
                for qualified_name, reason in transition_blocks.items()
                if not qualified_name.startswith(f"{name}/")
            }
            transition_blocks.update(compiled.blocks)
            for unit, output in compiled.units:
                document = self.catalog.serialize_unit(unit, profile="desired")
                child = ProjectedDocument(f"units/{name}/{unit.name}.json", document)
                writes.append(child)
                payload_writes.extend(output.payload_writes)
                payload_deletes.extend(output.payload_deletes)
                prefixes.update(output.payload_prefixes)
                replacements.extend(output.payload_replacements)
            # A carried Stack reprojected because its template changed keeps
            # its pre-existing structural context; an explicitly supplied
            # Stack binds to the current operation context.  The latter record
            # is one candidate payload, even when several selected Stacks use
            # it, rather than duplicate writes to the same logical key.
            if not isinstance(stack.spec, DesiredStackSpec):
                entry = _projection_context_entry(context)
                context_writes[entry.key] = entry

        deletes = _obsolete_owned_keys(current_desired, reproject, generated)
        payload_writes.extend(context_writes.values())
        transition_key = DESIRED_TRANSITION_BLOCKS_PATH.as_posix()
        had_transition_entry = any(entry.key == transition_key for entry in current_workspace.list_entries())
        if transition_blocks:
            prefixes.add(transition_key)
            payload_writes.append(
                WorkspaceEntry.file(
                    transition_key,
                    _canonical({"schema": 1, "blocks": dict(sorted(transition_blocks.items()))}),
                )
            )
        elif had_transition_entry:
            prefixes.add(transition_key)
            payload_deletes.append(transition_key)
        return CandidateTransformation(
            tuple(_coalesce_documents(writes, current_desired)),
            deletes=deletes,
            payload_writes=tuple(payload_writes),
            payload_deletes=tuple(payload_deletes),
            payload_prefixes=tuple(sorted(prefixes)),
            payload_replacements=tuple(replacements),
        )

    def _template(
        self,
        authored: StackResource,
        authored_content_id: ContentId,
        metadata: ResourceMetadata,
        retained_sources: tuple[RetainedSourcePlane, ...],
        context: ApplyProjectionContext,
    ) -> StackResource:
        if isinstance(authored.spec, StackTemplateInlineSpec):
            inline = authored.spec
            digest = authored_content_id.value
            source_context = _inherited_source_context(inline, retained_sources, context, self.source_encoder)
            acquisition = StackTemplateAcquisition(
                documentDigest=digest,
                requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
                resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
            )
        elif isinstance(authored.spec, StackTemplateGitSpec):
            request = authored.spec.source.fromGit
            plane, descriptor = _retained_template_source(retained_sources, context, authored.name, request.path)
            source = _source_lineage(self.source_encoder, descriptor, plane)
            if request.repository != source.repository and not _same_local_repository(
                request.repository, source.repository
            ):
                raise ProjectionCompilerError(
                    "StackTemplate Git source repository does not match issued source lineage"
                )
            request = replace(request, repository=source.repository)
            raw = plane.plane.workspace.read(descriptor.workspace_key)
            digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            if request.documentDigest is not None and request.documentDigest != digest:
                raise ProjectionCompilerError("StackTemplate source documentDigest mismatch")
            document = _decode_source_document(raw, descriptor.workspace_key)
            selected = _catalog(self.catalog.parse_stack_template, document, "authored")
            assert isinstance(selected, StackResource)
            if not isinstance(selected.spec, StackTemplateInlineSpec):
                raise ProjectionCompilerError(
                    "repository StackTemplate source cannot select another source recursively"
                )
            inline = selected.spec
            source_context = StackTemplateSourceContext(repository=source.repository, revision=source.revision)
            acquisition = StackTemplateAcquisition(
                documentDigest=digest,
                requestedSource=StackTemplateRequestedFromGit(fromGit=request),
                resolvedSource=StackTemplateResolvedFromGitSource(
                    fromGit=StackTemplateResolvedFromGit(
                        repository=source.repository, revision=source.revision, path=request.path
                    )
                ),
            )
        elif isinstance(authored.spec, StackTemplatePromotionSpec):
            if self.promotion_encoder is None:
                raise ProjectionCompilerError("StackTemplate fromPromotion requires an explicit promote transaction")
            frozen = context.projection_context
            descriptor = frozen.promotion_source if frozen is not None else None
            if descriptor is None:
                raise ProjectionCompilerError("StackTemplate promotion needs issued promotion source evidence")
            descriptor._validate()
            lineage = self.promotion_encoder.encode(descriptor)
            _validate_promotion_lineage(lineage, descriptor)
            source_stack, source_template, source_template_raw = _promoted_template(
                self.catalog, descriptor, authored.spec.source.fromPromotion.stack
            )
            if source_stack.metadata.uid is None or source_template.metadata.uid is None:
                raise ProjectionCompilerError("promotion source resources require desired UIDs")
            if source_template.name != authored.name:
                raise ProjectionCompilerError("promoted StackTemplate name must match its selected source template")
            source_stack_spec = source_stack.spec
            source_template_spec = source_template.spec
            assert isinstance(source_stack_spec, DesiredStackSpec)
            assert isinstance(source_template_spec, DesiredStackTemplateSpec)
            if source_stack_spec.templateRef.uid != source_template.metadata.uid or (
                source_stack_spec.templateRef.contentDigest != source_template_spec.contentDigest
            ):
                raise ProjectionCompilerError("promotion source Stack has a stale StackTemplate fence")
            inline = StackTemplateInlineSpec(
                parameters=list(source_template_spec.parameters), unitTemplates=dict(source_template_spec.unitTemplates)
            )
            source_context = source_template_spec.sourceContext
            resolved = StackTemplateResolvedFromPromotion(
                environment=str(lineage.source_environment),
                desiredRef=lineage.source_desired_ref,
                desiredRevision=lineage.source_desired_revision,
                stack=source_stack.name,
                stackUid=source_stack.metadata.uid,
                template=source_template.name,
                templateUid=source_template.metadata.uid,
                templateContentDigest=source_template_spec.contentDigest,
            )
            acquisition = StackTemplateAcquisition(
                documentDigest=f"sha256:{hashlib.sha256(source_template_raw).hexdigest()}",
                requestedSource=StackTemplateRequestedFromPromotion(fromPromotion=authored.spec.source.fromPromotion),
                resolvedSource=StackTemplateResolvedFromPromotionSource(fromPromotion=resolved),
            )
        else:
            raise ProjectionCompilerError("StackTemplate has an unsupported source")
        return StackResource(
            authored.gvk,
            metadata,
            DesiredStackTemplateSpec(
                parameters=list(inline.parameters),
                unitTemplates=dict(inline.unitTemplates),
                contentDigest=inline.semantic_content_digest(),
                acquisition=acquisition,
                sourceContext=source_context,
            ),
        )

    def _stack(
        self,
        stack: StackResource,
        templates: Mapping[str, StackResource],
        current: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
        *,
        previous_stack: StackResource | None = None,
    ) -> _StackCompilation:
        if not isinstance(stack.spec, (StackSpec, DesiredStackSpec)):
            raise ProjectionCompilerError(f"Stack {stack.name!r} has an invalid specification")
        template_name = stack.spec.template if isinstance(stack.spec, StackSpec) else stack.spec.templateRef.name
        template = templates.get(template_name)
        if template is None:
            raise ProjectionCompilerError(
                f"Stack {stack.name!r} references missing desired StackTemplate {template_name!r}"
            )
        if template.metadata.deletion is not None:
            raise ProjectionCompilerError(f"Stack {stack.name!r} references deleting StackTemplate {template_name!r}")
        if (
            not isinstance(template.spec, DesiredStackTemplateSpec)
            or stack.metadata.uid is None
            or template.metadata.uid is None
        ):
            raise ProjectionCompilerError("Stack projection requires desired Stack and StackTemplate identities")
        expanded = template.spec.expand(stack.spec.parameters)
        selected = set(stack.spec.units or (resource.name for resource in expanded))
        unknown = sorted(selected - {resource.name for resource in expanded})
        if unknown:
            raise ProjectionCompilerError(f"Stack {stack.name!r} selects unknown Unit templates: {', '.join(unknown)}")
        expanded = tuple(item for item in expanded if item.name in selected)
        for item in expanded:
            missing = sorted(set(item.dependsOn) - selected)
            if missing:
                raise ProjectionCompilerError(
                    f"Stack {stack.name!r} selects {item.name!r} but omits dependencies: {', '.join(missing)}"
                )
        context_digest = (
            stack.spec.structuralProjection.identity.projectionContextDigest
            if isinstance(stack.spec, DesiredStackSpec)
            else _projection_context_digest(context)
        )
        projection_units = {
            item.name: StackProjectionUnit(
                apiVersion=item.apiVersion,
                kind=item.kind,
                spec=ProjectionObject(_normalized_stack_unit_spec(item, template.spec.sourceContext)),
                dependsOn=list(item.dependsOn),
            )
            for item in expanded
        }
        structural = StackProjection.build(
            stack_uid=stack.metadata.uid,
            template_uid=template.metadata.uid,
            template_content_digest=template.spec.contentDigest,
            units=projection_units,
            context_digest=context_digest,
        )
        desired = StackResource(
            stack.gvk,
            stack.metadata,
            DesiredStackSpec(
                templateRef=StackTemplateReference(
                    name=template.name, uid=template.metadata.uid, contentDigest=template.spec.contentDigest
                ),
                parameters=stack.spec.parameters,
                units=stack.spec.units,
                artifactImports=stack.spec.artifactImports,
                structuralProjection=structural,
                activeProjection=stack.spec.activeProjection if isinstance(stack.spec, DesiredStackSpec) else None,
            ),
        )
        resolved_import_resolutions: dict[str, ArtifactImportResolution] = {}
        if stack.spec.artifactImports:
            if self.artifact_import_resolver is None or self.promotion_encoder is None:
                raise ProjectionCompilerError(
                    "Stack artifactImports require explicit promotion and artifact import resolvers"
                )
            projection = context.projection_context
            descriptor = projection.promotion_source if projection is not None else None
            if descriptor is None:
                raise ProjectionCompilerError("Stack artifactImports require issued promotion source evidence")
            lineage = self.promotion_encoder.encode(descriptor)
            _validate_promotion_lineage(lineage, descriptor)
            resolved_imports: dict[str, ResolvedArtifactImport] = {}
            for item in sorted(stack.spec.artifactImports, key=lambda item: (item.unit, item.name)):
                request = ArtifactImportRequest(item, desired, descriptor, lineage)
                request._validate()
                resolution = self.artifact_import_resolver.resolve(request)
                if not isinstance(resolution, ArtifactImportResolution):
                    raise ProjectionCompilerError("artifact import resolver must return an ArtifactImportResolution")
                resolution._validate()
                if resolution.request is not request:
                    raise ProjectionCompilerError("artifact import resolver returned evidence for a different request")
                key = f"{item.unit}/{item.name}"
                resolved_imports[key] = resolution.resolved
                resolved_import_resolutions[key] = resolution
            desired = StackResource(
                desired.gvk, desired.metadata, replace(desired.spec, resolvedArtifactImports=resolved_imports)
            )
        resources = scope_stack_template_resources(
            stack.name,
            tuple(
                StackTemplateResource(
                    apiVersion=item.apiVersion,
                    kind=item.kind,
                    name=item.name,
                    spec=item.spec,
                    dependsOn=list(item.dependsOn),
                )
                for item in expanded
            ),
        )
        units: list[tuple[UnitResource[Any], UnitProjection]] = []
        session = TemplateResolutionSession.begin(
            self.catalog,
            observed,
            current,
            current_workspace,
            resolved_import_resolutions,
        )
        owner = DesiredOwnerReference(
            apiVersion=CORE_API_VERSION, kind="Stack", name=stack.name, uid=stack.metadata.uid
        )
        pending = list(resources)
        for resource in resources:
            session.declare(f"{stack.name}/{resource.name}")
        blocked: list[str] = []
        unavailable: dict[str, str] = {}
        while pending:
            next_pending: list[StackTemplateResource] = []
            progressed = False
            blocked.clear()
            for resource in pending:
                if any(dependency in unavailable for dependency in resource.dependsOn):
                    unavailable[resource.name] = "dependency inputs are unavailable"
                    progressed = True
                    continue
                document: JsonObject = {
                    "apiVersion": resource.apiVersion,
                    "kind": resource.kind,
                    "metadata": {"name": resource.name},
                    "spec": cast(JsonObjectValue, _normalized_stack_unit_spec(resource, template.spec.sourceContext)),
                }
                authored = _catalog(self.catalog.parse_unit, document, "authored")
                assert isinstance(authored, UnitResource)
                metadata = ResourceMetadata(
                    name=resource.name,
                    uid=_owned_uid(stack.name, stack.metadata.uid, resource.name),
                    ownerReferences=[owner],
                )
                try:
                    output = self.unit_projector.project_unit(
                        authored,
                        metadata=metadata,
                        previous=current.get(
                            (authored.gvk.api_version, authored.gvk.kind, f"{stack.name}/{authored.name}")
                        ),
                        current_workspace=current_workspace,
                        retained_sources=retained_sources,
                        observed=observed,
                        context=context,
                        session=session,
                    )
                except (ReferenceUnavailable, ProjectionCompilerError) as exc:
                    if _contains_unavailable_reference(exc):
                        unavailable[resource.name] = str(exc)
                        progressed = True
                        continue
                    if _contains_pending_reference(exc):
                        next_pending.append(resource)
                        blocked.append(f"{resource.name}: {exc}")
                        continue
                    raise
                _validate_projected_unit(self.catalog, output, authored, metadata)
                units.append((output.unit, output))
                session.record(
                    output.unit,
                    ProjectedDocument(
                        f"units/{stack.name}/{output.unit.name}.json",
                        self.catalog.serialize_unit(output.unit, profile="desired"),
                    ),
                )
                progressed = True
            if not progressed:
                raise ProjectionCompilerError(
                    "Stack template references are blocked or cyclic: " + "; ".join(sorted(blocked))
                )
            pending = next_pending
        projected_units = {unit.name: unit for unit, _ in units}
        resolved_projection_units: dict[str, StackProjectionUnit] = {}
        for resource in resources:
            projected_unit = projected_units.get(resource.name)
            original = projection_units[resource.name]
            specification = dict(cast(Mapping[str, object], original.spec))
            if projected_unit is not None:
                desired_document = self.catalog.serialize_unit(projected_unit, profile="desired")
                desired_spec = desired_document.get("spec")
                original_source = specification.get("source")
                desired_source = desired_spec.get("source") if isinstance(desired_spec, Mapping) else None
                if isinstance(original_source, Mapping) and isinstance(desired_source, Mapping):
                    specification["source"] = {
                        **original_source,
                        "revision": desired_source.get("revision"),
                    }
            resolved_projection_units[resource.name] = StackProjectionUnit(
                apiVersion=original.apiVersion,
                kind=original.kind,
                spec=ProjectionObject(cast(Any, specification)),
                dependsOn=list(original.dependsOn),
            )
        structural = StackProjection.build(
            stack_uid=stack.metadata.uid,
            template_uid=template.metadata.uid,
            template_content_digest=template.spec.contentDigest,
            units=resolved_projection_units,
            context_digest=context_digest,
        )
        assert isinstance(desired.spec, DesiredStackSpec)
        desired = StackResource(desired.gvk, desired.metadata, replace(desired.spec, structuralProjection=structural))
        bindings: dict[str, StackProjectionUnitBinding] = {}
        for resource in (item for item in resources if item.name in projected_units):
            unit = projected_units[resource.name]
            if unit.metadata.uid is None:
                raise ProjectionCompilerError(f"Stack projected Unit {stack.name}/{resource.name} has no issued UID")
            bindings[resource.name] = StackProjectionUnitBinding(
                apiVersion=unit.gvk.api_version,
                kind=unit.gvk.kind,
                name=unit.name,
                uid=unit.metadata.uid,
                desiredDigest=desired_unit_binding_digest(unit),
                sourceProjectionDigest=structural.identity.projectionDigest,
                projectionContextDigest=structural.identity.projectionContextDigest,
                dependsOn=list(resource.dependsOn),
            )
        new_bindings = bindings
        prior = previous_stack if previous_stack is not None else stack
        previous_active = prior.spec.activeProjection if isinstance(prior.spec, DesiredStackSpec) else None
        waiting = bool(unavailable)
        staged = False
        if waiting and previous_active is not None:
            staged = all(
                logical_name in structural.units
                and structural.units[logical_name].apiVersion == binding.apiVersion
                and structural.units[logical_name].kind == binding.kind
                and logical_name == binding.name
                and sorted(binding.dependsOn) == sorted(structural.units[logical_name].dependsOn)
                for logical_name, binding in previous_active.units.items()
            )
            if staged:
                bindings = dict(new_bindings)
                for logical_name in unavailable:
                    previous_binding = previous_active.units.get(logical_name)
                    if previous_binding is not None:
                        bindings[logical_name] = previous_binding
            else:
                bindings = dict(previous_active.units)
        else:
            bindings = dict(new_bindings)

        if waiting and (previous_active is None or staged):
            changed = True
            while changed:
                changed = False
                active_names = set(bindings)
                for logical_name, binding in tuple(bindings.items()):
                    if any(dependency not in active_names for dependency in binding.dependsOn):
                        bindings.pop(logical_name)
                        changed = True

        source_projection_digest = (
            previous_active.sourceProjectionDigest
            if waiting and previous_active is not None and not staged
            else structural.identity.projectionDigest
        )
        projection_context_digest = (
            previous_active.projectionContextDigest
            if waiting and previous_active is not None and not staged
            else structural.identity.projectionContextDigest
        )
        active = StackActiveProjection.build(
            source_projection_digest=source_projection_digest,
            projection_context_digest=projection_context_digest,
            units=bindings,
        )
        desired = StackResource(desired.gvk, desired.metadata, replace(desired.spec, activeProjection=active))
        emitted_units = tuple(
            (unit, output)
            for unit, output in units
            if unit.name in bindings and bindings[unit.name] == new_bindings.get(unit.name)
        )
        active_identities = frozenset(
            (binding.apiVersion, binding.kind, f"{stack.name}/{binding.name}") for binding in bindings.values()
        )
        return _StackCompilation(
            ProjectedDocument(
                f"stacks/{stack.name}.json", self.catalog.serialize_stack_resource(desired, profile="desired")
            ),
            emitted_units,
            active_identities,
            {f"{stack.name}/{name}": reason for name, reason in unavailable.items()},
        )


def _catalog(call: Any, document: JsonObject, profile: str) -> object:
    try:
        return call(cast(JsonObject, _plain(document)), profile=profile)
    except (OperationError, TypeError, ValueError) as exc:
        raise ProjectionCompilerError(str(exc)) from exc


def _contains_pending_reference(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ReferenceUnavailable):
            return True
        current = current.__cause__
    return False


def _contains_unavailable_reference(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, PendingObservedEvidence) or (
            isinstance(current, ReferenceUnavailable) and not isinstance(current, PendingTemplateReference)
        ):
            return True
        current = current.__cause__
    return False


def _root_metadata(
    api_version: str,
    kind: str,
    name: str,
    authored_content_id: ContentId,
    previous: ProjectedDocument | None,
    context: ApplyProjectionContext,
) -> ResourceMetadata:
    if previous is not None:
        metadata = cast(Mapping[str, object], previous.document["metadata"])
        if metadata.get("ownerReferences") is not None or metadata.get("deletion") is not None:
            raise ProjectionCompilerError(f"desired {kind} {name!r} cannot be adopted or reapplied while deleting")
        uid = metadata.get("uid")
        if not isinstance(uid, str):
            raise ProjectionCompilerError(f"desired {kind} {name!r} has no UID")
        labels = dict(cast(Mapping[str, str], metadata.get("labels") or {}))
        existing = labels.get(_PARTITION_LABEL)
        if context.partition is not None and existing not in {None, context.partition}:
            raise ProjectionCompilerError(f"desired {kind} {name!r} belongs to partition {existing!r}")
        if context.partition is not None:
            labels[_PARTITION_LABEL] = context.partition
        return ResourceMetadata(name=name, uid=uid, labels=labels or None)
    issuer = context.root_identity_issuer
    if issuer is None:
        raise ProjectionCompilerError("new desired roots require an injected RootIncarnationIssuer")
    identity = (api_version, kind, name)
    request = RootIdentityRequest(
        context.environment_id,
        api_version,
        kind,
        name,
        context.primary_source.retained.source_snapshot_id if context.primary_source is not None else None,
        authored_content_id,
        tuple(
            tombstone.uid
            for tombstone in context.finalized_tombstones
            if (tombstone.api_version, tombstone.kind, tombstone.qualified_name) == identity
        ),
    )
    issued = issuer.issue(request)
    if not isinstance(issued, IssuedRootIdentity):
        raise ProjectionCompilerError("RootIncarnationIssuer must return an IssuedRootIdentity")
    issued._validate()
    if issued.request != request or issued.issuer_id != issuer.issuer_id:
        raise ProjectionCompilerError("issued root identity does not bind this issuer and exact root request")
    uid = issued.uid
    labels = {_PARTITION_LABEL: context.partition} if context.partition is not None else None
    return ResourceMetadata(name=name, uid=uid, labels=labels)


def _validate_projected_unit(
    catalog: ResourceCatalog, output: UnitProjection, authored: UnitResource[Any], metadata: ResourceMetadata
) -> None:
    if not isinstance(output, UnitProjection) or not isinstance(output.unit, UnitResource):
        raise ProjectionCompilerError("Unit projector must return a typed UnitProjection")
    unit = output.unit
    if unit.gvk != authored.gvk or unit.name != authored.name or unit.metadata != metadata:
        raise ProjectionCompilerError("Unit projector changed the catalog GVK, name, or issued metadata")
    document = catalog.serialize_unit(unit, profile="desired")
    _catalog(catalog.parse_unit, document, "desired")


def _desired_unit_source(
    unit: UnitResource[Any],
    previous: ProjectedDocument | None,
    retained_sources: tuple[RetainedSourcePlane, ...],
    context: ApplyProjectionContext,
    encoder: SourceLineageEncoder,
    selector: UnitSourceSelector,
    hasher: WorkspaceUnitInputHasher,
    qualified_name: str,
) -> tuple[DesiredSource | None, RetainedSourcePlane | None]:
    """Construct the typed desired source from explicit retained provenance."""

    specification = _plain(unit.driver.unit_contract.dump(unit.spec))
    if not isinstance(specification, dict) or not isinstance(specification.get("source"), dict):
        return None, None
    source = cast(dict[str, object], specification["source"])
    path = source.get("path")
    if not isinstance(path, str):
        raise ProjectionCompilerError(f"Unit {unit.name!r} has invalid source path")
    prior_source = _previous_unit_source(unit, previous)
    selection = selector.select(
        UnitSourceSelectionRequest(
            qualified_name,
            unit,
            source,
            prior_source,
            context.primary_source,
            context.named_sources,
            retained_sources,
        )
    )
    selection.descriptor._validate()
    if selection.descriptor not in selection.plane.descriptors:
        raise ProjectionCompilerError("Unit source selector returned an unbound retained descriptor")
    lineage = _source_lineage(encoder, selection.descriptor, selection.plane)
    desired = DesiredSource(
        path=path,
        inputs=cast(list[str] | None, source.get("inputs")),
        revision=lineage.revision,
        driverVersion=unit.driver.version,
        inputHash=hasher.hash(unit, selection.plane.plane.workspace, source),
    )
    # A policy-selected historical plane preserves its already verified
    # revision when the logical inputs are identical; unrelated new heads do
    # not cause a desired-state churn.
    if prior_source is not None and prior_source.inputHash == desired.inputHash:
        desired = DesiredSource(
            path=desired.path,
            inputs=desired.inputs,
            revision=prior_source.revision,
            driverVersion=desired.driverVersion,
            inputHash=desired.inputHash,
        )
    return desired, selection.plane


def _previous_unit_source(unit: UnitResource[Any], previous: ProjectedDocument | None) -> DesiredSource | None:
    if previous is None:
        return None
    source = cast(Mapping[str, object], previous.document.get("spec", {})).get("source")
    if not isinstance(source, Mapping):
        return None
    try:
        return DesiredSource(
            path=cast(str, source["path"]),
            revision=cast(str, source["revision"]),
            driverVersion=cast(int | None, source.get("driverVersion")),
            inputHash=cast(str | None, source.get("inputHash")),
            inputs=cast(list[str] | None, source.get("inputs")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionCompilerError(f"previous desired Unit {unit.name!r} has invalid source state") from exc


def _primary_source_plane(
    context: ApplyProjectionContext, retained_sources: tuple[RetainedSourcePlane, ...]
) -> RetainedSourcePlane | None:
    descriptor = context.primary_source
    if descriptor is None:
        return None
    planes = [plane for plane in retained_sources if descriptor in plane.descriptors]
    if len(planes) != 1:
        raise ProjectionCompilerError("primary source has no exact recovered workspace")
    return planes[0]


def _safe_relative(value: str, description: str) -> str:
    if not value or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/") if part != "."):
        if value == ".":
            return value
        raise ProjectionCompilerError(f"{description} must stay inside its source revision")
    return value


def _workspace_input_matches(
    entries: tuple[WorkspaceEntry, ...], root_prefix: str, pattern: str
) -> dict[str, WorkspaceEntry]:
    """Mirror legacy file/directory/glob selection over logical entries."""

    glob = any(character in pattern for character in "*?[")
    matches: dict[str, WorkspaceEntry] = {}
    if pattern == ".":
        return {
            entry.key.removeprefix(root_prefix): entry
            for entry in entries
            if entry.kind in {WorkspaceEntryKind.FILE, WorkspaceEntryKind.SYMLINK} and entry.key.startswith(root_prefix)
        }
    candidate_prefix = f"{root_prefix}{pattern}".rstrip("/")
    for entry in entries:
        if entry.kind not in {WorkspaceEntryKind.FILE, WorkspaceEntryKind.SYMLINK}:
            continue
        if not entry.key.startswith(root_prefix):
            continue
        relative = entry.key.removeprefix(root_prefix)
        matched = (
            _glob_matches(relative, pattern)
            if glob
            else (entry.key == candidate_prefix or entry.key.startswith(f"{candidate_prefix}/"))
        )
        if matched:
            matches[relative] = entry
    return matches


def _glob_matches(path: str, pattern: str) -> bool:
    """Match logical paths with the slash semantics of ``Path.glob``."""

    parts = tuple(part for part in path.split("/") if part)
    pattern_parts = tuple(part for part in pattern.split("/") if part)

    def visit(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return any(visit(next_index, pattern_index + 1) for next_index in range(path_index, len(parts) + 1))
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], part)
            and visit(path_index + 1, pattern_index + 1)
        )

    return visit(0, 0)


def _workspace_input_entry(name: str, entry: WorkspaceEntry) -> dict[str, str]:
    if entry.kind is WorkspaceEntryKind.SYMLINK:
        assert entry.target is not None
        content = entry.target.encode()
        mode = "120000"
    else:
        assert entry.content is not None
        content = entry.content
        mode = "100755" if entry.executable else "100644"
    return {"path": name, "mode": mode, "contentHash": hashlib.sha256(content).hexdigest()}


def _current_stacks(
    catalog: ResourceCatalog, current: Mapping[tuple[str, str, str], ProjectedDocument]
) -> tuple[dict[str, StackResource], dict[str, StackResource]]:
    templates: dict[str, StackResource] = {}
    stacks: dict[str, StackResource] = {}
    for identity, projected in current.items():
        if identity[:2] == (CORE_API_VERSION, "StackTemplate"):
            parsed = _catalog(catalog.parse_stack_template, projected.mutable_document(), "desired")
            assert isinstance(parsed, StackResource)
            templates[parsed.name] = parsed
        elif identity[:2] == (CORE_API_VERSION, "Stack"):
            parsed = _catalog(catalog.parse_stack, projected.mutable_document(), "desired")
            assert isinstance(parsed, StackResource)
            stacks[parsed.name] = parsed
    return templates, stacks


def _promoted_template(
    catalog: ResourceCatalog,
    descriptor: PromotionSourceDescriptor,
    requested_stack: str,
) -> tuple[StackResource, StackResource, bytes]:
    """Recover one source Stack/Template solely from its issued desired plane."""

    stack: StackResource | None = None
    templates: dict[str, tuple[StackResource, bytes]] = {}
    for entry in descriptor.source_desired.workspace.list_entries():
        if not _canonical_stack_root_key(entry.key):
            continue
        raw = descriptor.source_desired.workspace.read(entry.key)
        document = _decode_source_document(raw, entry.key)
        if document.get("kind") == "Stack":
            parsed = _catalog(catalog.parse_stack, document, "desired")
            assert isinstance(parsed, StackResource)
            if parsed.name == requested_stack:
                stack = parsed
                if not isinstance(parsed.spec, DesiredStackSpec):
                    raise ProjectionCompilerError("promotion source Stack is not desired state")
        elif document.get("kind") == "StackTemplate":
            parsed = _catalog(catalog.parse_stack_template, document, "desired")
            assert isinstance(parsed, StackResource)
            templates[parsed.name] = (parsed, raw)
    if stack is None:
        raise ProjectionCompilerError(f"promotion source has no desired Stack {requested_stack!r}")
    assert isinstance(stack.spec, DesiredStackSpec)
    if stack.metadata.deletion is not None or stack.spec.activeProjection is None:
        raise ProjectionCompilerError(f"promotion source Stack {requested_stack!r} is inactive or deleting")
    try:
        template, template_raw = templates[stack.spec.templateRef.name]
    except KeyError as exc:
        raise ProjectionCompilerError(
            f"promotion source Stack has no desired StackTemplate {stack.spec.templateRef.name!r}"
        ) from exc
    if not isinstance(template.spec, DesiredStackTemplateSpec) or template.metadata.uid is None:
        raise ProjectionCompilerError("promotion source StackTemplate is not desired state")
    if template.metadata.deletion is not None:
        raise ProjectionCompilerError(f"promotion source StackTemplate {template.name!r} is deleting")
    assert template_raw is not None
    return stack, template, template_raw


def _canonical_stack_root_key(key: str) -> bool:
    """Accept only one root document below stacks/ or stack-templates/."""

    parts = key.split("/")
    return (
        len(parts) == 2
        and parts[0] in {"stacks", "stack-templates"}
        and parts[1].endswith((".json", ".yaml", ".yml"))
        and bool(parts[1].rsplit(".", 1)[0])
    )


def _canonical_plane_entry(workspace: ImmutableWorkspace, stem: str) -> WorkspaceEntry:
    """Find the one canonical JSON/YAML document for an exact logical stem."""

    candidates = {f"{stem}{suffix}" for suffix in (".json", ".yaml", ".yml")}
    entries = [
        entry for entry in workspace.list_entries() if entry.key in candidates and entry.kind is WorkspaceEntryKind.FILE
    ]
    if not entries:
        raise ProjectionCompilerError(f"promotion source is missing canonical resource {stem!r}")
    if len(entries) != 1:
        raise ProjectionCompilerError(
            f"promotion source has ambiguous document formats for {stem!r}: "
            + ", ".join(sorted(entry.key for entry in entries))
        )
    return entries[0]


def _plane_stack(catalog: ResourceCatalog, workspace: ImmutableWorkspace, name: str) -> StackResource:
    entry = _canonical_plane_entry(workspace, f"stacks/{name}")
    parsed = _catalog(catalog.parse_stack, _decode_source_document(entry.content or b"", entry.key), "desired")
    assert isinstance(parsed, StackResource)
    return parsed


def _plane_stack_template(catalog: ResourceCatalog, workspace: ImmutableWorkspace, name: str) -> StackResource:
    entry = _canonical_plane_entry(workspace, f"stack-templates/{name}")
    parsed = _catalog(catalog.parse_stack_template, _decode_source_document(entry.content or b"", entry.key), "desired")
    assert isinstance(parsed, StackResource)
    return parsed


def _canonical_resource_key(key: str) -> bool:
    """Return whether a key can be one canonical resource document location."""

    parts = key.split("/")
    if len(parts) == 2 and parts[0] in {"units", "stacks", "stack-templates"}:
        return bool(parts[1].rsplit(".", 1)[0]) and parts[1].endswith((".json", ".yaml", ".yml"))
    return (
        len(parts) == 3
        and parts[0] == "units"
        and all(bool(part) for part in parts[1:])
        and parts[2].endswith((".json", ".yaml", ".yml"))
        and bool(parts[2].rsplit(".", 1)[0])
    )


def _validate_promotion_lineage(lineage: PromotionLineage, descriptor: PromotionSourceDescriptor) -> None:
    descriptor._validate()
    if not isinstance(lineage, PromotionLineage):
        raise ProjectionCompilerError("PromotionLineageEncoder must return a PromotionLineage")
    if (
        lineage.source_environment != descriptor.source_environment
        or lineage.lineage_evidence != descriptor.lineage_evidence
    ):
        raise ProjectionCompilerError("promotion lineage encoder returned evidence for a different issued source")
    fields = (
        lineage.source_desired_ref,
        lineage.source_desired_revision,
        lineage.source_observed_ref,
        lineage.source_observed_revision,
        lineage.target_desired_ref,
        lineage.target_desired_revision,
        lineage.target_observed_ref,
        lineage.target_observed_revision,
    )
    if not all(isinstance(value, str) and value for value in fields):
        raise ProjectionCompilerError("promotion lineage encoder returned incomplete legacy evidence")


def _retained_template_source(
    sources: tuple[RetainedSourcePlane, ...], context: ApplyProjectionContext, binding_key: str, path: str
) -> tuple[RetainedSourcePlane, RetainedSourceDescriptor]:
    """Find the explicitly named StackTemplate source by role and path."""

    matches = [
        (plane, descriptor)
        for plane in sources
        for descriptor in plane.descriptors
        if (
            descriptor in context.named_sources
            and descriptor.role is SourceBindingRole.STACK_TEMPLATE
            and descriptor.binding_key == binding_key
            and descriptor.workspace_key == path
        )
    ]
    if len(matches) != 1:
        raise ProjectionCompilerError("StackTemplate Git source requires one exact retained source descriptor")
    return matches[0]


def _same_local_repository(left: str, right: str) -> bool:
    """Compare an absolute POSIX path with its canonical file-URI spelling."""

    from urllib.parse import unquote, urlsplit

    left_uri = urlsplit(left)
    right_uri = urlsplit(right)
    if left_uri.scheme == "file" and left_uri.netloc in {"", "localhost"}:
        return right_uri.scheme == "" and right.startswith("/") and unquote(left_uri.path) == right
    if right_uri.scheme == "file" and right_uri.netloc in {"", "localhost"}:
        return left_uri.scheme == "" and left.startswith("/") and unquote(right_uri.path) == left
    return False


def _inherited_source_context(
    inline: StackTemplateInlineSpec,
    sources: tuple[RetainedSourcePlane, ...],
    context: ApplyProjectionContext,
    encoder: SourceLineageEncoder | None,
) -> StackTemplateSourceContext | None:
    if not _has_repository_source(inline):
        return None
    descriptor = context.primary_source
    if descriptor is None or descriptor.role is not SourceBindingRole.PRIMARY_AUTHORED:
        raise ProjectionCompilerError(
            "repository-backed inline StackTemplate requires an explicit primary retained source"
        )
    matches = [plane for plane in sources if descriptor in plane.descriptors]
    if len(matches) != 1:
        raise ProjectionCompilerError("primary retained source has no exact recovered source workspace")
    source = _source_lineage(encoder, descriptor, matches[0])
    return StackTemplateSourceContext(repository=source.repository, revision=source.revision)


def _source_lineage(
    encoder: SourceLineageEncoder | None, descriptor: RetainedSourceDescriptor, plane: RetainedSourcePlane
) -> SourceLineage:
    if encoder is None:
        raise ProjectionCompilerError("repository source needs an explicit SourceLineageEncoder")
    descriptor._validate()
    if descriptor not in plane.descriptors:
        raise ProjectionCompilerError("source lineage descriptor is not bound to its exact retained plane")
    try:
        source = encoder.encode(descriptor, plane)
    except (TypeError, ValueError) as exc:
        raise ProjectionCompilerError(str(exc)) from exc
    if not isinstance(source, SourceLineage):
        raise ProjectionCompilerError("SourceLineageEncoder must return SourceLineage")
    try:
        StackTemplateSourceContext(repository=source.repository, revision=source.revision)
    except (TypeError, ValueError) as exc:
        raise ProjectionCompilerError("source lineage encoder returned invalid Git source fields") from exc
    return source


def _has_repository_source(template: StackTemplateInlineSpec) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, Mapping):
            source = value.get("source")
            if isinstance(source, Mapping) and (isinstance(source.get("path"), str) or "fromParameter" in source):
                return True
            return any(visit(item) for item in value.values())
        if isinstance(value, tuple | list):
            return any(visit(item) for item in value)
        return getattr(value, "fromParameter", None) is not None

    return any(visit(item.spec) for item in template.unitTemplates.values())


def _normalized_stack_unit_spec(
    resource: StackTemplateResource, source_context: StackTemplateSourceContext | None
) -> dict[str, Any]:
    """Pin inherited repository-backed Unit sources in the desired projection."""

    raw = dump_template_value(cast(TemplateValue, resource.spec))
    if not isinstance(raw, dict):
        raise ProjectionCompilerError(f"Stack template Unit {resource.name!r} spec must be an object")
    normalized = cast(dict[str, Any], _plain(raw))
    source = normalized.get("source")
    if (
        source_context is not None
        and isinstance(source, dict)
        and isinstance(source.get("path"), str)
        and source.get("revision") is None
    ):
        normalized = dict(normalized)
        normalized["source"] = {**source, "revision": source_context.revision}
    return normalized


def _projection_context_digest(context: ApplyProjectionContext) -> str:
    payload = _projection_context_payload(context)
    return f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"


def _projection_context_entry(context: ApplyProjectionContext) -> WorkspaceEntry:
    digest = _projection_context_digest(context)
    payload = {**_projection_context_payload(context), "digest": digest}
    return WorkspaceEntry.file(f"{_CONTEXT_PREFIX}/{digest.removeprefix('sha256:')}.json", _canonical(payload))


def _projection_context_payload(context: ApplyProjectionContext) -> dict[str, object]:
    frozen = context.projection_context
    if frozen is None:
        raise ProjectionCompilerError("Stack projection requires a frozen Project/Environment context")
    return {
        "schema": 1,
        "kind": "ProjectionContext",
        "environment": str(context.environment_id),
        "projectFile": "gitopsctr.yaml",
        "environmentFile": "environment.yaml",
        "projectDocument": _decode_source_document(frozen.project_document, "gitopsctr.yaml"),
        "environmentDocument": _decode_source_document(frozen.environment_document, "environment.yaml"),
        "projectBytes": base64.b64encode(frozen.project_document).decode(),
        "environmentBytes": base64.b64encode(frozen.environment_document).decode(),
    }


def _owned_uid(stack_name: str, stack_uid: str, name: str) -> str:
    provenance = json.dumps(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "generated",
            "name": name,
            "stack": stack_name,
            "stackUid": stack_uid,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"d1-{hashlib.sha256(f'gitopsctr/desired-uid/v1\0{provenance}'.encode()).hexdigest()[:32]}"


def _obsolete_owned_keys(
    current: Mapping[tuple[str, str, str], ProjectedDocument], stacks: set[str], generated: set[tuple[str, str, str]]
) -> tuple[str, ...]:
    obsolete: list[str] = []
    for identity, item in current.items():
        metadata = cast(Mapping[str, object], item.document["metadata"])
        owners = metadata.get("ownerReferences")
        if not isinstance(owners, tuple | list) or len(owners) != 1 or not isinstance(owners[0], Mapping):
            continue
        owner = owners[0]
        if owner.get("kind") == "Stack" and owner.get("name") in stacks and identity not in generated:
            obsolete.append(item.key)
    return tuple(sorted(obsolete))


def _coalesce_documents(
    writes: list[ProjectedDocument], current: Mapping[tuple[str, str, str], ProjectedDocument]
) -> tuple[ProjectedDocument, ...]:
    result: list[ProjectedDocument] = []
    seen: set[tuple[str, str, str]] = set()
    for item in writes:
        if item.identity in seen:
            raise ProjectionCompilerError(f"projection emitted duplicate resource {item.identity!r}")
        seen.add(item.identity)
        previous = current.get(item.identity)
        result.append(
            previous if previous is not None and _canonical(previous.document) == _canonical(item.document) else item
        )
    return tuple(result)


def _decode_source_document(raw: bytes, key: str) -> JsonObject:
    try:
        if key.endswith(".json"):
            value = json.loads(raw)
        else:
            import yaml

            value = yaml.safe_load(raw)
    except Exception as exc:
        raise ProjectionCompilerError(f"{key}: retained document cannot be decoded") from exc
    if not isinstance(value, dict):
        raise ProjectionCompilerError(f"{key}: retained document must be an object")
    return cast(JsonObject, value)


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _json_pointer(value: object, pointer: object) -> Any:
    if not isinstance(pointer, str):
        raise ReferenceUnavailable("reference pointer must be a string")
    if pointer == "":
        return _plain(value)
    if not pointer.startswith("/"):
        raise ReferenceUnavailable(f"reference pointer {pointer!r} is invalid")
    current = value
    for raw_part in pointer.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ReferenceUnavailable(f"reference pointer {pointer!r} is unavailable")
    return _plain(current)


def _document_digest(document: JsonObject) -> str:
    return f"sha256:{hashlib.sha256(_canonical(document)).hexdigest()}"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default).encode()


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value
