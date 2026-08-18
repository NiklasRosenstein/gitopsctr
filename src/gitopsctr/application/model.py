"""Backend-neutral application vocabulary.

The application layer uses these values to preserve the identity and fencing
guarantees of a deployment without admitting storage, transport, or CLI
concepts into its contracts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from enum import StrEnum
from secrets import token_bytes, token_hex
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
class CandidateStoreId(_OpaqueValue):
    """Opaque identity of the candidate store that issued a sealed candidate."""


@dataclass(frozen=True)
class PublicationStoreId(_OpaqueValue):
    """Opaque identity of the transaction store that issued a publication proof."""


@dataclass(frozen=True)
class PublicationProofId(_OpaqueValue):
    """Unpredictable identifier for one authenticated publication proof."""


@dataclass(frozen=True)
class RetainedSourceHandle(_OpaqueValue):
    """Durable retention handle through which one source snapshot can be restored."""


@dataclass(frozen=True)
class RetentionStoreId(_OpaqueValue):
    """Opaque identity of the retention boundary that issued a handle."""


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
        self._validate()

    def _validate(self) -> None:
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
        self._validate()

    def _validate(self) -> None:
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
        self.head_observation._validate()
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
        self._validate()

    def _validate(self) -> None:
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
        self.intent._validate()
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


def _effect_authorization_wire_data(authorization: EffectAuthorization | None) -> dict[str, object] | None:
    """Canonicalize all effect-fencing evidence that can bind publication."""

    if authorization is None:
        return None
    authorization._validate()
    return {
        "accepted": authorization.accepted_desired_snapshot._wire_data(),
        "generation": authorization.generation,
        "input_snapshot": authorization.input_snapshot_id.to_wire(),
        "intent": {
            "kind": authorization.intent.kind.value,
            "resource_address": authorization.intent.resource_address,
            "resource_uid": authorization.intent.resource_uid,
        },
        "lease": authorization.lease_token.to_wire(),
    }


class PublicationMode(StrEnum):
    """The domain-authorized mode for a publication transaction."""

    DIRECT_ACCEPTED = "direct-accepted"
    REVIEW_REQUIRED = "review-required"
    FENCED_CONTINUATION = "fenced-continuation"


class PublicationTarget(StrEnum):
    """Whether an intent publishes accepted state or only a review candidate."""

    ACCEPTED_DESIRED = "accepted-desired"
    REVIEW_CANDIDATE = "review-candidate"


class PublicationOutcomeState(StrEnum):
    """The definite-or-ambiguous result of executing a publication attempt."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not-committed"
    UNKNOWN = "unknown"


_SEALED_CANDIDATE_ISSUANCE = object()


@dataclass(frozen=True, init=False)
class SealedCandidate:
    """An immutable candidate issued only by a candidate-store boundary."""

    handle: SealedCandidateHandle
    candidate_store_id: CandidateStoreId
    snapshot_id: SnapshotId
    content_id: ContentId
    _issuance: object = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("SealedCandidate must be issued by a CandidateStore")

    def _validate(self) -> None:
        if type(self) is not SealedCandidate:
            raise TypeError("SealedCandidate must not be subclassed")
        if getattr(self, "_issuance", None) is not _SEALED_CANDIDATE_ISSUANCE:
            raise TypeError("SealedCandidate has no valid candidate-store issuance proof")
        _require_instance(self.handle, SealedCandidateHandle, "handle")
        _require_instance(self.candidate_store_id, CandidateStoreId, "candidate_store_id")
        _require_instance(self.snapshot_id, SnapshotId, "snapshot_id")
        _require_instance(self.content_id, ContentId, "content_id")

    def __copy__(self) -> SealedCandidate:
        raise TypeError("SealedCandidate must not be copied")

    def __deepcopy__(self, _memo: object) -> SealedCandidate:
        raise TypeError("SealedCandidate must not be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex, /) -> str | tuple[Any, ...]:
        raise TypeError("SealedCandidate must not be serialized")


def _issue_sealed_candidate(
    handle: SealedCandidateHandle,
    candidate_store_id: CandidateStoreId,
    snapshot_id: SnapshotId,
    content_id: ContentId,
) -> SealedCandidate:
    """Issue a sealed candidate for an adapter implementing ``CandidateStore``."""

    candidate = object.__new__(SealedCandidate)
    object.__setattr__(candidate, "handle", handle)
    object.__setattr__(candidate, "candidate_store_id", candidate_store_id)
    object.__setattr__(candidate, "snapshot_id", snapshot_id)
    object.__setattr__(candidate, "content_id", content_id)
    object.__setattr__(candidate, "_issuance", _SEALED_CANDIDATE_ISSUANCE)
    candidate._validate()
    return candidate


_RETAINED_SOURCE_ISSUANCE = object()


@dataclass(frozen=True, init=False)
class RetainedSource:
    """A durable source-retention capability issued by its owning boundary."""

    handle: RetainedSourceHandle
    retention_store_id: RetentionStoreId
    source_snapshot_id: SourceSnapshotId
    content_id: ContentId
    _issuance: object = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("RetainedSource must be issued by SourceRetention")

    def _validate(self) -> None:
        if type(self) is not RetainedSource:
            raise TypeError("RetainedSource must not be subclassed")
        if getattr(self, "_issuance", None) is not _RETAINED_SOURCE_ISSUANCE:
            raise TypeError("RetainedSource has no valid retention issuance proof")
        _require_instance(self.handle, RetainedSourceHandle, "handle")
        _require_instance(self.retention_store_id, RetentionStoreId, "retention_store_id")
        _require_instance(self.source_snapshot_id, SourceSnapshotId, "source_snapshot_id")
        _require_instance(self.content_id, ContentId, "content_id")

    def __copy__(self) -> RetainedSource:
        raise TypeError("RetainedSource must not be copied")

    def __deepcopy__(self, _memo: object) -> RetainedSource:
        raise TypeError("RetainedSource must not be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex, /) -> str | tuple[Any, ...]:
        raise TypeError("RetainedSource must not be serialized")


def _issue_retained_source(
    handle: RetainedSourceHandle,
    retention_store_id: RetentionStoreId,
    source_snapshot_id: SourceSnapshotId,
    content_id: ContentId,
) -> RetainedSource:
    """Issue a retained source for an adapter implementing ``SourceRetention``."""

    retained = object.__new__(RetainedSource)
    object.__setattr__(retained, "handle", handle)
    object.__setattr__(retained, "retention_store_id", retention_store_id)
    object.__setattr__(retained, "source_snapshot_id", source_snapshot_id)
    object.__setattr__(retained, "content_id", content_id)
    object.__setattr__(retained, "_issuance", _RETAINED_SOURCE_ISSUANCE)
    retained._validate()
    return retained


@dataclass(frozen=True)
class OwnershipObservation:
    """One exact ownership incarnation, including observed absence.

    The adapter changes the incarnation for every ownership update, which
    makes a ``A -> B -> A`` ownership sequence distinguishable from no change.
    """

    owner: OwnershipId | None
    incarnation: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
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
class SourceOwnershipChange:
    """An exact source-keyed claim, transfer, or release in one transaction."""

    retained_source: RetainedSource
    expected_ownership: OwnershipObservation
    next_owner: OwnershipId | None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self) is not SourceOwnershipChange:
            raise TypeError("SourceOwnershipChange must not be subclassed")
        _require_instance(self.retained_source, RetainedSource, "retained_source")
        self.retained_source._validate()
        _require_instance(self.expected_ownership, OwnershipObservation, "expected_ownership")
        self.expected_ownership._validate()
        if self.next_owner is not None:
            _require_instance(self.next_owner, OwnershipId, "next_owner")


@dataclass(frozen=True)
class CoordinationObservation:
    """One exact incarnation-fenced observation of adapter coordination state."""

    value: str | None
    incarnation: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.value is not None:
            _require_opaque_value(self.value, "coordination value")
        _require_opaque_value(self.incarnation, "coordination incarnation")

    @classmethod
    def absent(cls, incarnation: str) -> CoordinationObservation:
        return cls(None, incarnation)

    @classmethod
    def present(cls, value: str, incarnation: str) -> CoordinationObservation:
        return cls(value, incarnation)


@dataclass(frozen=True)
class CoordinationChange:
    """An adapter-owned ABA-fenced coordination change in one transaction."""

    key: str
    expected: CoordinationObservation
    next_value: str | None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self) is not CoordinationChange:
            raise TypeError("CoordinationChange must not be subclassed")
        _require_opaque_value(self.key, "coordination key")
        _require_instance(self.expected, CoordinationObservation, "expected coordination observation")
        self.expected._validate()
        if self.next_value is not None:
            _require_opaque_value(self.next_value, "next coordination value")
        if self.expected.value == self.next_value:
            raise ValueError("coordination changes must change their value")


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
    source_ownership_changes: tuple[SourceOwnershipChange, ...]
    publication_owner: OwnershipId
    coordination_changes: tuple[CoordinationChange, ...]
    target: PublicationTarget
    mode: PublicationMode
    effect_authorization: EffectAuthorization | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        """Revalidate the complete mutable-object graph at a proof boundary."""

        if type(self) is not PublicationIntent:
            raise TypeError("PublicationIntent must not be subclassed")
        _require_instance(self.attempt_id, PublicationAttemptId, "attempt_id")
        _require_instance(self.channel_id, ChannelId, "channel_id")
        _require_instance(self.expected_head, HeadObservation, "expected_head")
        self.expected_head._validate()
        _require_instance(self.candidate, SealedCandidate, "candidate")
        self.candidate._validate()
        _require_instance(self.publication_owner, OwnershipId, "publication_owner")
        _require_instance(self.target, PublicationTarget, "target")
        _require_instance(self.mode, PublicationMode, "mode")
        if self.expected_head.channel_id != self.channel_id:
            raise ValueError("publication channel must match the expected head channel")
        if not isinstance(self.source_ownership_changes, tuple):
            raise TypeError("source_ownership_changes must be a tuple")
        if not isinstance(self.coordination_changes, tuple):
            raise TypeError("coordination_changes must be a tuple")
        if any(type(change) is not SourceOwnershipChange for change in self.source_ownership_changes):
            raise TypeError("source_ownership_changes must contain SourceOwnershipChange values")
        if any(type(change) is not CoordinationChange for change in self.coordination_changes):
            raise TypeError("coordination_changes must contain CoordinationChange values")
        for change in self.source_ownership_changes:
            change._validate()
        for change in self.coordination_changes:
            change._validate()
        source_ids = tuple(change.retained_source.source_snapshot_id for change in self.source_ownership_changes)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ownership_changes must be source-keyed and unique")
        if self.source_ownership_changes and not any(
            change.expected_ownership.owner == self.publication_owner or change.next_owner == self.publication_owner
            for change in self.source_ownership_changes
        ):
            raise ValueError("publication_owner must participate in a source ownership claim, transfer, or release")
        coordination_keys = tuple(change.key for change in self.coordination_changes)
        if len(set(coordination_keys)) != len(coordination_keys):
            raise ValueError("coordination_changes must have unique keys")
        if self.mode is PublicationMode.REVIEW_REQUIRED and self.target is not PublicationTarget.REVIEW_CANDIDATE:
            raise ValueError("review-required publication must target a review candidate")
        if self.mode is not PublicationMode.REVIEW_REQUIRED and self.target is not PublicationTarget.ACCEPTED_DESIRED:
            raise ValueError("accepted publication mode must target accepted desired state")
        if self.mode is PublicationMode.FENCED_CONTINUATION:
            if self.effect_authorization is None:
                raise ValueError("fenced continuation publication requires an effect authorization")
            self.effect_authorization._validate()
        elif self.effect_authorization is not None:
            raise ValueError("only fenced continuation publication may include an effect authorization")

    def _wire_data(self) -> dict[str, object]:
        """Return canonical full-intent evidence for a transaction proof."""

        self._validate()
        return {
            "attempt": self.attempt_id.to_wire(),
            "candidate": {
                "content": self.candidate.content_id.to_wire(),
                "handle": self.candidate.handle.to_wire(),
                "snapshot": self.candidate.snapshot_id.to_wire(),
                "store": self.candidate.candidate_store_id.to_wire(),
            },
            "channel": self.channel_id.to_wire(),
            "coordination": [
                {
                    "expected": {"incarnation": change.expected.incarnation, "value": change.expected.value},
                    "key": change.key,
                    "next": change.next_value,
                }
                for change in self.coordination_changes
            ],
            "effect_authorization": _effect_authorization_wire_data(self.effect_authorization),
            "expected_head": self.expected_head._wire_data(),
            "mode": self.mode.value,
            "owner": self.publication_owner.to_wire(),
            "ownership": [
                {
                    "expected": {
                        "incarnation": change.expected_ownership.incarnation,
                        "owner": (
                            change.expected_ownership.owner.to_wire()
                            if change.expected_ownership.owner is not None
                            else None
                        ),
                    },
                    "next": change.next_owner.to_wire() if change.next_owner is not None else None,
                    "retained": {
                        "content": change.retained_source.content_id.to_wire(),
                        "handle": change.retained_source.handle.to_wire(),
                        "snapshot": change.retained_source.source_snapshot_id.snapshot_id.to_wire(),
                        "source": change.retained_source.source_snapshot_id.source_id.to_wire(),
                        "store": change.retained_source.retention_store_id.to_wire(),
                    },
                }
                for change in self.source_ownership_changes
            ],
            "target": self.target.value,
        }


@dataclass(frozen=True)
class SourceOwnershipResult:
    """Keyed evidence for one requested source ownership mutation."""

    source_snapshot_id: SourceSnapshotId
    requested_next_owner: OwnershipId | None
    resulting_observation: OwnershipObservation

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        """Revalidate all nested evidence after an untrusted boundary."""

        if type(self) is not SourceOwnershipResult:
            raise TypeError("SourceOwnershipResult must not be subclassed")
        _require_instance(self.source_snapshot_id, SourceSnapshotId, "source_snapshot_id")
        if self.requested_next_owner is not None:
            _require_instance(self.requested_next_owner, OwnershipId, "requested_next_owner")
        _require_instance(self.resulting_observation, OwnershipObservation, "resulting_observation")
        if self.resulting_observation.owner != self.requested_next_owner:
            raise ValueError("ownership result must contain the requested next owner")


@dataclass(frozen=True)
class CoordinationResult:
    """Keyed evidence for one requested coordination mutation."""

    key: str
    requested_next_value: str | None
    resulting_observation: CoordinationObservation

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        """Revalidate all nested evidence after an untrusted boundary."""

        if type(self) is not CoordinationResult:
            raise TypeError("CoordinationResult must not be subclassed")
        _require_opaque_value(self.key, "coordination result key")
        if self.requested_next_value is not None:
            _require_opaque_value(self.requested_next_value, "requested next coordination value")
        _require_instance(self.resulting_observation, CoordinationObservation, "resulting coordination observation")
        if self.resulting_observation.value != self.requested_next_value:
            raise ValueError("coordination result must contain the requested next value")


@dataclass(frozen=True)
class _PublicationProofIssuer:
    """Private, strongly-retained signing material for one transaction store."""

    token: object
    secret: bytes


_PUBLICATION_PROOF_ISSUERS: dict[str, _PublicationProofIssuer] = {}


def _new_publication_proof_issuer(publication_store_id: PublicationStoreId) -> object:
    """Register private signing material for one concrete transaction store.

    The registry intentionally outlives a store instance: a committed proof
    must remain verifiable after adapter close, garbage collection, or process
    object churn. Store IDs are unpredictable and may never be re-registered.
    """

    _require_instance(publication_store_id, PublicationStoreId, "publication_store_id")
    if publication_store_id.value in _PUBLICATION_PROOF_ISSUERS:
        raise ValueError("publication store ID already has a proof issuer")
    token = object()
    _PUBLICATION_PROOF_ISSUERS[publication_store_id.value] = _PublicationProofIssuer(token, token_bytes(32))
    return token


def _publication_proof_issuer(publication_store_id: PublicationStoreId, issuer: object) -> _PublicationProofIssuer:
    """Return the one registered issuer only when its private token matches."""

    _require_instance(publication_store_id, PublicationStoreId, "publication_store_id")
    record = _PUBLICATION_PROOF_ISSUERS.get(publication_store_id.value)
    if record is None or issuer is not record.token:
        raise TypeError("PublicationProof has no valid transaction-store issuance proof")
    return record


@dataclass(frozen=True, init=False)
class PublicationProof:
    """Store-issued durable evidence that the complete intent committed."""

    intent: PublicationIntent
    publication_store_id: PublicationStoreId
    proof_id: PublicationProofId
    resulting_head: HeadObservation
    ownership_results: tuple[SourceOwnershipResult, ...]
    coordination_results: tuple[CoordinationResult, ...]
    _signature: str = field(init=False, repr=False)
    _issuance: object = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("PublicationProof must be issued by PublicationTransaction")

    def _validate(self) -> None:
        if type(self) is not PublicationProof:
            raise TypeError("PublicationProof must not be subclassed")
        _require_instance(self.intent, PublicationIntent, "intent")
        self.intent._validate()
        _require_instance(self.publication_store_id, PublicationStoreId, "publication_store_id")
        _require_instance(self.proof_id, PublicationProofId, "proof_id")
        issuer = _publication_proof_issuer(self.publication_store_id, getattr(self, "_issuance", None))
        _require_instance(self.resulting_head, HeadObservation, "resulting_head")
        self.resulting_head._validate()
        if self.resulting_head.channel_id != self.intent.channel_id:
            raise ValueError("publication proof head must belong to the intent channel")
        if self.resulting_head.snapshot_id != self.intent.candidate.snapshot_id:
            raise ValueError("publication proof head must contain the sealed candidate snapshot")
        if self.resulting_head.incarnation == self.intent.expected_head.incarnation:
            raise ValueError("publication proof head must advance the expected head incarnation")
        if not isinstance(self.ownership_results, tuple):
            raise TypeError("ownership_results must be a tuple")
        if any(type(result) is not SourceOwnershipResult for result in self.ownership_results):
            raise TypeError("ownership_results must contain SourceOwnershipResult values")
        for result in self.ownership_results:
            result._validate()
        expected_sources = tuple(
            change.retained_source.source_snapshot_id for change in self.intent.source_ownership_changes
        )
        actual_sources = tuple(result.source_snapshot_id for result in self.ownership_results)
        if actual_sources != expected_sources:
            raise ValueError("publication proof ownership results must cover ordered source changes exactly")
        expected_owners = tuple(change.next_owner for change in self.intent.source_ownership_changes)
        if tuple(result.requested_next_owner for result in self.ownership_results) != expected_owners:
            raise ValueError("publication proof ownership results must bind requested next owners")
        if any(
            result.resulting_observation.incarnation == change.expected_ownership.incarnation
            for result, change in zip(self.ownership_results, self.intent.source_ownership_changes, strict=True)
        ):
            raise ValueError("publication proof ownership results must advance every expected ownership incarnation")
        if not isinstance(self.coordination_results, tuple):
            raise TypeError("coordination_results must be a tuple")
        if any(type(result) is not CoordinationResult for result in self.coordination_results):
            raise TypeError("coordination_results must contain CoordinationResult values")
        for result in self.coordination_results:
            result._validate()
        expected_keys = tuple(change.key for change in self.intent.coordination_changes)
        if tuple(result.key for result in self.coordination_results) != expected_keys:
            raise ValueError("publication proof coordination results must cover ordered changes exactly")
        expected_values = tuple(change.next_value for change in self.intent.coordination_changes)
        if tuple(result.requested_next_value for result in self.coordination_results) != expected_values:
            raise ValueError("publication proof coordination results must bind requested next values")
        if any(
            result.resulting_observation.incarnation == change.expected.incarnation
            for result, change in zip(self.coordination_results, self.intent.coordination_changes, strict=True)
        ):
            raise ValueError(
                "publication proof coordination results must advance every expected coordination incarnation"
            )
        if not isinstance(self._signature, str) or not hmac.compare_digest(
            self._signature,
            hmac.new(
                issuer.secret, _canonical_wire(self._authenticated_wire_data()).encode(), hashlib.sha256
            ).hexdigest(),
        ):
            raise ValueError("publication proof authentication does not match its issued evidence")

    def _authenticated_wire_data(self) -> dict[str, object]:
        """Canonical complete evidence authenticated by the private store key."""

        return {
            "coordination_results": [
                {
                    "key": result.key,
                    "requested_next": result.requested_next_value,
                    "result": {
                        "incarnation": result.resulting_observation.incarnation,
                        "value": result.resulting_observation.value,
                    },
                }
                for result in self.coordination_results
            ],
            "intent": self.intent._wire_data(),
            "ownership_results": [
                {
                    "requested_next": (
                        result.requested_next_owner.to_wire() if result.requested_next_owner is not None else None
                    ),
                    "result": {
                        "incarnation": result.resulting_observation.incarnation,
                        "owner": (
                            result.resulting_observation.owner.to_wire()
                            if result.resulting_observation.owner is not None
                            else None
                        ),
                    },
                    "snapshot": result.source_snapshot_id.snapshot_id.to_wire(),
                    "source": result.source_snapshot_id.source_id.to_wire(),
                }
                for result in self.ownership_results
            ],
            "proof": self.proof_id.to_wire(),
            "resulting_head": self.resulting_head._wire_data(),
            "store": self.publication_store_id.to_wire(),
        }

    def __copy__(self) -> PublicationProof:
        raise TypeError("PublicationProof must not be copied")

    def __deepcopy__(self, _memo: object) -> PublicationProof:
        raise TypeError("PublicationProof must not be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex, /) -> str | tuple[Any, ...]:
        raise TypeError("PublicationProof must not be serialized")


def _issue_publication_proof(
    publication_store_id: PublicationStoreId,
    issuer: object,
    intent: PublicationIntent,
    resulting_head: HeadObservation,
    ownership_results: tuple[SourceOwnershipResult, ...],
    coordination_results: tuple[CoordinationResult, ...],
) -> PublicationProof:
    """Issue proof for an adapter implementing ``PublicationTransaction``."""

    proof = object.__new__(PublicationProof)
    issuer_record = _publication_proof_issuer(publication_store_id, issuer)
    object.__setattr__(proof, "intent", intent)
    object.__setattr__(proof, "publication_store_id", publication_store_id)
    object.__setattr__(proof, "proof_id", PublicationProofId(f"publication-proof:{token_hex(32)}"))
    object.__setattr__(proof, "resulting_head", resulting_head)
    object.__setattr__(proof, "ownership_results", ownership_results)
    object.__setattr__(proof, "coordination_results", coordination_results)
    object.__setattr__(
        proof,
        "_signature",
        hmac.new(
            issuer_record.secret, _canonical_wire(proof._authenticated_wire_data()).encode(), hashlib.sha256
        ).hexdigest(),
    )
    object.__setattr__(proof, "_issuance", issuer)
    proof._validate()
    return proof


@dataclass(frozen=True)
class PublicationOutcome:
    """Result of execution or verification without treating ambiguity as success."""

    state: PublicationOutcomeState
    proof: PublicationProof | None = None

    def __post_init__(self) -> None:
        _require_instance(self.state, PublicationOutcomeState, "state")
        if self.proof is not None:
            _require_instance(self.proof, PublicationProof, "proof")
            self.proof._validate()
        if self.state is PublicationOutcomeState.COMMITTED and self.proof is None:
            raise ValueError("a committed publication outcome requires a proof")
        if self.state is not PublicationOutcomeState.COMMITTED and self.proof is not None:
            raise ValueError("only a committed publication outcome may include a proof")


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
