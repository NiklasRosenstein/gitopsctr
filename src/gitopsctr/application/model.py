"""Backend-neutral application vocabulary.

The application layer uses these values to preserve the identity and fencing
guarantees of a deployment without admitting storage, transport, or CLI
concepts into its contracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, SupportsIndex


def _require_opaque_value(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{description} must be a non-empty, trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{description} must not contain control characters")
    return value


def _require_instance(value: object, expected_type: type[object], description: str) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f"{description} must be a {expected_type.__name__}")


def _canonical_wire(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class _OpaqueValue:
    """A validated opaque identifier whose spelling is already canonical."""

    value: str

    def __post_init__(self) -> None:
        _require_opaque_value(self.value, type(self).__name__)

    def __str__(self) -> str:
        return self.value

    def to_wire(self) -> str:
        """Return the caller-supplied canonical representation."""

        return self.value


@dataclass(frozen=True)
class SnapshotId(_OpaqueValue):
    """One immutable state version; it is intentionally distinct from content."""


@dataclass(frozen=True)
class ContentId(_OpaqueValue):
    """Deterministic identity for exact logical content."""


@dataclass(frozen=True)
class EnvironmentId(_OpaqueValue):
    """Opaque identity of a deployment environment."""


@dataclass(frozen=True)
class AuthorityObservation(_OpaqueValue):
    """Exact trusted observation of an environment's authority mapping."""


@dataclass(frozen=True)
class AuthorityIssuer(_OpaqueValue):
    """Identity of the authority that issued an accepted desired snapshot."""


@dataclass(frozen=True)
class ChannelId(_OpaqueValue):
    """Opaque identity of a mutable desired, observed, or candidate channel."""


@dataclass(frozen=True)
class SourceId(_OpaqueValue):
    """Opaque identity of a source repository or other authored source."""


@dataclass(frozen=True)
class ExecutionIdentity(_OpaqueValue):
    """Explicit identity of the runner that performs an application operation."""


@dataclass(frozen=True)
class PublicationAttemptId(_OpaqueValue):
    """Distinct durable identity for one publication transaction attempt."""


@dataclass(frozen=True)
class SealedCandidateHandle(_OpaqueValue):
    """Adapter-owned handle for an immutable sealed candidate."""


@dataclass(frozen=True)
class RetainedSourceHandle(_OpaqueValue):
    """Durable retention handle through which one source snapshot can be restored."""


@dataclass(frozen=True)
class OwnershipId(_OpaqueValue):
    """Opaque durable owner identity used for publication source claims."""


@dataclass(frozen=True)
class EffectLeaseToken(_OpaqueValue):
    """Opaque fencing token issued by the effect-retention implementation."""


@dataclass(frozen=True)
class HeadObservation:
    """One exact incarnation of a channel head, including observed absence.

    ``incarnation`` is supplied by the snapshot adapter and changes on every
    head update.  It consequently fences an ``A -> B -> A`` sequence even if
    the snapshot at the beginning and end is identical.
    """

    channel_id: ChannelId
    snapshot_id: SnapshotId | None
    incarnation: str

    def __post_init__(self) -> None:
        _require_instance(self.channel_id, ChannelId, "channel_id")
        if self.snapshot_id is not None:
            _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")
        _require_opaque_value(self.incarnation, "head incarnation")

    @classmethod
    def absent(cls, channel_id: ChannelId, incarnation: str) -> HeadObservation:
        """Create an incarnation-fenced observation that a channel is absent."""

        return cls(channel_id, None, incarnation)

    @classmethod
    def present(cls, channel_id: ChannelId, snapshot_id: SnapshotId, incarnation: str) -> HeadObservation:
        """Create an incarnation-fenced observation of one present head."""

        return cls(channel_id, snapshot_id, incarnation)

    @property
    def is_absent(self) -> bool:
        return self.snapshot_id is None

    def to_wire(self) -> str:
        return _canonical_wire(self._wire_data())

    def _wire_data(self) -> dict[str, str | None]:
        return {
            "channel": self.channel_id.to_wire(),
            "incarnation": self.incarnation,
            "snapshot": self.snapshot_id.to_wire() if self.snapshot_id is not None else None,
        }


@dataclass(frozen=True)
class SourceSnapshotId:
    """An exact immutable snapshot retained from one authored source."""

    source_id: SourceId
    snapshot_id: SnapshotId

    def __post_init__(self) -> None:
        _require_instance(self.source_id, SourceId, "source_id")
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")

    def to_wire(self) -> str:
        return _canonical_wire({"snapshot": self.snapshot_id.to_wire(), "source": self.source_id.to_wire()})


_ACCEPTED_DESIRED_ISSUANCE = object()


@dataclass(frozen=True, init=False)
class AcceptedDesiredSnapshot:
    """Desired state independently accepted by a deployment authority.

    This value cannot represent an absent channel and binds the accepted state
    to the exact environment, authority observation, channel incarnation, and
    immutable snapshot observed by the authority.
    """

    environment_id: EnvironmentId
    issuer: AuthorityIssuer
    authority_observation: AuthorityObservation
    channel_id: ChannelId
    head_observation: HeadObservation
    snapshot_id: SnapshotId
    _issuance: object = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("AcceptedDesiredSnapshot must be issued by a DeploymentAuthority")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("AcceptedDesiredSnapshot must not be subclassed")

    def _validate(self) -> None:
        if type(self) is not AcceptedDesiredSnapshot:
            raise TypeError("AcceptedDesiredSnapshot must not be subclassed")
        if getattr(self, "_issuance", None) is not _ACCEPTED_DESIRED_ISSUANCE:
            raise TypeError("AcceptedDesiredSnapshot has no valid authority issuance proof")
        _require_instance(self.environment_id, EnvironmentId, "environment_id")
        _require_instance(self.issuer, AuthorityIssuer, "issuer")
        _require_instance(self.authority_observation, AuthorityObservation, "authority_observation")
        _require_instance(self.channel_id, ChannelId, "channel_id")
        _require_instance(self.head_observation, HeadObservation, "head_observation")
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")
        if self.head_observation.channel_id != self.channel_id:
            raise ValueError("accepted desired snapshot channel must match its head observation")
        if self.head_observation.snapshot_id is None:
            raise ValueError("accepted desired snapshot cannot bind an absent head")
        if self.head_observation.snapshot_id != self.snapshot_id:
            raise ValueError("accepted desired snapshot must match its observed head snapshot")

    def to_wire(self) -> str:
        self._validate()
        return _canonical_wire(self._wire_data())

    def __copy__(self) -> AcceptedDesiredSnapshot:
        raise TypeError("AcceptedDesiredSnapshot must not be copied")

    def __deepcopy__(self, _memo: object) -> AcceptedDesiredSnapshot:
        raise TypeError("AcceptedDesiredSnapshot must not be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex, /) -> str | tuple[Any, ...]:
        raise TypeError("AcceptedDesiredSnapshot must not be serialized")

    def _wire_data(self) -> dict[str, object]:
        return {
            "authority": self.authority_observation.to_wire(),
            "channel": self.channel_id.to_wire(),
            "environment": self.environment_id.to_wire(),
            "head": self.head_observation._wire_data(),
            "issuer": self.issuer.to_wire(),
            "snapshot": self.snapshot_id.to_wire(),
        }


def _issue_accepted_desired_snapshot(
    environment_id: EnvironmentId,
    issuer: AuthorityIssuer,
    authority_observation: AuthorityObservation,
    channel_id: ChannelId,
    head_observation: HeadObservation,
    snapshot_id: SnapshotId,
) -> AcceptedDesiredSnapshot:
    """Issue a trusted acceptance value for use only by authority adapters.

    This deliberately private factory is the narrow construction seam for
    authority adapters and focused conformance tests.  Product callers receive
    accepted values only through :class:`DeploymentAuthority`.
    """

    accepted = object.__new__(AcceptedDesiredSnapshot)
    object.__setattr__(accepted, "environment_id", environment_id)
    object.__setattr__(accepted, "issuer", issuer)
    object.__setattr__(accepted, "authority_observation", authority_observation)
    object.__setattr__(accepted, "channel_id", channel_id)
    object.__setattr__(accepted, "head_observation", head_observation)
    object.__setattr__(accepted, "snapshot_id", snapshot_id)
    object.__setattr__(accepted, "_issuance", _ACCEPTED_DESIRED_ISSUANCE)
    accepted._validate()
    return accepted


class EffectKind(StrEnum):
    """The disjoint kinds of external effects the application can authorize."""

    RECONCILE = "reconcile"
    TEARDOWN = "teardown"


@dataclass(frozen=True)
class EffectIntent:
    """Caller-created description of one intended resource effect.

    It contains no authority, lease, or generation proof.  The effect-fencing
    port binds it to those values in an :class:`EffectAuthorization`.
    """

    kind: EffectKind
    resource_address: str
    resource_uid: str

    def __post_init__(self) -> None:
        _require_instance(self.kind, EffectKind, "kind")
        _require_opaque_value(self.resource_address, "resource_address")
        _require_opaque_value(self.resource_uid, "resource_uid")


_EFFECT_AUTHORIZATION_ISSUANCE = object()


@dataclass(frozen=True, init=False)
class EffectAuthorization:
    """Effect-fencing proof issued by the retention/fencing boundary."""

    intent: EffectIntent
    accepted_desired_snapshot: AcceptedDesiredSnapshot
    input_snapshot_id: SnapshotId
    lease_token: EffectLeaseToken
    generation: int
    _issuance: object = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("EffectAuthorization must be issued by EffectFencing")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("EffectAuthorization must not be subclassed")

    def _validate(self) -> None:
        if type(self) is not EffectAuthorization:
            raise TypeError("EffectAuthorization must not be subclassed")
        if getattr(self, "_issuance", None) is not _EFFECT_AUTHORIZATION_ISSUANCE:
            raise TypeError("EffectAuthorization has no valid fencing issuance proof")
        _require_instance(self.intent, EffectIntent, "intent")
        _require_instance(self.accepted_desired_snapshot, AcceptedDesiredSnapshot, "accepted_desired_snapshot")
        self.accepted_desired_snapshot._validate()
        _require_instance(self.input_snapshot_id, SnapshotId, "input_snapshot_id")
        _require_instance(self.lease_token, EffectLeaseToken, "lease_token")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if self.input_snapshot_id != self.accepted_desired_snapshot.snapshot_id:
            raise ValueError("effect authorization input snapshot must match the accepted desired snapshot")

    def __copy__(self) -> EffectAuthorization:
        raise TypeError("EffectAuthorization must not be copied")

    def __deepcopy__(self, _memo: object) -> EffectAuthorization:
        raise TypeError("EffectAuthorization must not be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex, /) -> str | tuple[Any, ...]:
        raise TypeError("EffectAuthorization must not be serialized")


def _issue_effect_authorization(
    intent: EffectIntent,
    accepted_desired_snapshot: AcceptedDesiredSnapshot,
    input_snapshot_id: SnapshotId,
    lease_token: EffectLeaseToken,
    generation: int,
) -> EffectAuthorization:
    """Issue effect-fencing proof for an adapter implementing ``EffectFencing``."""

    authorization = object.__new__(EffectAuthorization)
    object.__setattr__(authorization, "intent", intent)
    object.__setattr__(authorization, "accepted_desired_snapshot", accepted_desired_snapshot)
    object.__setattr__(authorization, "input_snapshot_id", input_snapshot_id)
    object.__setattr__(authorization, "lease_token", lease_token)
    object.__setattr__(authorization, "generation", generation)
    object.__setattr__(authorization, "_issuance", _EFFECT_AUTHORIZATION_ISSUANCE)
    authorization._validate()
    return authorization


class PublicationMode(StrEnum):
    """The domain-authorized mode for a publication transaction."""

    DIRECT_ACCEPTED = "direct-accepted"
    REVIEW_REQUIRED = "review-required"
    FENCED_CONTINUATION = "fenced-continuation"


@dataclass(frozen=True)
class SealedCandidate:
    """An immutable sealed candidate with both handle and exact content identity."""

    handle: SealedCandidateHandle
    snapshot_id: SnapshotId
    content_id: ContentId

    def __post_init__(self) -> None:
        _require_instance(self.handle, SealedCandidateHandle, "handle")
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")
        _require_instance(self.content_id, ContentId, "content_id")


@dataclass(frozen=True)
class RetainedSource:
    """A source snapshot and the durable handle that keeps it recoverable."""

    handle: RetainedSourceHandle
    source_snapshot_id: SourceSnapshotId

    def __post_init__(self) -> None:
        _require_instance(self.handle, RetainedSourceHandle, "handle")
        _require_instance(self.source_snapshot_id, SourceSnapshotId, "source_snapshot_id")


@dataclass(frozen=True)
class OwnershipObservation:
    """One exact ownership incarnation, including observed absence.

    The adapter changes the incarnation for every ownership update, which
    makes a ``A -> B -> A`` ownership sequence distinguishable from no change.
    """

    owner: OwnershipId | None
    incarnation: str

    def __post_init__(self) -> None:
        if self.owner is not None:
            _require_instance(self.owner, OwnershipId, "owner")
        _require_opaque_value(self.incarnation, "ownership incarnation")

    @classmethod
    def absent(cls, incarnation: str) -> OwnershipObservation:
        """Create an exact observation that no owner exists."""

        return cls(None, incarnation)

    @classmethod
    def present(cls, owner: OwnershipId, incarnation: str) -> OwnershipObservation:
        """Create an exact observation of one owner."""

        return cls(owner, incarnation)

    @property
    def is_absent(self) -> bool:
        return self.owner is None

    def to_wire(self) -> str:
        return _canonical_wire({"incarnation": self.incarnation, "owner": self.owner.to_wire() if self.owner else None})


@dataclass(frozen=True)
class SourceOwnershipRequirement:
    """Exact retained-source ownership assertion for one publication attempt."""

    retained_source: RetainedSource
    expected_ownership: OwnershipObservation

    def __post_init__(self) -> None:
        _require_instance(self.retained_source, RetainedSource, "retained_source")
        _require_instance(self.expected_ownership, OwnershipObservation, "expected_ownership")


@dataclass(frozen=True)
class PublicationIntent:
    """Durable request to publish a sealed candidate and transfer its sources.

    An adapter must commit this whole intent atomically, or durably retain it
    for verification and recovery.  This value does not claim that merely
    constructing it performs an atomic transaction.
    """

    attempt_id: PublicationAttemptId
    channel_id: ChannelId
    expected_head: HeadObservation
    candidate: SealedCandidate
    retained_sources: tuple[RetainedSource, ...]
    publication_owner: OwnershipId
    ownership_requirements: tuple[SourceOwnershipRequirement, ...]
    mode: PublicationMode
    effect_authorization: EffectAuthorization | None = None

    def __post_init__(self) -> None:
        _require_instance(self.attempt_id, PublicationAttemptId, "attempt_id")
        _require_instance(self.channel_id, ChannelId, "channel_id")
        _require_instance(self.expected_head, HeadObservation, "expected_head")
        _require_instance(self.candidate, SealedCandidate, "candidate")
        _require_instance(self.publication_owner, OwnershipId, "publication_owner")
        _require_instance(self.mode, PublicationMode, "mode")
        if self.expected_head.channel_id != self.channel_id:
            raise ValueError("publication channel must match the expected head channel")
        if not isinstance(self.retained_sources, tuple):
            raise TypeError("retained_sources must be a tuple")
        if not isinstance(self.ownership_requirements, tuple):
            raise TypeError("ownership_requirements must be a tuple")
        if any(not isinstance(source, RetainedSource) for source in self.retained_sources):
            raise TypeError("retained_sources must contain RetainedSource values")
        if any(not isinstance(requirement, SourceOwnershipRequirement) for requirement in self.ownership_requirements):
            raise TypeError("ownership_requirements must contain SourceOwnershipRequirement values")
        source_ids = tuple(source.source_snapshot_id for source in self.retained_sources)
        required_sources = tuple(
            requirement.retained_source.source_snapshot_id for requirement in self.ownership_requirements
        )
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("retained_sources must not contain duplicate source snapshots")
        if len(set(required_sources)) != len(required_sources):
            raise ValueError("ownership_requirements must not contain duplicate source snapshots")
        if set(required_sources) != set(source_ids):
            raise ValueError("ownership_requirements must cover exactly the retained sources")
        retained_by_snapshot = {source.source_snapshot_id: source for source in self.retained_sources}
        if any(
            retained_by_snapshot[requirement.retained_source.source_snapshot_id] != requirement.retained_source
            for requirement in self.ownership_requirements
        ):
            raise ValueError("ownership_requirements must use the retained source handle for each source snapshot")
        if self.mode is PublicationMode.FENCED_CONTINUATION:
            if self.effect_authorization is None:
                raise ValueError("fenced continuation publication requires an effect authorization")
            self.effect_authorization._validate()
        elif self.effect_authorization is not None:
            raise ValueError("only fenced continuation publication may include an effect authorization")


def _logical_input_label(value: object, description: str) -> str:
    label = _require_opaque_value(value, description)
    if label.startswith("/") or "\\" in label or any(segment in {"", ".", ".."} for segment in label.split("/")):
        raise ValueError(f"{description} must be a logical label, not a path")
    return label


@dataclass(frozen=True)
class ValidateCommand:
    """Validate authored resources identified by backend-neutral input labels."""

    targets: tuple[str, ...] = ()
    environments: tuple[EnvironmentId, ...] = ()
    fail_fast: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.targets, tuple):
            raise TypeError("targets must be a tuple")
        if not isinstance(self.environments, tuple):
            raise TypeError("environments must be a tuple")
        for target in self.targets:
            _logical_input_label(target, "validation target")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("validation targets must not contain duplicates")
        if any(not isinstance(environment, EnvironmentId) for environment in self.environments):
            raise TypeError("environments must contain EnvironmentId values")
        if len(set(self.environments)) != len(self.environments):
            raise ValueError("validation environments must not contain duplicates")
        if not isinstance(self.fail_fast, bool):
            raise TypeError("fail_fast must be a bool")


@dataclass(frozen=True)
class ValidationSubject:
    """Display identity for a validation subject, including invalid input.

    Unlike authored document labels, a subject is not required to be a safe
    relative path: it must be able to identify malformed environment names,
    absolute CLI inputs, and parser-level subjects without losing detail.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("validation subject must be a non-empty string")
        if "\0" in self.value:
            raise ValueError("validation subject must not contain NUL")

    def __str__(self) -> str:
        return self.value

    def to_wire(self) -> str:
        return self.value


def _human_message(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("validation issue message must be a non-empty string")
    if "\0" in value:
        raise ValueError("validation issue message must not contain NUL")
    return value


@dataclass(frozen=True)
class ValidationIssue:
    """One backend-neutral validation problem associated with a typed subject."""

    subject: ValidationSubject
    message: str
    code: str | None = None

    def __post_init__(self) -> None:
        _require_instance(self.subject, ValidationSubject, "validation issue subject")
        _human_message(self.message)
        if self.code is not None:
            _require_opaque_value(self.code, "validation issue code")


class ValidationFailFastError(Exception):
    """Typed application failure carrying the first validation issue."""

    def __init__(self, issue: ValidationIssue) -> None:
        if not isinstance(issue, ValidationIssue):
            raise TypeError("issue must be a ValidationIssue")
        self.issue = issue
        super().__init__(f"{issue.subject}: {issue.message}")


@dataclass(frozen=True)
class ValidationResult:
    """Typed result of a validation command.

    The result carries the exact logical authored documents and environments
    successfully inspected.  It deliberately has no filesystem or backend
    handles, so command adapters can render it without depending on the
    source implementation.
    """

    issues: tuple[ValidationIssue, ...] = ()
    validated_documents: tuple[str, ...] = ()
    validated_environments: tuple[EnvironmentId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.issues, tuple):
            raise TypeError("issues must be a tuple")
        if any(not isinstance(issue, ValidationIssue) for issue in self.issues):
            raise TypeError("issues must contain ValidationIssue values")
        if not isinstance(self.validated_documents, tuple):
            raise TypeError("validated_documents must be a tuple")
        for document in self.validated_documents:
            _logical_input_label(document, "validated document")
        if len(set(self.validated_documents)) != len(self.validated_documents):
            raise ValueError("validated_documents must not contain duplicates")
        if not isinstance(self.validated_environments, tuple):
            raise TypeError("validated_environments must be a tuple")
        if any(not isinstance(environment, EnvironmentId) for environment in self.validated_environments):
            raise TypeError("validated_environments must contain EnvironmentId values")
        if len(set(self.validated_environments)) != len(self.validated_environments):
            raise ValueError("validated_environments must not contain duplicates")

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class SnapshotInspectionCommand:
    """Request an immutable snapshot inspection by its backend-neutral identity."""

    snapshot_id: SnapshotId

    def __post_init__(self) -> None:
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")


@dataclass(frozen=True)
class SnapshotInspectionResult:
    """Stable identity information returned by the minimum snapshot read slice."""

    snapshot_id: SnapshotId
    content_id: ContentId

    def __post_init__(self) -> None:
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")
        _require_instance(self.content_id, ContentId, "content_id")
