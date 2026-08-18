"""Application-owned orchestration for apply projection and publication.

This module coordinates only typed logical capabilities.  It does not know
about Git refs, repositories, filesystem paths, temporary directories, or CLI
rendering.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from gitopsctr.application.apply import ApplyCommand, ApplyResult, AuthoredChangeSet
from gitopsctr.application.apply_projection import (
    ApplyDocumentValidator,
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ApplyPublicationDecision,
    ExactPlane,
    FinalizedTombstone,
    RetainedSourceDescriptor,
    RetainedSourcePlane,
    RootIncarnationIssuer,
    StackProjectionCompiler,
    UnitProjectionCompiler,
    WorkspaceProjectionContext,
    project_apply,
)
from gitopsctr.application.model import (
    ChannelId,
    CoordinationChange,
    CoordinationObservation,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcomeState,
    PublicationRecovery,
    PublicationRecoveryLocator,
    PublicationTarget,
    RetainedSource,
    SealedCandidate,
    SourceOwnershipChange,
    SourceSnapshotId,
)
from gitopsctr.application.ports import (
    CandidateStore,
    PublicationExecutionUnknownError,
    PublicationTransaction,
    SnapshotReader,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.sources import SourceRepository, same_source_payload
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace
from gitopsctr.resource_api import GVK

_RESOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_RESOURCE_UID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_QUALIFIED_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*")


class ApplyOrchestrationError(ValueError):
    """One apply cannot be completed through its closed application ports."""


@dataclass(frozen=True, slots=True)
class ApplyEnvironmentConfiguration:
    """Adapter-resolved environment policy and source descriptor bindings."""

    desired_channel: ChannelId
    observed_channel: ChannelId
    candidate_channel: ChannelId | None
    policy: ApplyProjectionPolicy
    workspace_context: WorkspaceProjectionContext | None = None
    primary_source: RetainedSourceDescriptor | None = None
    named_sources: tuple[RetainedSourceDescriptor, ...] = ()
    coordination_requests: tuple[ApplyCoordinationRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyCoordinationRequest:
    """One adapter-policy coordination value to fence into publication."""

    key: str
    next_value: str

    def __post_init__(self) -> None:
        for value, description in ((self.key, "key"), (self.next_value, "next value")):
            if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
                raise ValueError(f"apply coordination {description} must be canonical text")


class ApplyEnvironmentResolver(Protocol):
    """Resolve configuration from exact source input without observing state."""

    def resolve(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyEnvironmentConfiguration: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplySourceEvidence:
    """Every exact retained source capability available to one projection."""

    retained_planes: tuple[RetainedSourcePlane, ...]
    primary_source: RetainedSourceDescriptor | None = None
    named_sources: tuple[RetainedSourceDescriptor, ...] = ()
    release_on_nonpublication: tuple[RetainedSource, ...] = ()


class ApplySourceEvidenceProvider(Protocol):
    """Recover primary and historical source planes without selector inference."""

    def prepare(
        self,
        command: ApplyCommand,
        changes: AuthoredChangeSet,
        configuration: ApplyEnvironmentConfiguration,
        desired: ExactPlane,
        observed: ExactPlane,
    ) -> ApplySourceEvidence: ...


class ApplyPublicationAuthority(CandidateStore, PublicationTransaction, Protocol):
    """One authority for channel heads, candidates, and source ownership."""

    def prepare_head(self, channel_id: ChannelId) -> HeadObservation:
        """Return an exact managed head, explicitly bootstrapping migration state if needed."""

    def ownership(self, source: SourceSnapshotId) -> OwnershipObservation: ...

    def coordination(self, key: str) -> CoordinationObservation: ...

    def recovery_locator(self, intent: PublicationIntent) -> PublicationRecoveryLocator: ...

    def recover_publication(self, locator: PublicationRecoveryLocator) -> PublicationRecovery: ...

    def close(self) -> None: ...


class ApplyPublicationIdentityIssuer(Protocol):
    """Issue deterministic, retry-stable publication identities."""

    def issue_attempt(
        self, environment: str, target: ChannelId, base: HeadObservation, candidate: SealedCandidate
    ) -> PublicationAttemptId: ...

    def issue_owner(self, environment: str, candidate: SealedCandidate) -> OwnershipId: ...


@dataclass(frozen=True, slots=True)
class CandidatePublicationRequest:
    """Publish one already validated complete desired candidate."""

    environment_id: str
    desired_channel: ChannelId
    expected_desired_head: HeadObservation
    candidate: ImmutableWorkspace
    review_required: bool = False
    candidate_channel: ChannelId | None = None
    coordination_requests: tuple[ApplyCoordinationRequest, ...] = ()
    retained_sources: tuple[RetainedSource, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.environment_id, str)
            or not self.environment_id
            or self.environment_id != self.environment_id.strip()
            or "\x00" in self.environment_id
        ):
            raise ValueError("candidate publication environment ID must be canonical text")
        if not isinstance(self.desired_channel, ChannelId):
            raise TypeError("candidate publication desired_channel must be a ChannelId")
        if not isinstance(self.expected_desired_head, HeadObservation):
            raise TypeError("candidate publication expected_desired_head must be a HeadObservation")
        self.expected_desired_head._validate()
        if self.expected_desired_head.channel_id != self.desired_channel:
            raise ValueError("candidate publication expected head belongs to another desired channel")
        if not isinstance(self.candidate, ImmutableWorkspace) or self.candidate.is_mutable:
            raise TypeError("candidate publication requires an immutable workspace")
        if type(self.review_required) is not bool:
            raise TypeError("candidate publication review_required must be bool")
        if self.review_required != (self.candidate_channel is not None):
            raise ValueError("review candidate publication requires exactly one candidate channel")
        if self.candidate_channel in {self.desired_channel}:
            raise ValueError("candidate publication channel must differ from desired")
        if not isinstance(self.coordination_requests, tuple) or any(
            not isinstance(item, ApplyCoordinationRequest) for item in self.coordination_requests
        ):
            raise TypeError("candidate publication coordination requests must be a tuple")
        if not isinstance(self.retained_sources, tuple) or any(
            not isinstance(item, RetainedSource) for item in self.retained_sources
        ):
            raise TypeError("candidate publication retained sources must be a tuple")
        source_snapshots: set[SourceSnapshotId] = set()
        for retained in self.retained_sources:
            retained._validate()
            if retained.source_snapshot_id in source_snapshots:
                raise ValueError("candidate publication retained sources must have unique snapshots")
            source_snapshots.add(retained.source_snapshot_id)


@dataclass(frozen=True, slots=True)
class CandidatePublicationCoordinator:
    """Seal and transactionally publish a complete candidate through one authority."""

    authority: ApplyPublicationAuthority
    identity_issuer: ApplyPublicationIdentityIssuer

    def publish(self, request: CandidatePublicationRequest) -> ApplyResult:
        if not isinstance(request, CandidatePublicationRequest):
            raise TypeError("request must be a CandidatePublicationRequest")
        request.__post_init__()
        workspace = self.authority.begin_candidate(request.candidate, request.expected_desired_head.snapshot_id)
        sealed = self.authority.seal_candidate(workspace)
        if sealed.content_id != request.candidate.content_id:
            raise ApplyOrchestrationError("sealed candidate differs from the validated candidate workspace")
        if request.review_required:
            assert request.candidate_channel is not None
            target_channel = request.candidate_channel
            expected_head = self.authority.prepare_head(target_channel)
            target = PublicationTarget.REVIEW_CANDIDATE
            mode = PublicationMode.REVIEW_REQUIRED
            review_base = request.expected_desired_head
        else:
            target_channel = request.desired_channel
            expected_head = request.expected_desired_head
            target = PublicationTarget.ACCEPTED_DESIRED
            mode = PublicationMode.DIRECT_ACCEPTED
            review_base = None
        owner = self.identity_issuer.issue_owner(request.environment_id, sealed)
        coordination = tuple(
            CoordinationChange(item.key, self.authority.coordination(item.key), item.next_value)
            for item in request.coordination_requests
        )
        ownership = tuple(
            SourceOwnershipChange(
                retained,
                self.authority.ownership(retained.source_snapshot_id),
                owner,
            )
            for retained in request.retained_sources
        )
        intent = PublicationIntent(
            self.identity_issuer.issue_attempt(request.environment_id, target_channel, expected_head, sealed),
            target_channel,
            expected_head,
            sealed,
            ownership,
            owner,
            coordination,
            target,
            mode,
            review_base_head=review_base,
        )
        locator = self.authority.recovery_locator(intent)
        verified = False
        try:
            outcome = self.authority.execute(intent)
        except PublicationExecutionUnknownError:
            outcome = self.authority.verify(intent)
            verified = True
        outcome._validate()
        if outcome.state is PublicationOutcomeState.UNKNOWN and not verified:
            outcome = self.authority.verify(intent)
            outcome._validate()
        return ApplyResult(
            sealed.snapshot_id if outcome.state is PublicationOutcomeState.COMMITTED else None,
            mode,
            intent,
            outcome,
            locator,
        )

    def recover(self, locator: PublicationRecoveryLocator) -> ApplyResult:
        recovered = self.authority.recover_publication(locator)
        recovered.intent._validate()
        recovered.outcome._validate()
        snapshot = (
            recovered.intent.candidate.snapshot_id
            if recovered.outcome.state is PublicationOutcomeState.COMMITTED
            else None
        )
        return ApplyResult(snapshot, recovered.intent.mode, recovered.intent, recovered.outcome, locator)


@dataclass(frozen=True, slots=True)
class HmacApplyPublicationIdentityIssuer:
    """Explicit-seed production identity provider with no ambient UUID or clock."""

    issuer_id: str
    identity_seed: str

    def __post_init__(self) -> None:
        for value, description in ((self.issuer_id, "issuer ID"), (self.identity_seed, "identity seed")):
            if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
                raise ValueError(f"apply publication {description} must be canonical text")

    def issue_attempt(
        self, environment: str, target: ChannelId, base: HeadObservation, candidate: SealedCandidate
    ) -> PublicationAttemptId:
        candidate._validate()
        base._validate()
        return PublicationAttemptId(
            "apply-attempt:"
            + self._digest(
                environment,
                target.value,
                base.to_wire(),
                candidate.snapshot_id.value,
                candidate.content_id.value,
            )
        )

    def issue_owner(self, environment: str, candidate: SealedCandidate) -> OwnershipId:
        candidate._validate()
        return OwnershipId(
            "apply-owner:" + self._digest(environment, candidate.snapshot_id.value, candidate.content_id.value)
        )

    def _digest(self, *values: str) -> str:
        evidence = "\x00".join((self.issuer_id, *values)).encode()
        return hmac.new(self.identity_seed.encode(), evidence, hashlib.sha256).hexdigest()


@dataclass(slots=True)
class ApplyCoordinator:
    """Coordinate pure apply projection and one recoverable publication."""

    snapshot_reader: SnapshotReader
    authority: ApplyPublicationAuthority
    source_repository: SourceRepository
    environment_resolver: ApplyEnvironmentResolver
    validator: ApplyDocumentValidator
    unit_compiler: UnitProjectionCompiler
    stack_compiler: StackProjectionCompiler
    root_identity_issuer: RootIncarnationIssuer
    publication_identity_issuer: ApplyPublicationIdentityIssuer
    source_evidence_provider: ApplySourceEvidenceProvider | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        if not isinstance(command, ApplyCommand):
            raise TypeError("command must be an ApplyCommand")
        if not isinstance(changes, AuthoredChangeSet):
            raise TypeError("changes must be an AuthoredChangeSet")
        acquisition = changes.source_acquisition
        retained: RetainedSource | None = None
        additional_retained: tuple[RetainedSource, ...] = ()
        published_or_ambiguous = False
        try:
            if changes.source_snapshot_id is not None:
                if acquisition is None:
                    raise ApplyOrchestrationError(
                        "source-backed apply requires the decoder's exact retained source acquisition"
                    )
                acquisition._validate()
                recovered = self.source_repository.recover(acquisition.retained)
                if not same_source_payload(recovered, acquisition.snapshot):
                    raise ApplyOrchestrationError("recovered authored source differs from decoded exact source")
                retained = acquisition.retained
            elif acquisition is not None:
                raise ApplyOrchestrationError("non-source-backed apply cannot carry a retained source acquisition")

            configuration = self.environment_resolver.resolve(command, changes)
            desired = self._plane(configuration.desired_channel)
            if configuration.policy.review_required and desired.head.is_absent and not command.dry_run:
                desired = self._initialize_review_base(command, configuration, desired)
            observed = self._plane(configuration.observed_channel)
            source_evidence = (
                self.source_evidence_provider.prepare(command, changes, configuration, desired, observed)
                if self.source_evidence_provider is not None
                else ApplySourceEvidence(
                    self._retained_planes(changes, configuration),
                    configuration.primary_source,
                    configuration.named_sources,
                )
            )
            additional_retained = source_evidence.release_on_nonpublication
            context = ApplyProjectionContext(
                command.environment_id,
                configuration.desired_channel,
                configuration.observed_channel,
                configuration.candidate_channel,
                configuration.policy,
                command.partition,
                command.dry_run,
                configuration.workspace_context,
                source_evidence.primary_source,
                source_evidence.named_sources,
                self.root_identity_issuer,
                _finalized_tombstones(desired),
            )
            projected = project_apply(
                changes,
                current_desired=desired,
                observed=observed,
                retained_sources=source_evidence.retained_planes,
                context=context,
                validator=self.validator,
                unit_compiler=self.unit_compiler,
                stack_compiler=self.stack_compiler,
            )
            if projected.plan.decision in {ApplyPublicationDecision.NO_CHANGE, ApplyPublicationDecision.DRY_RUN}:
                return ApplyResult(desired.head.snapshot_id, None)

            immutable_candidate = InMemoryWorkspace(
                projected.candidate.list_entries(), capabilities=projected.candidate.capabilities, mutable=False
            )
            candidate_workspace = self.authority.begin_candidate(immutable_candidate, desired.head.snapshot_id)
            sealed = self.authority.seal_candidate(candidate_workspace)
            if sealed.content_id != projected.plan.candidate_content_id:
                raise ApplyOrchestrationError("sealed candidate differs from the validated apply plan")

            review = projected.plan.decision is ApplyPublicationDecision.REVIEW
            if review:
                target_channel = configuration.candidate_channel
                if target_channel is None:
                    raise ApplyOrchestrationError("review-required apply has no candidate channel")
                target_head = self.authority.prepare_head(target_channel)
                mode = PublicationMode.REVIEW_REQUIRED
                target = PublicationTarget.REVIEW_CANDIDATE
                review_base = desired.head
            else:
                target_channel = configuration.desired_channel
                target_head = desired.head
                mode = PublicationMode.DIRECT_ACCEPTED
                target = PublicationTarget.ACCEPTED_DESIRED
                review_base = None
            owner = self.publication_identity_issuer.issue_owner(str(command.environment_id), sealed)
            source_changes = tuple(
                SourceOwnershipChange(
                    plane.retained,
                    self.authority.ownership(plane.retained.source_snapshot_id),
                    owner,
                )
                for plane in projected.plan.retained_sources
            )
            coordination_changes = tuple(
                CoordinationChange(
                    request.key,
                    self.authority.coordination(request.key),
                    request.next_value,
                )
                for request in configuration.coordination_requests
            )
            intent = PublicationIntent(
                self.publication_identity_issuer.issue_attempt(
                    str(command.environment_id), target_channel, target_head, sealed
                ),
                target_channel,
                target_head,
                sealed,
                source_changes,
                owner,
                coordination_changes,
                target,
                mode,
                review_base_head=review_base,
            )
            locator = self.authority.recovery_locator(intent)
            verified = False
            try:
                outcome = self.authority.execute(intent)
            except PublicationExecutionUnknownError:
                # An adapter exception can happen after the durable transaction
                # crossed its commit point.  Re-observe the exact attempt before
                # deciding whether its retained sources may be released.
                outcome = self.authority.verify(intent)
                verified = True
                outcome._validate()
            outcome._validate()
            if outcome.state is PublicationOutcomeState.UNKNOWN and not verified:
                outcome = self.authority.verify(intent)
                outcome._validate()
            published_or_ambiguous = outcome.state in {
                PublicationOutcomeState.COMMITTED,
                PublicationOutcomeState.UNKNOWN,
            }
            return ApplyResult(
                sealed.snapshot_id if outcome.state is PublicationOutcomeState.COMMITTED else None,
                mode,
                intent,
                outcome,
                locator,
            )
        finally:
            if not published_or_ambiguous:
                # This exact acquisition handle was minted for this apply.  A
                # different owner of the same source snapshot does not adopt it.
                handles = ((retained,) if retained is not None else ()) + additional_retained
                released: set[object] = set()
                for handle in handles:
                    if handle.handle in released:
                        continue
                    self.source_repository.release(handle)
                    released.add(handle.handle)

    def _plane(self, channel: ChannelId) -> ExactPlane:
        head = self.authority.prepare_head(channel)
        head._validate()
        if head.snapshot_id is None:
            return ExactPlane(head, InMemoryWorkspace(mutable=False))
        snapshot = self.snapshot_reader.open_snapshot(head.snapshot_id)
        return ExactPlane(head, snapshot.workspace, snapshot)

    def _initialize_review_base(
        self,
        command: ApplyCommand,
        configuration: ApplyEnvironmentConfiguration,
        desired: ExactPlane,
    ) -> ExactPlane:
        """Publish one explicit empty accepted base before the first reviewed change."""

        if not desired.head.is_absent:
            return desired
        candidate_workspace = self.authority.begin_candidate(InMemoryWorkspace(mutable=False), None)
        sealed = self.authority.seal_candidate(candidate_workspace)
        owner = self.publication_identity_issuer.issue_owner(str(command.environment_id), sealed)
        intent = PublicationIntent(
            self.publication_identity_issuer.issue_attempt(
                str(command.environment_id), configuration.desired_channel, desired.head, sealed
            ),
            configuration.desired_channel,
            desired.head,
            sealed,
            (),
            owner,
            (),
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.DIRECT_ACCEPTED,
        )
        locator = self.authority.recovery_locator(intent)
        try:
            outcome = self.authority.execute(intent)
        except PublicationExecutionUnknownError:
            outcome = self.authority.verify(intent)
        outcome._validate()
        if outcome.state is PublicationOutcomeState.UNKNOWN:
            raise ApplyOrchestrationError(
                f"initial reviewed apply base publication is unknown; recover attempt {locator.to_wire()}"
            )
        if outcome.state is PublicationOutcomeState.NOT_COMMITTED:
            raise ApplyOrchestrationError("initial reviewed apply base was not committed")
        initialized = self._plane(configuration.desired_channel)
        if initialized.head.snapshot_id != sealed.snapshot_id:
            raise ApplyOrchestrationError("initial reviewed apply base proof is not immediately visible")
        return initialized

    def recover(self, locator: PublicationRecoveryLocator) -> ApplyResult:
        """Recover one durable attempt without resolving source selectors again."""

        if not isinstance(locator, PublicationRecoveryLocator):
            raise TypeError("locator must be a PublicationRecoveryLocator")
        recovered = self.authority.recover_publication(locator)
        recovered.intent._validate()
        recovered.outcome._validate()
        snapshot = (
            recovered.intent.candidate.snapshot_id
            if recovered.outcome.state is PublicationOutcomeState.COMMITTED
            else None
        )
        return ApplyResult(snapshot, recovered.intent.mode, recovered.intent, recovered.outcome, locator)

    def _retained_planes(
        self, changes: AuthoredChangeSet, configuration: ApplyEnvironmentConfiguration
    ) -> tuple[RetainedSourcePlane, ...]:
        acquisition = changes.source_acquisition
        if acquisition is None:
            if configuration.primary_source is not None or configuration.named_sources:
                raise ApplyOrchestrationError("source descriptors require an exact authored source acquisition")
            return ()
        acquisition._validate()
        descriptors = tuple(
            descriptor
            for descriptor in (configuration.primary_source, *configuration.named_sources)
            if descriptor is not None
        )
        if any(descriptor.retained != acquisition.retained for descriptor in descriptors):
            raise ApplyOrchestrationError("apply source descriptor belongs to another retained source")
        source = acquisition.snapshot
        channel = ChannelId(f"retained-source/{source.source_snapshot_id.source_id.value}")
        head = HeadObservation.present(
            channel,
            source.source_snapshot_id.snapshot_id,
            f"retained:{acquisition.retained.handle.value}",
        )
        snapshot = SnapshotView(source.source_snapshot_id.snapshot_id, source.content_id, source.workspace)
        return (RetainedSourcePlane(acquisition.retained, ExactPlane(head, source.workspace, snapshot), descriptors),)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.environment_resolver.close()
        finally:
            try:
                self.authority.close()
            finally:
                try:
                    self.source_repository.close()
                finally:
                    self.snapshot_reader.close()


def _finalized_tombstones(desired: ExactPlane) -> tuple[FinalizedTombstone, ...]:
    """Load canonical finalized-incarnation fences from the exact desired view."""

    prefix = ".gitopsctr/incarnations/resources"
    tombstones: list[FinalizedTombstone] = []
    for entry in desired.workspace.list_entries(prefix):
        if entry.content is None or not entry.key.endswith(".json"):
            raise ApplyOrchestrationError("desired incarnation evidence must contain canonical JSON files")
        try:
            document = json.loads(entry.content)
            resource = document["resource"]
            required = {
                "apiVersion",
                "kind",
                "name",
                "uid",
                "deletionGeneration",
                "qualifiedName",
                "effectLeaseRef",
            }
            if (
                not isinstance(document, dict)
                or set(document) != {"schema", "kind", "resource"}
                or type(document.get("schema")) is not int
                or document.get("schema") != 1
                or document.get("kind") != "ResourceIncarnationTombstone"
                or not isinstance(resource, dict)
                or not required <= set(resource)
                or not set(resource) <= required | {"partition"}
            ):
                raise ValueError
            api_version = resource["apiVersion"]
            kind = resource["kind"]
            name = resource["name"]
            qualified_name = resource["qualifiedName"]
            uid = resource["uid"]
            deletion_generation = resource["deletionGeneration"]
            partition = resource.get("partition")
            effect_lease_ref = resource["effectLeaseRef"]
            if (
                not isinstance(api_version, str)
                or not isinstance(kind, str)
                or not isinstance(name, str)
                or _RESOURCE_NAME.fullmatch(name) is None
                or not isinstance(uid, str)
                or _RESOURCE_UID.fullmatch(uid) is None
                or not isinstance(qualified_name, str)
                or _QUALIFIED_NAME.fullmatch(qualified_name) is None
                or type(deletion_generation) is not int
                or deletion_generation < 1
                or (
                    partition is not None
                    and (not isinstance(partition, str) or _RESOURCE_NAME.fullmatch(partition) is None)
                )
                or (effect_lease_ref is not None and (not isinstance(effect_lease_ref, str) or not effect_lease_ref))
            ):
                raise ValueError
            GVK(api_version, kind)
            tombstone = FinalizedTombstone(api_version, kind, qualified_name, uid)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplyOrchestrationError(f"invalid desired incarnation evidence: {entry.key!r}") from exc
        expected = f"{prefix}/{api_version}/{kind}/{qualified_name}/{uid}.json"
        if entry.key != expected:
            raise ApplyOrchestrationError(f"invalid desired incarnation evidence path: {entry.key!r}")
        tombstones.append(tombstone)
    return tuple(sorted(tombstones, key=lambda item: (item.api_version, item.kind, item.qualified_name, item.uid)))
