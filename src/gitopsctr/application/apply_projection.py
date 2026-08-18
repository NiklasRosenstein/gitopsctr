"""Pure desired-state projection for an explicit apply operation.

This is deliberately the semantic half of apply, rather than an adapter for a
particular repository.  It accepts only decoded authored documents and logical
workspaces, returns an unsealed mutable workspace, and never selects a ref,
reads a clock, creates a UUID, or publishes anything.  The publication adapter
is consequently able to seal and fence this exact candidate independently.

Stack expansion is a separate pure capability.  The application core owns the
transactional rules around Stack and StackTemplate roots, while a supplied
``StackProjectionCompiler`` owns the already substantial structural expansion
algorithm.  Keeping that compiler explicit avoids making a local checkout or
the legacy controller an accidental application dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast, runtime_checkable

import yaml

from gitopsctr.application.apply import AuthoredChangeSet
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    RetainedSource,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.workspace import (
    ImmutableWorkspace,
    InMemoryWorkspace,
    MutableWorkspace,
    WorkspaceEntry,
    WorkspaceEntryKind,
    WorkspaceEntryNotFoundError,
    entry_content_id,
    validate_workspace_key,
)
from gitopsctr.resource_api import JsonObject

_PARTITION_LABEL = "gitopsctr.io/partition"
_ROOT_DIRECTORIES = frozenset(("units", "stacks", "stack-templates"))
_DOCUMENT_SUFFIXES = frozenset((".json", ".yaml", ".yml"))
_RESOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*")


class ApplyProjectionError(ValueError):
    """Raised when an authored change cannot form a closed desired candidate."""


class ApplyPublicationDecision(StrEnum):
    """The only outcomes available before a publication adapter is invoked."""

    DRY_RUN = "dry-run"
    NO_CHANGE = "no-change"
    DIRECT = "direct"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ApplyProjectionPolicy:
    """Explicit policy and identity inputs for one pure projection.

    Root identity provenance is derived from the exact canonical authored
    document.  That is the same explicit input used by the legacy direct-apply
    path, with no process-global UUID or wall clock.
    """

    review_required: bool = False
    require_retained_source: bool = True

    def __post_init__(self) -> None:
        if type(self.review_required) is not bool or type(self.require_retained_source) is not bool:
            raise TypeError("projection policy flags must be bool")


@dataclass(frozen=True, slots=True)
class FinalizedTombstone:
    """A finalized same-name incarnation that a new root must never reuse."""

    api_version: str
    kind: str
    qualified_name: str
    uid: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _canonical_identity_text(self.api_version, "tombstone apiVersion")
        _canonical_identity_text(self.kind, "tombstone kind")
        _resource_name(self.qualified_name, "tombstone qualified name")
        if not isinstance(self.uid, str) or not self.uid or self.uid != self.uid.strip() or "\x00" in self.uid:
            raise ApplyProjectionError("tombstone UID must be non-empty canonical text")


@dataclass(frozen=True, slots=True)
class RootIdentityRequest:
    """All durable evidence an issuer must bind to a new desired root UID."""

    environment_id: EnvironmentId
    api_version: str
    kind: str
    qualified_name: str
    source_snapshot_id: SourceSnapshotId | None
    authored_content_id: ContentId
    finalized_tombstone_uids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "finalized_tombstone_uids", tuple(sorted(self.finalized_tombstone_uids)))
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.environment_id, EnvironmentId):
            raise TypeError("root identity environment_id must be an EnvironmentId")
        _canonical_identity_text(self.api_version, "root identity apiVersion")
        _canonical_identity_text(self.kind, "root identity kind")
        _resource_name(self.qualified_name, "root identity qualified name")
        if self.source_snapshot_id is not None and not isinstance(self.source_snapshot_id, SourceSnapshotId):
            raise TypeError("root identity source_snapshot_id must be a SourceSnapshotId or None")
        if not isinstance(self.authored_content_id, ContentId):
            raise TypeError("root identity authored_content_id must be a ContentId")
        if not isinstance(self.finalized_tombstone_uids, tuple) or any(
            not isinstance(uid, str) or not uid or uid != uid.strip() or "\x00" in uid
            for uid in self.finalized_tombstone_uids
        ):
            raise TypeError("root identity tombstone UIDs must be canonical strings")
        if len(set(self.finalized_tombstone_uids)) != len(self.finalized_tombstone_uids):
            raise ApplyProjectionError("root identity tombstone UIDs must be unique")


_ROOT_IDENTITY_ISSUANCE = object()
_ROOT_IDENTITY_BINDINGS: dict[int, tuple[object, ...]] = {}


def _root_identity_binding(request: RootIdentityRequest, issuer_id: str, uid: str) -> tuple[object, ...]:
    return (
        request.environment_id,
        request.api_version,
        request.kind,
        request.qualified_name,
        request.source_snapshot_id,
        request.authored_content_id,
        request.finalized_tombstone_uids,
        issuer_id,
        uid,
    )


@dataclass(frozen=True, slots=True, init=False)
class IssuedRootIdentity:
    """An adapter-issued root UID whose complete evidence remains inspectable."""

    request: RootIdentityRequest
    issuer_id: str
    uid: str
    _issuance: object

    def _validate(self) -> None:
        if self._issuance is not _ROOT_IDENTITY_ISSUANCE:
            raise TypeError("root identity must be issued by a trusted RootIncarnationIssuer")
        self.request._validate()
        if not isinstance(self.issuer_id, str) or not self.issuer_id or self.issuer_id != self.issuer_id.strip():
            raise ApplyProjectionError("root identity issuer ID must be canonical text")
        if not isinstance(self.uid, str) or not self.uid or self.uid != self.uid.strip() or "\x00" in self.uid:
            raise ApplyProjectionError("issued root UID must be canonical text")
        if _ROOT_IDENTITY_BINDINGS.get(id(self)) != _root_identity_binding(self.request, self.issuer_id, self.uid):
            raise TypeError("issued root identity was modified after issuance")
        if self.uid in self.request.finalized_tombstone_uids:
            raise ApplyProjectionError("issued root UID reuses a finalized same-name incarnation")


def _issue_root_identity(request: RootIdentityRequest, issuer_id: str, uid: str) -> IssuedRootIdentity:
    """Adapter/test issuance hook; consumers can only validate the opaque result."""

    identity = object.__new__(IssuedRootIdentity)
    object.__setattr__(identity, "request", request)
    object.__setattr__(identity, "issuer_id", issuer_id)
    object.__setattr__(identity, "uid", uid)
    object.__setattr__(identity, "_issuance", _ROOT_IDENTITY_ISSUANCE)
    _ROOT_IDENTITY_BINDINGS[id(identity)] = _root_identity_binding(request, issuer_id, uid)
    identity._validate()
    return identity


@runtime_checkable
class RootIncarnationIssuer(Protocol):
    """Trusted provider for stable, tombstone-fenced root identities."""

    @property
    def issuer_id(self) -> str: ...

    def issue(self, request: RootIdentityRequest) -> IssuedRootIdentity: ...


@dataclass(frozen=True, slots=True)
class HmacRootIncarnationIssuer:
    """Production issuer with an explicit, stable namespace secret.

    Composition supplies both values from durable configuration.  Given the
    same request it is idempotent; including finalized same-name UIDs makes a
    recreated resource a different incarnation without consulting a clock or
    generating a UUID.
    """

    issuer_id: str
    identity_seed: str

    def __post_init__(self) -> None:
        _canonical_identity_text(self.issuer_id, "root identity issuer ID")
        _canonical_identity_text(self.identity_seed, "root identity seed")

    def issue(self, request: RootIdentityRequest) -> IssuedRootIdentity:
        request._validate()
        evidence = "\0".join(
            (
                request.environment_id.value,
                request.api_version,
                request.kind,
                request.qualified_name,
                request.source_snapshot_id.to_wire() if request.source_snapshot_id else "",
                request.authored_content_id.value,
                *request.finalized_tombstone_uids,
            )
        ).encode()
        for attempt in range(1024):
            suffix = attempt.to_bytes(2, "big")
            uid = f"d1-{hmac.new(self.identity_seed.encode(), evidence + suffix, hashlib.sha256).hexdigest()[:32]}"
            if uid not in request.finalized_tombstone_uids:
                return _issue_root_identity(request, self.issuer_id, uid)
        raise ApplyProjectionError("root identity issuer could not avoid finalized UID reuse")


@dataclass(frozen=True, slots=True)
class ExactPlane:
    """One head observation transitively bound to exact logical content.

    An absent head is represented only by an empty, snapshot-less workspace.
    A present head is represented only by its matching immutable SnapshotView.
    This prevents a caller from pairing a correct head fence with unrelated
    bytes from a different snapshot.
    """

    head: HeadObservation
    workspace: ImmutableWorkspace
    snapshot: SnapshotView | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.head, HeadObservation):
            raise TypeError("head must be a HeadObservation")
        self.head._validate()
        _immutable(self.workspace, "plane workspace")
        if self.head.snapshot_id is None:
            if self.snapshot is not None or self.workspace.list_entries():
                raise ApplyProjectionError("an absent head requires an empty snapshot-less workspace")
            return
        if not isinstance(self.snapshot, SnapshotView):
            raise ApplyProjectionError("a present head requires its exact SnapshotView")
        if (
            self.snapshot.snapshot_id != self.head.snapshot_id
            or self.snapshot.workspace.content_id != self.workspace.content_id
        ):
            raise ApplyProjectionError("plane head, snapshot, and workspace must identify the same content")
        if self.snapshot.workspace.list_entries() != self.workspace.list_entries():
            raise ApplyProjectionError("plane workspace must be the exact snapshot workspace")

    @property
    def content_id(self) -> ContentId:
        return self.workspace.content_id


@dataclass(frozen=True, slots=True)
class RetainedSourcePlane:
    """An issued retained source and the exact source workspace recovered from it."""

    retained: RetainedSource
    plane: ExactPlane
    descriptors: tuple[RetainedSourceDescriptor, ...] = ()

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.retained, RetainedSource):
            raise TypeError("retained must be an issued RetainedSource")
        self.retained._validate()
        if not isinstance(self.plane, ExactPlane):
            raise TypeError("plane must be an ExactPlane")
        if self.plane.head.snapshot_id != self.retained.source_snapshot_id.snapshot_id:
            raise ApplyProjectionError("retained source plane must bind its exact source snapshot")
        if self.plane.content_id != self.retained.content_id:
            raise ApplyProjectionError("retained source plane content does not match retention evidence")
        if not isinstance(self.descriptors, tuple) or any(
            not isinstance(item, RetainedSourceDescriptor) for item in self.descriptors
        ):
            raise TypeError("retained source descriptors must be a tuple of issued values")
        for descriptor in self.descriptors:
            descriptor._validate()
            if descriptor.retained != self.retained:
                raise ApplyProjectionError("retained source descriptor belongs to a different retained source")


_RETAINED_SOURCE_DESCRIPTOR_ISSUANCE = object()
_PROMOTION_SOURCE_DESCRIPTOR_ISSUANCE = object()
_PROMOTION_DESCRIPTOR_BINDINGS: dict[int, tuple[object, ...]] = {}
_RETAINED_SOURCE_DESCRIPTOR_BINDINGS: dict[int, tuple[object, ...]] = {}


class SourceBindingRole(StrEnum):
    """The explicit role by which a pure projection may consume retained source."""

    PRIMARY_AUTHORED = "primary-authored"
    STACK_TEMPLATE = "stack-template"
    WORKLOAD = "workload"
    PROMOTION = "promotion"


def _binding_key(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ApplyProjectionError("source binding key must be non-empty canonical text")
    return value


@dataclass(frozen=True, slots=True, init=False)
class RetainedSourceDescriptor:
    """Adapter-issued transport/path provenance for one retained source workspace."""

    retained: RetainedSource
    source_id: SourceId
    binding_key: str
    role: SourceBindingRole
    workspace_key: str
    selector_evidence: ContentId
    _issuance: object

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("RetainedSourceDescriptor must be issued by a source adapter")

    def _validate(self) -> None:
        if type(self) is not RetainedSourceDescriptor or self._issuance is not _RETAINED_SOURCE_DESCRIPTOR_ISSUANCE:
            raise TypeError("retained source descriptor has no valid adapter issuance proof")
        self.retained._validate()
        if not isinstance(self.source_id, SourceId) or self.source_id != self.retained.source_snapshot_id.source_id:
            raise ApplyProjectionError("retained source descriptor must bind its retained SourceId")
        _binding_key(self.binding_key)
        if not isinstance(self.role, SourceBindingRole):
            raise TypeError("retained source role must be a SourceBindingRole")
        # ``.`` is the canonical selector for the retained workspace root;
        # every non-root selector remains an exact safe logical key.
        if self.workspace_key != ".":
            validate_workspace_key(self.workspace_key)
        if not isinstance(self.selector_evidence, ContentId):
            raise TypeError("retained source selector evidence must be a ContentId")
        current = (
            self.retained.handle,
            self.retained.retention_store_id,
            self.retained.source_snapshot_id,
            self.retained.content_id,
            self.source_id,
            self.binding_key,
            self.role,
            self.workspace_key,
            self.selector_evidence,
        )
        if _RETAINED_SOURCE_DESCRIPTOR_BINDINGS.get(id(self)) != current:
            raise TypeError("retained source descriptor binding was modified after issuance")


def _issue_retained_source_descriptor(
    retained: RetainedSource,
    binding_key: str,
    role: SourceBindingRole,
    workspace_key: str,
    selector_evidence: ContentId,
) -> RetainedSourceDescriptor:
    """Issue source provenance at the authenticated source-adapter boundary."""

    descriptor = object.__new__(RetainedSourceDescriptor)
    object.__setattr__(descriptor, "retained", retained)
    object.__setattr__(descriptor, "source_id", retained.source_snapshot_id.source_id)
    object.__setattr__(descriptor, "binding_key", binding_key)
    object.__setattr__(descriptor, "role", role)
    object.__setattr__(descriptor, "workspace_key", workspace_key)
    object.__setattr__(descriptor, "selector_evidence", selector_evidence)
    object.__setattr__(descriptor, "_issuance", _RETAINED_SOURCE_DESCRIPTOR_ISSUANCE)
    _RETAINED_SOURCE_DESCRIPTOR_BINDINGS[id(descriptor)] = (
        retained.handle,
        retained.retention_store_id,
        retained.source_snapshot_id,
        retained.content_id,
        retained.source_snapshot_id.source_id,
        binding_key,
        role,
        workspace_key,
        selector_evidence,
    )
    descriptor._validate()
    return descriptor


@dataclass(frozen=True, slots=True, init=False)
class PromotionSourceDescriptor:
    """Adapter-issued, implementation-neutral evidence for a promotion lineage.

    The descriptor deliberately carries exact planes and their SnapshotViews,
    rather than a Git ref or revision spelling. A Git adapter may translate
    this evidence later, but structural projection consumes only the closed
    environment/snapshot/content binding established here.
    """

    source_environment: EnvironmentId
    target_environment: EnvironmentId
    source_desired: ExactPlane
    source_observed: ExactPlane
    target_desired: ExactPlane
    target_observed: ExactPlane
    lineage_evidence: ContentId
    _issuance: object

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("PromotionSourceDescriptor must be issued by a promotion adapter")

    def _validate(self) -> None:
        if type(self) is not PromotionSourceDescriptor or self._issuance is not _PROMOTION_SOURCE_DESCRIPTOR_ISSUANCE:
            raise TypeError("promotion source descriptor has no valid adapter issuance proof")
        if not isinstance(self.source_environment, EnvironmentId) or not isinstance(
            self.target_environment, EnvironmentId
        ):
            raise TypeError("promotion source and target environments must be EnvironmentId values")
        for plane in (self.source_desired, self.source_observed, self.target_desired, self.target_observed):
            if not isinstance(plane, ExactPlane):
                raise TypeError("promotion lineage planes must be ExactPlane values")
            plane._validate()
        if self.source_desired.snapshot is None or self.target_desired.snapshot is None:
            raise ApplyProjectionError("promotion lineage requires exact source and target desired snapshots")
        if not isinstance(self.lineage_evidence, ContentId):
            raise TypeError("promotion lineage evidence must be a ContentId")
        current = (
            self.source_environment,
            self.target_environment,
            self.source_desired,
            self.source_observed,
            self.target_desired,
            self.target_observed,
            self.lineage_evidence,
        )
        if _PROMOTION_DESCRIPTOR_BINDINGS.get(id(self)) != current:
            raise TypeError("promotion source descriptor binding was modified after issuance")


def _issue_promotion_source_descriptor(
    source_environment: EnvironmentId,
    target_environment: EnvironmentId,
    source_desired: ExactPlane,
    source_observed: ExactPlane,
    target_desired: ExactPlane,
    target_observed: ExactPlane,
    lineage_evidence: ContentId,
) -> PromotionSourceDescriptor:
    """Issue closed promotion evidence at the adapter/authority boundary."""

    descriptor = object.__new__(PromotionSourceDescriptor)
    object.__setattr__(descriptor, "source_environment", source_environment)
    object.__setattr__(descriptor, "target_environment", target_environment)
    object.__setattr__(descriptor, "source_desired", source_desired)
    object.__setattr__(descriptor, "source_observed", source_observed)
    object.__setattr__(descriptor, "target_desired", target_desired)
    object.__setattr__(descriptor, "target_observed", target_observed)
    object.__setattr__(descriptor, "lineage_evidence", lineage_evidence)
    object.__setattr__(descriptor, "_issuance", _PROMOTION_SOURCE_DESCRIPTOR_ISSUANCE)
    _PROMOTION_DESCRIPTOR_BINDINGS[id(descriptor)] = (
        source_environment,
        target_environment,
        source_desired,
        source_observed,
        target_desired,
        target_observed,
        lineage_evidence,
    )
    descriptor._validate()
    return descriptor


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionContext:
    """Frozen Project/Environment/promotion inputs required by pure projection."""

    project_document: bytes
    environment_document: bytes
    promotion_desired: ExactPlane | None = None
    promotion_observed: ExactPlane | None = None
    promotion_source: PromotionSourceDescriptor | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.project_document, bytes) or not self.project_document:
            raise ApplyProjectionError("projection context requires exact Project document bytes")
        if not isinstance(self.environment_document, bytes) or not self.environment_document:
            raise ApplyProjectionError("projection context requires exact Environment document bytes")
        for plane in (self.promotion_desired, self.promotion_observed):
            if plane is not None:
                if not isinstance(plane, ExactPlane):
                    raise TypeError("promotion planes must be ExactPlane values")
                plane._validate()
        if self.promotion_source is not None:
            if not isinstance(self.promotion_source, PromotionSourceDescriptor):
                raise TypeError("promotion_source must be an issued PromotionSourceDescriptor or None")
            self.promotion_source._validate()


@dataclass(frozen=True, slots=True)
class ApplyProjectionContext:
    """Non-storage values that bind a plan to one environment and channels."""

    environment_id: EnvironmentId
    desired_channel: ChannelId
    observed_channel: ChannelId | None
    candidate_channel: ChannelId | None
    policy: ApplyProjectionPolicy
    partition: str | None = None
    dry_run: bool = False
    projection_context: WorkspaceProjectionContext | None = None
    primary_source: RetainedSourceDescriptor | None = None
    named_sources: tuple[RetainedSourceDescriptor, ...] = ()
    root_identity_issuer: RootIncarnationIssuer | None = None
    finalized_tombstones: tuple[FinalizedTombstone, ...] = ()

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.environment_id, EnvironmentId):
            raise TypeError("environment_id must be an EnvironmentId")
        if not isinstance(self.desired_channel, ChannelId):
            raise TypeError("desired_channel must be a ChannelId")
        if self.observed_channel is not None and not isinstance(self.observed_channel, ChannelId):
            raise TypeError("observed_channel must be a ChannelId or None")
        if self.candidate_channel is not None and not isinstance(self.candidate_channel, ChannelId):
            raise TypeError("candidate_channel must be a ChannelId or None")
        if not isinstance(self.policy, ApplyProjectionPolicy):
            raise TypeError("policy must be an ApplyProjectionPolicy")
        if self.partition is not None:
            _resource_name(self.partition, "partition")
        if type(self.dry_run) is not bool:
            raise TypeError("dry_run must be bool")
        if self.projection_context is not None:
            if not isinstance(self.projection_context, WorkspaceProjectionContext):
                raise TypeError("projection_context must be a WorkspaceProjectionContext or None")
            self.projection_context._validate()
            if (
                self.projection_context.promotion_source is not None
                and self.projection_context.promotion_source.target_environment != self.environment_id
            ):
                raise ApplyProjectionError("promotion lineage target environment must match the apply environment")
        if self.primary_source is not None:
            if not isinstance(self.primary_source, RetainedSourceDescriptor):
                raise TypeError("primary_source must be an issued RetainedSourceDescriptor or None")
            self.primary_source._validate()
            if self.primary_source.role is not SourceBindingRole.PRIMARY_AUTHORED:
                raise ApplyProjectionError("primary_source must use the primary-authored source role")
        if not isinstance(self.named_sources, tuple) or any(
            not isinstance(source, RetainedSourceDescriptor) for source in self.named_sources
        ):
            raise TypeError("named_sources must be a tuple of issued retained source descriptors")
        for source in self.named_sources:
            source._validate()
        # A binding name identifies a workload/template policy, not one
        # immutable source version.  Historical and current retained planes
        # can therefore legitimately share it.  They must nevertheless be
        # distinguishable by immutable source evidence: two descriptors for
        # the same source snapshot would let a selector choose arbitrarily.
        evidence = {
            (
                source.binding_key,
                source.retained.retention_store_id,
                source.retained.source_snapshot_id,
                source.retained.content_id,
            )
            for source in self.named_sources
        }
        if len(evidence) != len(self.named_sources):
            raise ApplyProjectionError("named source descriptors must not repeat exact retained evidence")
        snapshots = {(source.binding_key, source.retained.source_snapshot_id) for source in self.named_sources}
        if len(snapshots) != len(self.named_sources):
            raise ApplyProjectionError("named source binding has ambiguous retained source snapshot evidence")
        if self.root_identity_issuer is not None:
            if not isinstance(self.root_identity_issuer, RootIncarnationIssuer):
                raise TypeError("root_identity_issuer must implement RootIncarnationIssuer")
            _canonical_identity_text(self.root_identity_issuer.issuer_id, "root identity issuer ID")
        if not isinstance(self.finalized_tombstones, tuple) or any(
            not isinstance(tombstone, FinalizedTombstone) for tombstone in self.finalized_tombstones
        ):
            raise TypeError("finalized_tombstones must be a tuple of FinalizedTombstone values")
        for tombstone in self.finalized_tombstones:
            tombstone._validate()
        tombstone_keys = {
            (tombstone.api_version, tombstone.kind, tombstone.qualified_name, tombstone.uid)
            for tombstone in self.finalized_tombstones
        }
        if len(tombstone_keys) != len(self.finalized_tombstones):
            raise ApplyProjectionError("finalized tombstones must be unique")


@dataclass(frozen=True, slots=True)
class ProjectedDocument:
    """One desired document emitted by a pure Stack projection compiler."""

    key: str
    document: JsonObject

    def __post_init__(self) -> None:
        self._validate()
        object.__setattr__(self, "document", _frozen_json_object(self.document))

    def _validate(self) -> None:
        _candidate_key(self.key)
        _validate_resource_document(self.document, self.key)
        if self.key.rsplit(".", 1)[0] != _key_for(self.document).rsplit(".", 1)[0]:
            raise ApplyProjectionError("projected document key must be the canonical GVK/name location")

    def mutable_document(self) -> JsonObject:
        """Return a fresh document for a compiler or candidate writer."""

        return _copy_json_object(self.document)

    @property
    def identity(self) -> tuple[str, str, str]:
        """Storage-qualified resource identity, including a Stack owner path."""

        return _storage_identity(self.document, self.key)


@dataclass(frozen=True, slots=True)
class FrozenAuthoredDocument:
    """A compiler-facing deep-frozen authored document with its exact content ID."""

    origin: str
    content_id: ContentId
    _wire: bytes

    @classmethod
    def from_change(cls, origin: str, content_id: ContentId, document: JsonObject) -> FrozenAuthoredDocument:
        return cls(origin, content_id, _canonical_json(document))

    @property
    def document(self) -> JsonObject:
        return _frozen_json_object(cast(JsonObject, json.loads(self._wire)))


@runtime_checkable
class StackProjectionCompiler(Protocol):
    """Pure structural Stack expansion capability.

    The compiler receives frozen authored values and current desired documents;
    it must return the desired Stack/StackTemplate roots and any owned Units as
    normal logical documents.  It cannot access a filesystem or publication
    channel through this contract.
    """

    def project(
        self,
        documents: tuple[FrozenAuthoredDocument, ...],
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> CandidateTransformation: ...


@runtime_checkable
class ApplyDocumentValidator(Protocol):
    """Authoritative catalog/driver validator injected by the composition root."""

    def validate_authored(self, document: JsonObject) -> None: ...

    def validate_desired(self, document: JsonObject) -> None: ...

    def validate_graph(self, documents: Mapping[tuple[str, str, str], ProjectedDocument]) -> None: ...

    def validate_workspace(self, workspace: ImmutableWorkspace) -> None: ...


@runtime_checkable
class UnitProjectionCompiler(Protocol):
    """Authoritative Unit/driver projection capability.

    Production composition supplies a driver-aware implementation.  The core
    does not treat an authored Unit as a desired document merely by copying
    it; the compiler must issue the desired root and any materialized payload
    delta explicitly.
    """

    def project(
        self,
        documents: tuple[FrozenAuthoredDocument, ...],
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> CandidateTransformation: ...


_PAYLOAD_REPLACEMENT_BINDINGS: dict[int, tuple[object, ...]] = {}


@dataclass(frozen=True, slots=True)
class PayloadPrefixReplacement:
    """An atomic, exact-content-fenced replacement of one payload subtree.

    The compiler derives the expected subtree evidence from the immutable
    current desired workspace it receives.  The core rechecks that evidence
    immediately before recursively pruning the prefix, so stale or corrupt
    materializations cannot be silently reused or removed.
    """

    prefix: str
    expected_current_content_id: ContentId
    expected_current_entries: tuple[tuple[str, ContentId], ...]
    entries: tuple[WorkspaceEntry, ...] = ()

    def __post_init__(self) -> None:
        _PAYLOAD_REPLACEMENT_BINDINGS[id(self)] = (
            self.prefix,
            self.expected_current_content_id,
            self.expected_current_entries,
            self.entries,
        )
        self._validate()

    def _validate(self) -> None:
        if _PAYLOAD_REPLACEMENT_BINDINGS.get(id(self)) != (
            self.prefix,
            self.expected_current_content_id,
            self.expected_current_entries,
            self.entries,
        ):
            raise TypeError("payload replacement was modified after construction")
        validate_workspace_key(self.prefix)
        if not isinstance(self.expected_current_content_id, ContentId):
            raise TypeError("payload replacement expected_current_content_id must be a ContentId")
        if not isinstance(self.expected_current_entries, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], ContentId)
            for item in self.expected_current_entries
        ):
            raise TypeError("payload replacement expected_current_entries must be (key, ContentId) tuples")
        expected_entries = self.expected_current_entries
        if tuple(sorted(expected_entries)) != expected_entries or len(
            {key for key, _content_id in expected_entries}
        ) != len(expected_entries):
            raise ApplyProjectionError("payload replacement expected entries must be sorted and unique")
        if any(key != self.prefix and not key.startswith(f"{self.prefix}/") for key, _content_id in expected_entries):
            raise ApplyProjectionError("payload replacement expected entries must remain below its prefix")
        if not isinstance(self.entries, tuple) or any(not isinstance(entry, WorkspaceEntry) for entry in self.entries):
            raise TypeError("payload replacement entries must be WorkspaceEntry values")
        if len({entry.key for entry in self.entries}) != len(self.entries):
            raise ApplyProjectionError("payload replacement entries cannot repeat a key")
        if any(entry.key != self.prefix and not entry.key.startswith(f"{self.prefix}/") for entry in self.entries):
            raise ApplyProjectionError("payload replacement entries must remain below its prefix")


def payload_prefix_evidence(
    workspace: ImmutableWorkspace, prefix: str
) -> tuple[ContentId, tuple[tuple[str, ContentId], ...]]:
    """Return exact subtree content and per-entry evidence for a replacement."""

    _immutable(workspace, "payload evidence workspace")
    validate_workspace_key(prefix)
    return _payload_prefix_evidence_for_workspace(workspace, prefix)


def _payload_prefix_evidence_for_workspace(
    workspace: ImmutableWorkspace, prefix: str
) -> tuple[ContentId, tuple[tuple[str, ContentId], ...]]:
    validate_workspace_key(prefix)
    return _payload_prefix_evidence_from_entries(workspace.list_entries(prefix))


def _payload_prefix_evidence_from_entries(
    entries: tuple[WorkspaceEntry, ...],
) -> tuple[ContentId, tuple[tuple[str, ContentId], ...]]:
    workspace = InMemoryWorkspace(entries, mutable=False)
    return workspace.content_id, tuple((entry.key, entry_content_id(entry)) for entry in entries)


@dataclass(frozen=True, slots=True)
class CandidateTransformation:
    """A pure, scoped delta over a complete logical candidate workspace.

    The candidate starts as an exact copy of desired content, so unrelated
    documents and non-document payloads are preserved by construction. Stack
    projection may write desired documents and explicitly delete obsolete
    owned fan-out documents; all other deletion is rejected by the core.
    """

    writes: tuple[ProjectedDocument, ...]
    deletes: tuple[str, ...] = ()
    payload_writes: tuple[WorkspaceEntry, ...] = ()
    payload_deletes: tuple[str, ...] = ()
    payload_prefixes: tuple[str, ...] = ()
    payload_replacements: tuple[PayloadPrefixReplacement, ...] = ()

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.writes, tuple) or any(not isinstance(item, ProjectedDocument) for item in self.writes):
            raise TypeError("candidate transformation writes must be ProjectedDocument values")
        if not isinstance(self.deletes, tuple) or any(not isinstance(key, str) for key in self.deletes):
            raise TypeError("candidate transformation deletes must be workspace keys")
        if not isinstance(self.payload_writes, tuple) or any(
            not isinstance(item, WorkspaceEntry) for item in self.payload_writes
        ):
            raise TypeError("candidate transformation payload writes must be WorkspaceEntry values")
        if not isinstance(self.payload_deletes, tuple) or any(not isinstance(key, str) for key in self.payload_deletes):
            raise TypeError("candidate transformation payload deletes must be workspace keys")
        if not isinstance(self.payload_prefixes, tuple) or any(
            not isinstance(key, str) for key in self.payload_prefixes
        ):
            raise TypeError("candidate transformation payload prefixes must be workspace keys")
        if not isinstance(self.payload_replacements, tuple) or any(
            not isinstance(item, PayloadPrefixReplacement) for item in self.payload_replacements
        ):
            raise TypeError("candidate transformation payload replacements must be PayloadPrefixReplacement values")
        for prefix in self.payload_prefixes:
            validate_workspace_key(prefix)
        all_write_keys = {item.key for item in self.writes}.union(item.key for item in self.payload_writes)
        all_delete_keys = set(self.deletes).union(self.payload_deletes)
        if len(all_write_keys) != len(self.writes) + len(self.payload_writes) or len(all_delete_keys) != len(
            self.deletes
        ) + len(self.payload_deletes):
            raise ApplyProjectionError("candidate transformation cannot repeat a workspace key")
        if all_write_keys.intersection(all_delete_keys):
            raise ApplyProjectionError("candidate transformation cannot both write and delete a key")
        for key in self.deletes:
            _candidate_key(key)
        for entry in self.payload_writes:
            _validate_payload_scope(entry.key, self.payload_prefixes)
        for key in self.payload_deletes:
            validate_workspace_key(key)
            _validate_payload_scope(key, self.payload_prefixes)
        replacement_prefixes = tuple(item.prefix for item in self.payload_replacements)
        if len(set(replacement_prefixes)) != len(replacement_prefixes):
            raise ApplyProjectionError("payload replacements cannot repeat a prefix")
        for replacement in self.payload_replacements:
            replacement._validate()
            _validate_payload_scope(replacement.prefix, self.payload_prefixes)
        direct_payload_keys = (*all_write_keys, *all_delete_keys)
        for prefix in replacement_prefixes:
            if any(key == prefix or key.startswith(f"{prefix}/") for key in direct_payload_keys):
                raise ApplyProjectionError("payload replacements cannot overlap direct payload deltas")


@dataclass(frozen=True, slots=True)
class CanonicalUnitProjectionCompiler:
    """Reference pure Unit compiler for catalog-validated canonical documents.

    It is deliberately limited to root identity/partition projection. A
    driver-aware composition may replace it to emit materialized payload
    writes, but it remains a real production-safe implementation for Units
    whose validated desired form has no auxiliary workspace content.
    """

    def project(
        self,
        documents: tuple[FrozenAuthoredDocument, ...],
        current_desired: Mapping[tuple[str, str, str], ProjectedDocument],
        current_workspace: ImmutableWorkspace,
        retained_sources: tuple[RetainedSourcePlane, ...],
        observed: ImmutableWorkspace,
        context: ApplyProjectionContext,
    ) -> CandidateTransformation:
        if not documents:
            return CandidateTransformation(())
        return CandidateTransformation(
            tuple(_desired_root(item.document, item.content_id, current_desired, context) for item in documents)
        )


_CANONICAL_UNIT_COMPILER = CanonicalUnitProjectionCompiler()


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """Closed plan facts for a candidate that has not been sealed or published."""

    environment_id: EnvironmentId
    desired_channel: ChannelId
    observed_channel: ChannelId | None
    candidate_channel: ChannelId | None
    desired_plane: ExactPlane
    observed_plane: ExactPlane
    base_content_id: ContentId
    candidate_content_id: ContentId
    authored_source_snapshot: SourceSnapshotId | None
    primary_source: RetainedSourcePlane | None
    retained_sources: tuple[RetainedSourcePlane, ...]
    applied_identities: tuple[tuple[str, str, str], ...]
    decision: ApplyPublicationDecision
    partition: str | None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.environment_id, EnvironmentId):
            raise TypeError("environment_id must be an EnvironmentId")
        if not isinstance(self.desired_channel, ChannelId):
            raise TypeError("desired_channel must be a ChannelId")
        if self.observed_channel is not None and not isinstance(self.observed_channel, ChannelId):
            raise TypeError("observed_channel must be a ChannelId or None")
        if self.candidate_channel is not None and not isinstance(self.candidate_channel, ChannelId):
            raise TypeError("candidate_channel must be a ChannelId or None")
        if not isinstance(self.desired_plane, ExactPlane) or self.desired_plane.head.channel_id != self.desired_channel:
            raise ApplyProjectionError("plan desired plane must bind the desired channel")
        self.desired_plane._validate()
        if not isinstance(self.observed_plane, ExactPlane):
            raise TypeError("observed_plane must be an ExactPlane")
        self.observed_plane._validate()
        if self.observed_channel is None or self.observed_plane.head.channel_id != self.observed_channel:
            raise ApplyProjectionError("plan observed plane must bind the observed channel")
        if not isinstance(self.base_content_id, ContentId) or not isinstance(self.candidate_content_id, ContentId):
            raise TypeError("plan workspace identities must be ContentId values")
        if self.base_content_id != self.desired_plane.content_id:
            raise ApplyProjectionError("plan base content must equal its exact desired plane")
        if self.authored_source_snapshot is not None and not isinstance(
            self.authored_source_snapshot, SourceSnapshotId
        ):
            raise TypeError("authored_source_snapshot must be a SourceSnapshotId or None")
        if self.primary_source is not None and not isinstance(self.primary_source, RetainedSourcePlane):
            raise TypeError("primary_source must be an exact RetainedSourcePlane or None")
        if not isinstance(self.retained_sources, tuple) or any(
            not isinstance(source, RetainedSourcePlane) for source in self.retained_sources
        ):
            raise TypeError("retained_sources must be a tuple of exact retained source planes")
        for source in self.retained_sources:
            source._validate()
        source_keys = {
            (source.retained.retention_store_id, source.retained.source_snapshot_id, source.retained.content_id)
            for source in self.retained_sources
        }
        if len(source_keys) != len(self.retained_sources):
            raise ApplyProjectionError("plan retained sources must have unique store, snapshot, and content identities")
        if self.authored_source_snapshot is None:
            if self.primary_source is not None:
                raise ApplyProjectionError("a non-source-backed plan cannot name a primary retained source")
        else:
            if self.primary_source is None or self.primary_source not in self.retained_sources:
                raise ApplyProjectionError("source-backed plan requires a retained primary source")
            if self.primary_source.retained.source_snapshot_id != self.authored_source_snapshot:
                raise ApplyProjectionError("plan primary source does not match the authored source snapshot")
        if not isinstance(self.decision, ApplyPublicationDecision):
            raise TypeError("decision must be an ApplyPublicationDecision")
        if self.partition is not None:
            _resource_name(self.partition, "partition")
        for identity in self.applied_identities:
            if (
                not isinstance(identity, tuple)
                or len(identity) != 3
                or not all(isinstance(value, str) for value in identity)
            ):
                raise TypeError("applied identities must be (api_version, kind, name) string tuples")
            _resource_name(identity[2], "applied resource name")
        if tuple(sorted(set(self.applied_identities))) != self.applied_identities:
            raise ApplyProjectionError("applied identities must be sorted and duplicate-free")


@dataclass(frozen=True, slots=True)
class ApplyProjectionResult:
    """A deterministic mutable candidate and its non-publishing decision."""

    plan: ApplyPlan
    candidate: MutableWorkspace

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.plan, ApplyPlan):
            raise TypeError("plan must be an ApplyPlan")
        self.plan._validate()
        if not isinstance(self.candidate, MutableWorkspace) or not self.candidate.is_mutable:
            raise TypeError("candidate must implement mutable logical workspace operations")
        if self.candidate.content_id != self.plan.candidate_content_id:
            raise ApplyProjectionError("candidate content does not match its plan")


def project_apply(
    changes: AuthoredChangeSet,
    *,
    current_desired: ExactPlane,
    observed: ExactPlane,
    retained_sources: tuple[RetainedSourcePlane, ...] = (),
    context: ApplyProjectionContext,
    validator: ApplyDocumentValidator,
    unit_compiler: UnitProjectionCompiler = _CANONICAL_UNIT_COMPILER,
    stack_compiler: StackProjectionCompiler | None = None,
) -> ApplyProjectionResult:
    """Transform one exact authored change set into an unsealed candidate.

    All inputs are immutable logical workspaces.  ``observed`` is accepted now
    because applying must resolve its three coherent planes once; this pure
    stage deliberately does not interpret observations, which are reserved for
    the stack/effect compilers.  A source-backed change set is rejected unless
    the supplied retained evidence and source workspace agree exactly.
    """

    if not isinstance(changes, AuthoredChangeSet):
        raise TypeError("changes must be an AuthoredChangeSet")
    if not isinstance(context, ApplyProjectionContext):
        raise TypeError("context must be an ApplyProjectionContext")
    context._validate()
    if not isinstance(current_desired, ExactPlane) or current_desired.head.channel_id != context.desired_channel:
        raise ApplyProjectionError("current_desired must be the exact desired-channel plane")
    current_desired._validate()
    if not isinstance(observed, ExactPlane) or observed.head.channel_id != context.observed_channel:
        raise ApplyProjectionError("observed must be the exact observed-channel plane")
    observed._validate()
    promotion_source = context.projection_context.promotion_source if context.projection_context is not None else None
    if promotion_source is not None:
        promotion_source._validate()
        if not _same_exact_plane(promotion_source.target_desired, current_desired):
            raise ApplyProjectionError("promotion lineage target desired plane does not match the apply desired plane")
        if not _same_exact_plane(promotion_source.target_observed, observed):
            raise ApplyProjectionError(
                "promotion lineage target observed plane does not match the apply observed plane"
            )
    if not isinstance(retained_sources, tuple) or any(
        not isinstance(item, RetainedSourcePlane) for item in retained_sources
    ):
        raise TypeError("retained_sources must be a tuple of RetainedSourcePlane values")
    for source in retained_sources:
        source._validate()
    if not isinstance(validator, ApplyDocumentValidator):
        raise TypeError("validator must implement ApplyDocumentValidator")
    if not isinstance(unit_compiler, UnitProjectionCompiler):
        raise TypeError("unit_compiler must implement UnitProjectionCompiler")
    _validate_source_evidence(changes, retained_sources, context)
    if not changes.documents and context.partition is None:
        raise ApplyProjectionError(
            "apply produced zero documents; specify a partition for authoritative empty membership"
        )

    current_documents = _load_desired_documents(current_desired.workspace, validator)
    _validate_owner_graph(current_documents)
    candidate = (
        current_desired.workspace.mutable_copy()
        if isinstance(current_desired.workspace, InMemoryWorkspace)
        else InMemoryWorkspace(
            current_desired.workspace.list_entries(), capabilities=current_desired.workspace.capabilities, mutable=True
        )
    )
    authored = tuple(document.document for document in changes.documents)
    for document in authored:
        validator.validate_authored(document)
    stack_inputs = tuple(document for document in authored if _kind(document) in {"Stack", "StackTemplate"})
    ordinary_inputs = tuple(document for document in authored if _kind(document) not in {"Stack", "StackTemplate"})
    if stack_inputs and stack_compiler is None:
        raise ApplyProjectionError("Stack and StackTemplate input requires a pure StackProjectionCompiler")

    projected: list[ProjectedDocument] = []
    applied: set[tuple[str, str, str]] = set()
    unit_transformation: CandidateTransformation | None = None
    transformation: CandidateTransformation | None = None
    intermediate_documents = current_documents
    intermediate_workspace = current_desired.workspace
    ordinary_frozen = tuple(
        FrozenAuthoredDocument.from_change(document.origin, document.content_id, document.document)
        for document in changes.documents
        if _kind(document.document) not in {"Stack", "StackTemplate"}
    )
    if ordinary_frozen:
        unit_transformation = unit_compiler.project(
            ordinary_frozen,
            MappingProxyType(current_documents),
            current_desired.workspace,
            tuple(retained_sources),
            observed.workspace,
            context,
        )
        if isinstance(unit_transformation, CandidateTransformation):
            unit_transformation._validate()
        if not isinstance(unit_transformation, CandidateTransformation) or not unit_transformation.writes:
            raise ApplyProjectionError("UnitProjectionCompiler must return a non-empty CandidateTransformation")
        _validate_unit_transformation(unit_transformation, ordinary_inputs)
        for item in unit_transformation.writes:
            projected.append(item)
            applied.add(item.identity)
            existing = current_documents.get(item.identity)
            if existing is None or _canonical_json(existing.document) != _canonical_json(item.document):
                _replace_document(candidate, item)
        _apply_transformation_payload(candidate, unit_transformation)
        intermediate_documents, intermediate_workspace = _validated_intermediate_candidate(candidate, validator)
        if not applied.issubset(intermediate_documents):
            raise ApplyProjectionError("Unit transformation removed a required applied resource identity")
    if stack_inputs:
        assert stack_compiler is not None
        frozen = tuple(
            FrozenAuthoredDocument.from_change(document.origin, document.content_id, document.document)
            for document in changes.documents
            if _kind(document.document) in {"Stack", "StackTemplate"}
        )
        transformation = stack_compiler.project(
            frozen,
            MappingProxyType(intermediate_documents),
            intermediate_workspace,
            tuple(retained_sources),
            observed.workspace,
            context,
        )
        if isinstance(transformation, CandidateTransformation):
            transformation._validate()
        if not isinstance(transformation, CandidateTransformation) or not transformation.writes:
            raise ApplyProjectionError("StackProjectionCompiler must return a non-empty scoped CandidateTransformation")
        _validate_stack_transformation(transformation, intermediate_documents, stack_inputs)
        for item in transformation.writes:
            _assert_compiler_result(item, intermediate_documents, context)
            projected.append(item)
            applied.add(item.identity)

    keys = [item.key for item in projected]
    if len(keys) != len(set(keys)):
        raise ApplyProjectionError("projection produced duplicate candidate paths")
    identities = [item.identity for item in projected]
    if len(identities) != len(set(identities)):
        raise ApplyProjectionError("projection produced duplicate resource identities")
    if transformation is not None:
        for item in transformation.writes:
            existing = intermediate_documents.get(item.identity)
            if existing is None or _canonical_json(existing.document) != _canonical_json(item.document):
                _replace_document(candidate, item)
    transformations = tuple(
        item for item in (unit_transformation, transformation) if isinstance(item, CandidateTransformation)
    )
    _validate_payload_replacement_interactions(transformations)
    if transformation is not None:
        _apply_transformation_payload(candidate, transformation)
        for key in transformation.deletes:
            candidate.delete(key)
    _prune_partition(candidate, current_documents, {_identity(document) for document in authored}, context.partition)
    candidate_documents = _load_desired_documents(candidate, validator)
    if not applied.issubset(candidate_documents):
        raise ApplyProjectionError("transformation removed a required applied resource identity")
    _validate_owner_graph(candidate_documents)
    _validate_stack_template_referrers(candidate_documents)
    validator.validate_graph(MappingProxyType(candidate_documents))
    validation_content_id = candidate.content_id
    validation_workspace = InMemoryWorkspace(
        candidate.list_entries(), capabilities=candidate.capabilities, mutable=False
    )
    validator.validate_workspace(validation_workspace)
    if candidate.content_id != validation_content_id:
        raise ApplyProjectionError("workspace validator mutated the candidate through an invalid alias")

    decision = (
        ApplyPublicationDecision.NO_CHANGE
        if candidate.content_id == current_desired.content_id
        else ApplyPublicationDecision.DRY_RUN
        if context.dry_run
        else ApplyPublicationDecision.REVIEW
        if context.policy.review_required
        else ApplyPublicationDecision.DIRECT
    )
    plan = ApplyPlan(
        context.environment_id,
        context.desired_channel,
        context.observed_channel,
        context.candidate_channel,
        current_desired,
        observed,
        current_desired.content_id,
        candidate.content_id,
        changes.source_snapshot_id,
        next(
            (item for item in retained_sources if item.retained.source_snapshot_id == changes.source_snapshot_id),
            None,
        ),
        retained_sources,
        tuple(sorted(applied)),
        decision,
        context.partition,
    )
    return ApplyProjectionResult(plan, candidate)


def _validated_intermediate_candidate(
    candidate: InMemoryWorkspace,
    validator: ApplyDocumentValidator,
) -> tuple[dict[tuple[str, str, str], ProjectedDocument], ImmutableWorkspace]:
    """Freeze and validate ordinary Unit changes before Stack compilation.

    The detached immutable view is the exact candidate Stack compilation may
    inspect.  In particular, its resource entry bytes define the ContentIds
    used to fence receipts for root Units changed earlier in this operation.
    """

    candidate_content_id = candidate.content_id
    workspace = InMemoryWorkspace(candidate.list_entries(), capabilities=candidate.capabilities, mutable=False)
    documents = _load_desired_documents(workspace, validator)
    _validate_owner_graph(documents)
    _validate_stack_template_referrers(documents)
    validator.validate_graph(MappingProxyType(documents))
    validator.validate_workspace(workspace)
    if candidate.content_id != candidate_content_id:
        raise ApplyProjectionError("workspace validator mutated the intermediate candidate through an invalid alias")
    return documents, workspace


def _immutable(workspace: object, name: str) -> None:
    if not isinstance(workspace, ImmutableWorkspace):
        raise TypeError(f"{name} must implement ImmutableWorkspace")
    if workspace.is_mutable:
        raise ApplyProjectionError(f"{name} must be immutable")


def _same_exact_plane(left: ExactPlane, right: ExactPlane) -> bool:
    """Compare the complete head/snapshot/content binding, not a display label."""

    left._validate()
    right._validate()
    return (
        left.head == right.head
        and left.content_id == right.content_id
        and left.snapshot == right.snapshot
        and left.workspace.list_entries() == right.workspace.list_entries()
    )


def _validate_source_evidence(
    changes: AuthoredChangeSet,
    retained_sources: tuple[RetainedSourcePlane, ...],
    context: ApplyProjectionContext,
) -> None:
    source_by_snapshot = {item.retained.source_snapshot_id: item for item in retained_sources}
    if len(source_by_snapshot) != len(retained_sources):
        raise ApplyProjectionError("retained source planes must not repeat a source snapshot")
    available_descriptors = {descriptor for plane in retained_sources for descriptor in plane.descriptors}
    named = context.named_sources
    if any(descriptor not in available_descriptors for descriptor in named):
        raise ApplyProjectionError("named source descriptor is not bound to an exact retained source plane")
    if changes.source_snapshot_id is None:
        if context.primary_source is not None:
            raise ApplyProjectionError("non-source-backed input cannot select a primary retained source")
        return
    primary = context.primary_source
    if primary is None or primary not in available_descriptors:
        raise ApplyProjectionError("source-backed apply requires an explicit primary issued retained source")
    if primary.retained.source_snapshot_id != changes.source_snapshot_id:
        raise ApplyProjectionError("primary retained source does not match the decoded source snapshot")
    evidence = source_by_snapshot.get(changes.source_snapshot_id)
    if evidence is None or primary not in evidence.descriptors:
        raise ApplyProjectionError("primary retained source has no exact recovered source workspace")
    if context.policy.require_retained_source and evidence.retained.source_snapshot_id != changes.source_snapshot_id:
        raise ApplyProjectionError("retained source evidence does not match the decoded source snapshot")


def _load_desired_documents(
    workspace: ImmutableWorkspace, validator: ApplyDocumentValidator
) -> dict[tuple[str, str, str], ProjectedDocument]:
    documents: dict[tuple[str, str, str], ProjectedDocument] = {}
    for entry in workspace.list_entries():
        top_level = entry.key.split("/", 1)[0]
        if entry.kind is WorkspaceEntryKind.SYMLINK:
            raise ApplyProjectionError(f"desired workspace cannot contain symbolic links: {entry.key!r}")
        if top_level in _ROOT_DIRECTORIES and entry.kind is not WorkspaceEntryKind.FILE:
            raise ApplyProjectionError(f"desired resource entry must be a regular file: {entry.key!r}")
        if top_level in _ROOT_DIRECTORIES and not _is_resource_key(entry.key):
            raise ApplyProjectionError(f"desired resource key is not a canonical document path: {entry.key!r}")
        if entry.kind is not WorkspaceEntryKind.FILE or not _is_resource_key(entry.key):
            continue
        document = _decode_document(workspace.read(entry.key), entry.key)
        validator.validate_desired(document)
        projected = ProjectedDocument(entry.key, document)
        identity = projected.identity
        if identity in documents:
            raise ApplyProjectionError(f"current desired workspace has duplicate resource identity {identity!r}")
        documents[identity] = projected
    return documents


def _decode_document(raw: bytes, key: str) -> JsonObject:
    try:
        loaded = json.loads(raw) if key.endswith(".json") else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ApplyProjectionError(f"{key}: cannot decode desired document") from exc
    if not isinstance(loaded, dict) or not all(isinstance(name, str) for name in loaded):
        raise ApplyProjectionError(f"{key}: desired document must be a JSON object")
    return cast(JsonObject, loaded)


def _desired_root(
    authored: JsonObject,
    authored_content_id: ContentId,
    current: Mapping[tuple[str, str, str], ProjectedDocument],
    context: ApplyProjectionContext,
) -> ProjectedDocument:
    _validate_resource_document(authored, "authored input")
    identity = _identity(authored)
    previous = current.get(identity)
    document = _copy_json_object(authored)
    metadata = cast(JsonObject, document["metadata"])
    authored_labels = _labels(metadata)
    authored_partition = authored_labels.pop(_PARTITION_LABEL, None)
    if authored_partition is not None and authored_partition != context.partition:
        raise ApplyProjectionError(
            f"{identity!r}: authored partition labels are permitted only when the operation selects that partition"
        )
    # Partition ownership is operation authority, never a self-assignment in
    # source input. Preserve non-partition labels but re-add the label only
    # below after the root has passed partition fencing.
    _assign_labels(metadata, authored_labels)
    if _owner_references(metadata) is not None or metadata.get("deletion") is not None:
        raise ApplyProjectionError(f"{identity!r}: explicit apply accepts only non-deleting root resources")
    if previous is not None:
        previous_metadata = cast(JsonObject, previous.document["metadata"])
        if _owner_references(previous_metadata) is not None:
            raise ApplyProjectionError(f"{identity!r}: refusing adoption of an owned desired resource")
        if previous_metadata.get("deletion") is not None:
            raise ApplyProjectionError(f"{identity!r}: desired resource is deleting and cannot be applied")
        previous_partition = _partition(previous_metadata)
        if context.partition is not None and previous_partition not in {None, context.partition}:
            raise ApplyProjectionError(f"{identity!r}: resource belongs to partition {previous_partition!r}")
        uid = previous_metadata.get("uid")
        if not isinstance(uid, str):
            raise ApplyProjectionError(f"{identity!r}: current desired resource has no UID")
        metadata["uid"] = uid
        labels = _labels(metadata)
        if context.partition is None:
            if previous_partition is not None:
                labels[_PARTITION_LABEL] = previous_partition
        else:
            labels[_PARTITION_LABEL] = context.partition
        _assign_labels(metadata, labels)
    else:
        metadata["uid"] = _issue_root_uid(context, identity, authored_content_id)
        labels = _labels(metadata)
        if context.partition is not None:
            labels[_PARTITION_LABEL] = context.partition
        _assign_labels(metadata, labels)
    # A no-op must retain the exact existing logical entry rather than merely
    # serialize an equivalent document with a different extension or bytes.
    # Publication decisions fence logical content identity, not parsed YAML.
    if previous is not None and _canonical_json(document) == _canonical_json(previous.document):
        return previous
    return ProjectedDocument(_key_for(document), document)


def _assert_compiler_result(
    item: ProjectedDocument,
    current: Mapping[tuple[str, str, str], ProjectedDocument],
    context: ApplyProjectionContext,
) -> None:
    if item.key != _key_for(item.document):
        raise ApplyProjectionError("Stack compiler must emit canonical .json GVK/name keys")
    metadata = cast(JsonObject, item.document["metadata"])
    identity = item.identity
    previous = current.get(identity)
    if _owner_references(metadata) is None:
        if metadata.get("deletion") is not None:
            raise ApplyProjectionError(f"{identity!r}: compiler cannot explicitly apply a deleting root")
        previous_partition = _partition(cast(JsonObject, previous.document["metadata"])) if previous else None
        if context.partition is not None and previous_partition not in {None, context.partition}:
            raise ApplyProjectionError(f"{identity!r}: resource belongs to partition {previous_partition!r}")
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ApplyProjectionError(f"{identity!r}: Stack compiler root must have a desired UID")
        if previous is not None:
            previous_uid = cast(JsonObject, previous.document["metadata"]).get("uid")
            if previous_uid != uid:
                raise ApplyProjectionError(f"{identity!r}: Stack compiler cannot change an existing root UID")


def _validate_stack_transformation(
    transformation: CandidateTransformation,
    current: Mapping[tuple[str, str, str], ProjectedDocument],
    inputs: tuple[JsonObject, ...],
) -> None:
    """Keep a Stack compiler within its declared root/owned-resource scope."""

    selected_roots = {_identity(document) for document in inputs}
    selected_template_names = {identity[2] for identity in selected_roots if identity[1] == "StackTemplate"}
    eligible_roots = set(selected_roots)
    for identity, document in current.items():
        if identity[1] != "Stack":
            continue
        metadata = cast(JsonObject, document.document["metadata"])
        if _owner_references(metadata) is not None or metadata.get("deletion") is not None:
            continue
        spec = document.document.get("spec")
        template_ref = spec.get("templateRef") if isinstance(spec, Mapping) else None
        template_name = template_ref.get("name") if isinstance(template_ref, Mapping) else None
        if template_name in selected_template_names:
            eligible_roots.add(identity)
    written_roots: set[tuple[str, str, str]] = set()
    for item in transformation.writes:
        metadata = cast(JsonObject, item.document["metadata"])
        if _owner_references(metadata) is None:
            identity = item.identity
            if identity not in eligible_roots:
                raise ApplyProjectionError("Stack compiler cannot modify an unrelated desired root")
            written_roots.add(identity)
        else:
            owner = _owner_identity(metadata)
            assert owner is not None
            if owner[0] not in eligible_roots:
                raise ApplyProjectionError("Stack compiler cannot modify a child of an unrelated Stack root")
    if not selected_roots.issubset(written_roots):
        raise ApplyProjectionError(
            "Stack compiler must project every selected Stack or StackTemplate root exactly once"
        )
    deleted_documents = tuple(
        target
        for key in transformation.deletes
        if (target := next((item for item in current.values() if item.key == key), None)) is not None
    )
    _validate_payload_ownership(
        transformation,
        eligible_roots,
        payload_documents=(*transformation.writes, *deleted_documents),
        allow_projection_contexts=True,
    )
    for key in transformation.deletes:
        target = next((item for item in current.values() if item.key == key), None)
        if target is None:
            raise ApplyProjectionError(f"Stack compiler cannot delete unknown desired entry {key!r}")
        metadata = cast(JsonObject, target.document["metadata"])
        if _owner_references(metadata) is None:
            raise ApplyProjectionError("Stack compiler can delete only obsolete owned fan-out resources")
        owner = _owner_identity(metadata)
        assert owner is not None
        if owner[0] not in eligible_roots:
            raise ApplyProjectionError("Stack compiler cannot delete a child of an unrelated Stack root")


def _validate_unit_transformation(transformation: CandidateTransformation, inputs: tuple[JsonObject, ...]) -> None:
    if transformation.deletes:
        raise ApplyProjectionError("UnitProjectionCompiler cannot delete desired resources")
    selected = {_identity(document) for document in inputs}
    written = {item.identity for item in transformation.writes}
    if written != selected:
        raise ApplyProjectionError("UnitProjectionCompiler must project exactly the selected Unit roots")
    for item in transformation.writes:
        metadata = cast(JsonObject, item.document["metadata"])
        if _owner_references(metadata) is not None:
            raise ApplyProjectionError("UnitProjectionCompiler cannot issue owned resources for explicit Unit input")
    _validate_payload_ownership(
        transformation,
        selected,
        payload_documents=transformation.writes,
    )


def _replace_document(candidate: InMemoryWorkspace, item: ProjectedDocument) -> None:
    _remove_document_variants(candidate, item.key)
    candidate.write(item.key, _canonical_json(item.document))


def _prune_partition(
    candidate: InMemoryWorkspace,
    current: Mapping[tuple[str, str, str], ProjectedDocument],
    applied: set[tuple[str, str, str]],
    partition: str | None,
) -> None:
    if partition is None:
        return
    owner_children = _owner_children(current)
    for identity, previous in current.items():
        metadata = cast(JsonObject, previous.document["metadata"])
        if identity in applied or _owner_references(metadata) is not None or _partition(metadata) != partition:
            continue
        for selected in _owned_closure(identity, owner_children):
            selected_document = current[selected]
            deleting = _copy_json_object(selected_document.document)
            deletion = cast(JsonObject, deleting["metadata"])
            if deletion.get("deletion") is None:
                deletion["deletion"] = {
                    "generation": 1,
                    "resourceDigest": f"sha256:{hashlib.sha256(_canonical_json(selected_document.document)).hexdigest()}",
                }
            _replace_document(candidate, ProjectedDocument(selected_document.key, deleting))


def _remove_document_variants(candidate: InMemoryWorkspace, key: str) -> None:
    base = key.rsplit(".", 1)[0]
    for suffix in _DOCUMENT_SUFFIXES:
        candidate_key = f"{base}{suffix}"
        try:
            candidate.delete(candidate_key)
        except Exception as exc:
            # Absence is normal; a malformed candidate is not.
            from gitopsctr.application.workspace import WorkspaceEntryNotFoundError

            if not isinstance(exc, WorkspaceEntryNotFoundError):
                raise


def _validate_payload_scope(key: str, prefixes: tuple[str, ...]) -> None:
    first = key.split("/", 1)[0]
    if first in _ROOT_DIRECTORIES:
        raise ApplyProjectionError("payload deltas cannot address reserved resource roots")
    if not prefixes or not any(key == prefix or key.startswith(f"{prefix}/") for prefix in prefixes):
        raise ApplyProjectionError(f"payload key {key!r} is outside the transformation's declared ownership scope")
    if _is_resource_key(key):
        raise ApplyProjectionError("payload writes cannot address resource-document paths")


def _validate_payload_ownership(
    transformation: CandidateTransformation,
    authorized_roots: set[tuple[str, str, str]],
    *,
    payload_documents: tuple[ProjectedDocument, ...],
    allow_projection_contexts: bool = False,
) -> None:
    """Bind payload paths to emitted resource identities, never compiler claims.

    Materialization follows the storage-qualified Unit identity, so two Stack
    roots may independently materialize a leaf named ``db``.  Projection
    context records are the one fixed Stack-only namespace; compiler supplied
    prefixes cannot broaden either authority.
    """

    allowed_prefixes = {
        f"materialized/{document.identity[2]}" for document in payload_documents if document.key.startswith("units/")
    }
    if allow_projection_contexts and authorized_roots:
        allowed_prefixes.add(".gitopsctr/projection-contexts")
        allowed_prefixes.add(".gitopsctr/transition-blocks.json")
    if any(prefix not in allowed_prefixes for prefix in transformation.payload_prefixes):
        raise ApplyProjectionError("payload prefixes must be derived from emitted resource identities")
    materialized_prefixes = {
        f"materialized/{document.identity[2]}" for document in payload_documents if document.key.startswith("units/")
    }
    if any(replacement.prefix not in materialized_prefixes for replacement in transformation.payload_replacements):
        raise ApplyProjectionError("payload replacements must target an emitted Unit materialization prefix")


def _apply_payload_entry(candidate: InMemoryWorkspace, entry: WorkspaceEntry) -> None:
    """Apply one explicit payload entry, preserving an exact semantic no-op."""

    try:
        if candidate.inspect(entry.key) == entry:
            return
        candidate.delete(entry.key, recursive=True)
    except WorkspaceEntryNotFoundError:
        pass
    if entry.kind is WorkspaceEntryKind.FILE:
        assert entry.content is not None
        candidate.write(entry.key, entry.content, executable=entry.executable)
    elif entry.kind is WorkspaceEntryKind.DIRECTORY:
        candidate.mkdir(entry.key)
    else:
        assert entry.target is not None
        candidate.symlink(entry.key, entry.target)


def _apply_transformation_payload(candidate: InMemoryWorkspace, transformation: CandidateTransformation) -> None:
    """Apply one already-authorized payload delta exactly once."""

    for entry in transformation.payload_writes:
        _apply_payload_entry(candidate, entry)
    for key in transformation.payload_deletes:
        try:
            candidate.delete(key, recursive=True)
        except WorkspaceEntryNotFoundError:
            raise ApplyProjectionError(f"transformation cannot delete unknown payload entry {key!r}") from None
    for replacement in transformation.payload_replacements:
        _apply_payload_replacement(candidate, replacement)


def _validate_payload_replacement_interactions(transformations: tuple[CandidateTransformation, ...]) -> None:
    prefixes = tuple(replacement.prefix for delta in transformations for replacement in delta.payload_replacements)
    if len(set(prefixes)) != len(prefixes) or any(
        left != right and (left.startswith(f"{right}/") or right.startswith(f"{left}/"))
        for index, left in enumerate(prefixes)
        for right in prefixes[index + 1 :]
    ):
        raise ApplyProjectionError("payload replacements cannot overlap another replacement subtree")
    direct_keys = tuple(
        key
        for delta in transformations
        for key in (
            *(entry.key for entry in delta.payload_writes),
            *delta.payload_deletes,
        )
    )
    if any(key == prefix or key.startswith(f"{prefix}/") for prefix in prefixes for key in direct_keys):
        raise ApplyProjectionError("payload replacements cannot overlap direct payload deltas")


def _apply_payload_replacement(candidate: InMemoryWorkspace, replacement: PayloadPrefixReplacement) -> None:
    """Fence, prune, and repopulate one materialization subtree atomically."""

    replacement._validate()
    actual_content_id, actual_entries = _payload_prefix_evidence_for_workspace(candidate, replacement.prefix)
    if (
        actual_content_id != replacement.expected_current_content_id
        or actual_entries != replacement.expected_current_entries
    ):
        raise ApplyProjectionError(f"payload replacement prefix {replacement.prefix!r} is stale or corrupt")
    if actual_entries:
        candidate.delete(replacement.prefix, recursive=True)
    for entry in replacement.entries:
        _apply_payload_entry(candidate, entry)


def _owner_children(
    documents: Mapping[tuple[str, str, str], ProjectedDocument],
) -> Mapping[tuple[str, str, str], tuple[tuple[str, str, str], ...]]:
    children: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {identity: [] for identity in documents}
    for identity, document in documents.items():
        metadata = cast(JsonObject, document.document["metadata"])
        owner = _owner_identity(metadata)
        if owner is None:
            continue
        parent, parent_uid = owner
        parent_document = documents.get(parent)
        if parent_document is None:
            raise ApplyProjectionError(f"{identity!r}: owned resource references a missing desired owner {parent!r}")
        parent_metadata = cast(JsonObject, parent_document.document["metadata"])
        if parent_metadata.get("uid") != parent_uid:
            raise ApplyProjectionError(f"{identity!r}: owner reference UID does not match its desired owner")
        children[parent].append(identity)
    return MappingProxyType({identity: tuple(sorted(values)) for identity, values in children.items()})


def _validate_owner_graph(documents: Mapping[tuple[str, str, str], ProjectedDocument]) -> None:
    children = _owner_children(documents)
    visiting: set[tuple[str, str, str]] = set()
    visited: set[tuple[str, str, str]] = set()

    def visit(identity: tuple[str, str, str]) -> None:
        if identity in visited:
            return
        if identity in visiting:
            raise ApplyProjectionError(f"desired ownership graph contains a cycle at {identity!r}")
        visiting.add(identity)
        for child in children[identity]:
            visit(child)
        visiting.remove(identity)
        visited.add(identity)

    for identity in sorted(documents):
        visit(identity)


def _owned_closure(
    root: tuple[str, str, str],
    children: Mapping[tuple[str, str, str], tuple[tuple[str, str, str], ...]],
) -> tuple[tuple[str, str, str], ...]:
    selected: list[tuple[str, str, str]] = []

    def visit(identity: tuple[str, str, str]) -> None:
        selected.append(identity)
        for child in children[identity]:
            visit(child)

    visit(root)
    return tuple(selected)


def _owner_identity(metadata: JsonObject) -> tuple[tuple[str, str, str], str] | None:
    owners = _owner_references(metadata)
    if owners is None:
        return None
    if not isinstance(owners, (tuple, list)) or len(owners) != 1 or not isinstance(owners[0], Mapping):
        raise ApplyProjectionError("desired ownerReferences must contain exactly one owner reference")
    owner = owners[0]
    api_version, kind, name, uid = (owner.get(name) for name in ("apiVersion", "kind", "name", "uid"))
    if not all(isinstance(value, str) and value for value in (api_version, kind, name, uid)):
        raise ApplyProjectionError("desired owner reference requires apiVersion, kind, name, and uid")
    assert isinstance(api_version, str) and isinstance(kind, str) and isinstance(name, str) and isinstance(uid, str)
    _resource_name(name, "desired owner name")
    return (api_version, kind, name), uid


def _validate_stack_template_referrers(documents: Mapping[tuple[str, str, str], ProjectedDocument]) -> None:
    """Reject a live desired Stack that points at a template being deleted."""

    deleting_templates = {
        identity[2]
        for identity, document in documents.items()
        if identity[1] == "StackTemplate"
        and cast(JsonObject, document.document["metadata"]).get("deletion") is not None
    }
    if not deleting_templates:
        return
    for identity, document in documents.items():
        if identity[1] != "Stack":
            continue
        metadata = cast(JsonObject, document.document["metadata"])
        if metadata.get("deletion") is not None:
            continue
        spec = document.document.get("spec")
        template_ref = spec.get("templateRef") if isinstance(spec, Mapping) else None
        template_name = template_ref.get("name") if isinstance(template_ref, Mapping) else None
        if template_name in deleting_templates:
            raise ApplyProjectionError(
                f"desired Stack {identity[2]!r} references deleting StackTemplate {template_name!r}"
            )


def _key_for(document: JsonObject) -> str:
    kind = _kind(document)
    metadata = cast(JsonObject, document["metadata"])
    name = cast(str, metadata["name"])
    owner = _owner_identity(metadata)
    if owner is not None:
        owner_identity, _owner_uid = owner
        if kind in {"Stack", "StackTemplate"} or owner_identity[1] != "Stack":
            raise ApplyProjectionError("only Unit resources may be Stack-owned desired children")
        return f"units/{owner_identity[2]}/{name}.json"
    directory = "stack-templates" if kind == "StackTemplate" else "stacks" if kind == "Stack" else "units"
    return f"{directory}/{name}.json"


def _storage_identity(document: JsonObject, key: str) -> tuple[str, str, str]:
    """Return the graph identity qualified by canonical Stack ownership/path."""

    api_version, kind, name = _identity(document)
    metadata = cast(JsonObject, document["metadata"])
    owner = _owner_identity(metadata)
    expected = _key_for(document)
    if key.rsplit(".", 1)[0] != expected.rsplit(".", 1)[0]:
        raise ApplyProjectionError("resource path does not match its metadata and Stack owner fence")
    if owner is None:
        return api_version, kind, name
    owner_identity, _owner_uid = owner
    return api_version, kind, f"{owner_identity[2]}/{name}"


def _identity(document: JsonObject) -> tuple[str, str, str]:
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ApplyProjectionError("resource requires object metadata")
    name = metadata.get("name")
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(name, str):
        raise ApplyProjectionError("resource requires apiVersion, kind, and metadata.name")
    return api_version, kind, name


def _kind(document: JsonObject) -> str:
    return _identity(document)[1]


def _validate_resource_document(document: object, location: str) -> None:
    if not isinstance(document, Mapping):
        raise ApplyProjectionError(f"{location}: resource must be a JSON object")
    identity = _identity(cast(JsonObject, document))
    _resource_name(identity[2], f"{location} metadata.name")
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping) or not all(isinstance(key, str) for key in metadata):
        raise ApplyProjectionError(f"{location}: metadata must be a JSON object")


def _candidate_key(key: str) -> None:
    if not _is_resource_key(key):
        raise ApplyProjectionError(f"candidate key must be a resource document path: {key!r}")
    segments = key.rsplit(".", 1)[0].split("/")
    if len(segments) < 2 or any(not segment or segment in {".", ".."} for segment in segments):
        raise ApplyProjectionError(f"candidate key is not safe: {key!r}")


def _is_resource_key(key: str) -> bool:
    first, separator, _rest = key.partition("/")
    return bool(separator) and first in _ROOT_DIRECTORIES and any(key.endswith(suffix) for suffix in _DOCUMENT_SUFFIXES)


def _resource_name(value: object, description: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_NAME.fullmatch(value):
        raise ApplyProjectionError(f"{description} must be a canonical resource name")
    return value


def _canonical_identity_text(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ApplyProjectionError(f"{description} must be non-empty canonical text")
    return value


def _issue_root_uid(
    context: ApplyProjectionContext,
    identity: tuple[str, str, str],
    authored_content_id: ContentId,
) -> str:
    """Request a UID from the explicit authority; core never invents one."""

    issuer = context.root_identity_issuer
    if issuer is None:
        raise ApplyProjectionError("new desired roots require an injected RootIncarnationIssuer")
    source_snapshot_id = context.primary_source.retained.source_snapshot_id if context.primary_source else None
    request = RootIdentityRequest(
        context.environment_id,
        identity[0],
        identity[1],
        identity[2],
        source_snapshot_id,
        authored_content_id,
        tuple(
            tombstone.uid
            for tombstone in context.finalized_tombstones
            if (tombstone.api_version, tombstone.kind, tombstone.qualified_name) == identity
        ),
    )
    issued = issuer.issue(request)
    if not isinstance(issued, IssuedRootIdentity):
        raise ApplyProjectionError("RootIncarnationIssuer must return an IssuedRootIdentity")
    issued._validate()
    if issued.request != request or issued.issuer_id != issuer.issuer_id:
        raise ApplyProjectionError("issued root identity does not bind this issuer and exact root request")
    return issued.uid


def _labels(metadata: JsonObject) -> dict[str, str]:
    raw = metadata.get("labels")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ApplyProjectionError("metadata.labels must be a string mapping")
    return dict(cast(dict[str, str], raw))


def _assign_labels(metadata: JsonObject, labels: Mapping[str, str]) -> None:
    if labels:
        metadata["labels"] = dict(sorted(labels.items()))
    else:
        metadata.pop("labels", None)


def _partition(metadata: JsonObject) -> str | None:
    labels = _labels(metadata)
    value = labels.get(_PARTITION_LABEL)
    if value is not None:
        _resource_name(value, "metadata partition")
    return value


def _owner_references(metadata: JsonObject) -> object | None:
    return metadata.get("ownerReferences")


def _copy_json_object(value: JsonObject) -> JsonObject:
    return cast(JsonObject, _plain_json(value))


def _canonical_json(value: JsonObject) -> bytes:
    return json.dumps(_plain_json(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _frozen_json_object(value: JsonObject) -> JsonObject:
    frozen = _freeze_json(_plain_json(value))
    assert isinstance(frozen, Mapping)
    return cast(JsonObject, frozen)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    if isinstance(value, list):
        return [_plain_json(child) for child in value]
    return value
