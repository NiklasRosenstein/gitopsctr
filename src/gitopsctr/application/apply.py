"""Typed apply intent and result values.

The values in this module deliberately describe *what* is to be applied, not
where an incoming adapter happened to obtain it.  In particular a file name,
``Path``, Git ref, or a temporary checkout is not part of an application
command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationRecoveryLocator,
    RetainedSource,
    SnapshotId,
    SourceSnapshotId,
)
from gitopsctr.application.sources import SourceRequest, SourceSnapshot
from gitopsctr.resource_api import JsonObject

if TYPE_CHECKING:
    from gitopsctr.application.model import PublicationIntent


def _label(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\0" in value:
        raise ValueError(f"{description} must be a non-empty, trimmed display label")
    return value


_AUTHORED_DOCUMENT_ISSUANCE = object()
_AUTHORED_SOURCE_BINDINGS: dict[object, tuple[object, ...]] = {}


@dataclass(frozen=True, slots=True, init=False)
class AuthoredDocument:
    """One normalized authored document with exact incoming-byte identity."""

    origin: str
    content_id: ContentId
    _document_wire: str
    _issuance: object

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("AuthoredDocument must be issued by an AuthoredChangeDecoder")

    def _validate(self) -> None:
        if type(self) is not AuthoredDocument:
            raise TypeError("AuthoredDocument must not be subclassed")
        if self._issuance is not _AUTHORED_DOCUMENT_ISSUANCE:
            raise TypeError("AuthoredDocument has no valid decoder issuance proof")
        _label(self.origin, "authored document origin")
        if not isinstance(self.content_id, ContentId):
            raise TypeError("authored document content_id must be a ContentId")
        if not isinstance(json.loads(self._document_wire), dict):
            raise TypeError("authored document must be a JSON object")

    @property
    def document(self) -> JsonObject:
        """Return a fresh normalized document, preventing post-decode mutation."""

        document = json.loads(self._document_wire)
        assert isinstance(document, dict)
        return document


def _issue_authored_document(origin: str, document: JsonObject, content_id: ContentId) -> AuthoredDocument:
    """Issue a normalized document from an incoming authored-input adapter."""

    if not isinstance(document, dict):
        raise TypeError("authored document must be a JSON object")
    issued = object.__new__(AuthoredDocument)
    object.__setattr__(issued, "origin", origin)
    object.__setattr__(issued, "content_id", content_id)
    object.__setattr__(
        issued, "_document_wire", json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
    object.__setattr__(issued, "_issuance", _AUTHORED_DOCUMENT_ISSUANCE)
    issued._validate()
    return issued


@dataclass(frozen=True, slots=True, init=False)
class AuthoredSourceAcquisition:
    """One selector resolution retained before source-backed input is decoded.

    The decoder issues this value from the exact immutable source workspace it
    used to read authored bytes.  Apply orchestration can therefore recover and
    retain that same snapshot without resolving a moving selector a second
    time.
    """

    snapshot: SourceSnapshot
    retained: RetainedSource
    _issuance: object

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("AuthoredSourceAcquisition must be issued by an AuthoredChangeDecoder")

    def _validate(self) -> None:
        if type(self) is not AuthoredSourceAcquisition:
            raise TypeError("authored source acquisition has no valid decoder issuance proof")
        if not isinstance(self.snapshot, SourceSnapshot):
            raise TypeError("authored source snapshot must be a SourceSnapshot")
        if self.snapshot.workspace.is_mutable or self.snapshot.workspace.content_id != self.snapshot.content_id:
            raise ValueError("authored source snapshot must bind an immutable exact workspace")
        if not isinstance(self.retained, RetainedSource):
            raise TypeError("authored retained source must be a RetainedSource")
        self.retained._validate()
        if (
            self.snapshot.source_snapshot_id != self.retained.source_snapshot_id
            or self.snapshot.content_id != self.retained.content_id
        ):
            raise ValueError("authored source snapshot does not match its retained capability")
        binding = (
            self.snapshot.source_snapshot_id,
            self.snapshot.content_id,
            self.snapshot.workspace.list_entries(),
            self.retained.handle,
            self.retained.retention_store_id,
            self.retained.source_snapshot_id,
            self.retained.content_id,
        )
        if _AUTHORED_SOURCE_BINDINGS.get(self._issuance) != binding:
            raise TypeError("authored source acquisition was modified after issuance")


def _issue_authored_source_acquisition(snapshot: SourceSnapshot, retained: RetainedSource) -> AuthoredSourceAcquisition:
    """Issue exact source/retention evidence at the decoder boundary."""

    if not isinstance(snapshot, SourceSnapshot) or not isinstance(retained, RetainedSource):
        raise TypeError("authored source acquisition requires SourceSnapshot and RetainedSource values")
    retained._validate()
    acquired = object.__new__(AuthoredSourceAcquisition)
    issuance = object()
    object.__setattr__(acquired, "snapshot", snapshot)
    object.__setattr__(acquired, "retained", retained)
    object.__setattr__(acquired, "_issuance", issuance)
    _AUTHORED_SOURCE_BINDINGS[issuance] = (
        snapshot.source_snapshot_id,
        snapshot.content_id,
        snapshot.workspace.list_entries(),
        retained.handle,
        retained.retention_store_id,
        retained.source_snapshot_id,
        retained.content_id,
    )
    acquired._validate()
    return acquired


@dataclass(frozen=True, slots=True)
class AuthoredChangeSet:
    """One fully decoded, duplicate-free explicit authoring operation.

    When the incoming selection was source-backed, ``source_snapshot_id`` is
    the exact source snapshot resolved by the decoder.  An apply service must
    use that immutable identity, never resolve the original selector again.
    """

    documents: tuple[AuthoredDocument, ...]
    source_snapshot_id: SourceSnapshotId | None = None
    source_acquisition: AuthoredSourceAcquisition | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.documents, tuple):
            raise TypeError("authored documents must be a tuple")
        if self.source_snapshot_id is not None and not isinstance(self.source_snapshot_id, SourceSnapshotId):
            raise TypeError("source_snapshot_id must be a SourceSnapshotId or None")
        if self.source_acquisition is not None:
            if not isinstance(self.source_acquisition, AuthoredSourceAcquisition):
                raise TypeError("source_acquisition must be an AuthoredSourceAcquisition or None")
            self.source_acquisition._validate()
            if self.source_snapshot_id != self.source_acquisition.snapshot.source_snapshot_id:
                raise ValueError("source acquisition must match source_snapshot_id")
        elif self.source_snapshot_id is None:
            pass
        if any(not isinstance(document, AuthoredDocument) for document in self.documents):
            raise TypeError("authored documents must contain AuthoredDocument values")
        identities: set[tuple[str, str]] = set()
        for item in self.documents:
            item._validate()
            api_version = item.document.get("apiVersion")
            kind = item.document.get("kind")
            metadata = item.document.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if not all(isinstance(value, str) and value for value in (api_version, kind, name)):
                raise ValueError(f"{item.origin}: resource requires apiVersion, kind, and metadata.name")
            assert isinstance(kind, str)
            assert isinstance(name, str)
            family = "stack" if kind == "Stack" else ("stacktemplate" if kind == "StackTemplate" else "unit")
            identity = (family, name)
            if identity in identities:
                raise ValueError(f"duplicate authored resource {identity!r}")
            identities.add(identity)


@dataclass(frozen=True, slots=True)
class ApplyCommand:
    """Backend-neutral intent to project an authored change set."""

    environment_id: EnvironmentId
    input_labels: tuple[str, ...]
    desired_channel: ChannelId | None
    observed_channel: ChannelId | None
    candidate_channel: ChannelId | None
    source_request: SourceRequest | None
    partition: str | None = None
    dry_run: bool = False
    verbose: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.environment_id, EnvironmentId):
            raise TypeError("environment_id must be an EnvironmentId")
        if not isinstance(self.input_labels, tuple):
            raise TypeError("input_labels must be a tuple")
        for label in self.input_labels:
            _label(label, "authored input label")
        if self.desired_channel is not None and not isinstance(self.desired_channel, ChannelId):
            raise TypeError("desired_channel must be a ChannelId or None")
        if self.observed_channel is not None and not isinstance(self.observed_channel, ChannelId):
            raise TypeError("observed_channel must be a ChannelId or None")
        if self.candidate_channel is not None and not isinstance(self.candidate_channel, ChannelId):
            raise TypeError("candidate_channel must be a ChannelId or None")
        if self.source_request is not None and not isinstance(self.source_request, SourceRequest):
            raise TypeError("source_request must be a SourceRequest or None")
        if self.partition is not None:
            _label(self.partition, "partition")
        if not isinstance(self.dry_run, bool) or not isinstance(self.verbose, bool):
            raise TypeError("dry_run and verbose must be bool")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Closed application result for an apply attempt."""

    snapshot_id: SnapshotId | None
    publication_mode: PublicationMode | None
    publication: PublicationIntent | None = None
    publication_outcome: PublicationOutcome | None = None
    recovery_locator: PublicationRecoveryLocator | None = None

    def __post_init__(self) -> None:
        if self.snapshot_id is not None and not isinstance(self.snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId or None")
        if self.publication_mode is not None and not isinstance(self.publication_mode, PublicationMode):
            raise TypeError("publication_mode must be a PublicationMode or None")
        if self.publication is not None:
            # Import at runtime to avoid model/apply import cycles in type-only use.
            from gitopsctr.application.model import PublicationIntent

            if not isinstance(self.publication, PublicationIntent):
                raise TypeError("publication must be a PublicationIntent or None")
            self.publication._validate()
            if self.publication_mode != self.publication.mode:
                raise ValueError("apply result publication mode must match the publication intent")
            if not isinstance(self.publication_outcome, PublicationOutcome):
                raise ValueError("a publication intent requires its exact publication outcome")
            if not isinstance(self.recovery_locator, PublicationRecoveryLocator):
                raise ValueError("a publication intent requires a durable recovery locator")
            if self.recovery_locator.attempt_id != self.publication.attempt_id:
                raise ValueError("publication recovery locator must bind the exact attempt")
            self.publication_outcome._validate()
            if self.publication_outcome.state is PublicationOutcomeState.COMMITTED:
                proof = self.publication_outcome.proof
                assert proof is not None
                if proof.intent != self.publication:
                    raise ValueError("committed apply result proof must bind the exact publication intent")
                if self.snapshot_id != self.publication.candidate.snapshot_id:
                    raise ValueError("committed apply result snapshot must be the sealed publication candidate")
            elif self.snapshot_id is not None:
                raise ValueError("an uncommitted or unknown publication cannot return an accepted snapshot")
        elif self.publication_mode is not None:
            raise ValueError("an apply result publication mode requires a publication intent")
        elif self.publication_outcome is not None:
            raise ValueError("a publication outcome requires its exact publication intent")
        elif self.recovery_locator is not None:
            raise ValueError("a publication recovery locator requires its exact publication intent")
