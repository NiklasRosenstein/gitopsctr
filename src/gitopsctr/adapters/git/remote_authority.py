"""Signed client adapter for a controlled shared Git publication authority.

The remote protocol carries only canonical JSON data.  Application capabilities
are reconstructed locally *after* an Ed25519 signature, request nonce, service
identity, stable store identities, and advertised capabilities have all been
verified.  The server's private publication HMAC is never distributed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import stat
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gitopsctr.application.apply import ApplyCommand, ApplyResult, AuthoredChangeSet
from gitopsctr.application.apply_orchestration import ApplyCoordinator, ApplyPublicationIdentityIssuer
from gitopsctr.application.apply_projection import (
    IssuedRootIdentity,
    RootIdentityRequest,
    RootIncarnationIssuer,
    _issue_root_identity,
)
from gitopsctr.application.model import (
    CandidateStoreId,
    ChannelId,
    ContentId,
    CoordinationChange,
    CoordinationObservation,
    CoordinationResult,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationProofId,
    PublicationRecovery,
    PublicationRecoveryLocator,
    PublicationStoreId,
    PublicationTarget,
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SealedCandidate,
    SealedCandidateHandle,
    SnapshotId,
    SourceId,
    SourceOwnershipChange,
    SourceOwnershipResult,
    SourceSnapshotId,
    _issue_publication_proof,
    _issue_retained_source,
    _issue_review_acceptance_observation,
    _issue_sealed_candidate,
    _open_publication_proof_issuer,
)
from gitopsctr.application.ports import PublicationExecutionUnknownError, PublicationRecoveryNotFoundError
from gitopsctr.application.review_adoption import (
    ReviewAdoptionCommand,
    ReviewAdoptionCoordinator,
    ReviewAdoptionResult,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.sources import (
    RetainedSourceLocator,
    SourceRepository,
    SourceRequest,
    SourceRetentionError,
    SourceSnapshot,
)
from gitopsctr.application.workspace import (
    ImmutableWorkspace,
    InMemoryWorkspace,
    MutableWorkspace,
    WorkspaceCapabilities,
    WorkspaceEntry,
    WorkspaceEntryKind,
)

_PROTOCOL = "gitopsctr-authority/v1"
_GIT_SNAPSHOT_PREFIX = "git-commit:"
_MAX_RESPONSE_BYTES = 128 * 1024 * 1024
REQUIRED_AUTHORITY_CAPABILITIES = frozenset(
    {
        "candidate-store-v1",
        "coordination-v1",
        "identity-issuance-v1",
        "publication-v1",
        "review-adoption-v1",
        "snapshot-read-v1",
        "source-retention-v1",
    }
)
_CLIENT_ISSUER_LOCK = RLock()
_CLIENT_ISSUER_SECRETS: dict[str, bytes] = {}


class RemoteAuthorityError(ValueError):
    """A controlled authority request or authenticated response is invalid."""


class RemoteAuthorityUnavailableError(RemoteAuthorityError):
    """The configured controlled authority transport cannot be reached safely."""


class RemotePublicationExecutionUnknownError(RemoteAuthorityError, PublicationExecutionUnknownError):
    """The server reports that publication may have crossed its commit point."""


class AuthorityHttpTransport(Protocol):
    """Opaque HTTPS request transport; authentication can be supplied externally."""

    @property
    def caller_authenticated(self) -> bool: ...

    def post(self, endpoint: str, request: bytes) -> bytes: ...

    def close(self) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass(slots=True)
class UrllibAuthorityHttpTransport:
    """Mutual-TLS HTTPS transport configured by an operator-owned composition.

    The supplied context must already contain the client certificate/private
    key and trust roots.  Neither credential is read from Project content.
    """

    ssl_context: ssl.SSLContext
    client_identity: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.ssl_context, ssl.SSLContext):
            raise TypeError("controlled authority transport requires an operator-issued TLS context")
        if (
            not isinstance(self.client_identity, str)
            or not self.client_identity
            or self.client_identity != self.client_identity.strip()
        ):
            raise ValueError("controlled authority transport requires an authenticated client identity")

    @property
    def caller_authenticated(self) -> bool:
        return True

    def post(self, endpoint: str, request: bytes) -> bytes:
        if not endpoint.startswith("https://"):
            raise RemoteAuthorityUnavailableError("controlled authority transport requires HTTPS")
        message = urllib.request.Request(
            endpoint,
            data=request,
            headers={"Content-Type": "application/vnd.gitopsctr.authority+json;version=1"},
            method="POST",
        )
        opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=self.ssl_context),
        )
        try:
            with opener.open(message, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > _MAX_RESPONSE_BYTES:
                    raise RemoteAuthorityUnavailableError("controlled authority response is too large")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RemoteAuthorityUnavailableError("controlled authority request failed") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise RemoteAuthorityUnavailableError("controlled authority response is too large")
        return body

    def close(self) -> None:
        """urllib owns no persistent client session."""


def authenticated_transport_from_git_config(repository: Path) -> UrllibAuthorityHttpTransport:
    """Load operator-owned mTLS credential paths from local Git configuration."""

    if not isinstance(repository, Path):
        raise TypeError("repository must be a Path")
    values = {
        name: _git_config_value(repository, f"gitopsctr.authority.{name}")
        for name in ("clientCertificate", "clientKey", "caBundle", "clientIdentity")
    }
    if any(values[name] is None for name in ("clientCertificate", "clientKey", "clientIdentity")):
        raise RemoteAuthorityUnavailableError(
            "controlled authority requires local Git config for clientCertificate, clientKey, and clientIdentity"
        )
    certificate = _credential_file(cast(str, values["clientCertificate"]), "client certificate")
    private_key = _credential_file(cast(str, values["clientKey"]), "client private key", private=True)
    ca_bundle = _credential_file(values["caBundle"], "authority CA bundle") if values["caBundle"] is not None else None
    try:
        context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle is not None else None)
        context.load_cert_chain(str(certificate), str(private_key))
    except (OSError, ssl.SSLError) as exc:
        raise RemoteAuthorityUnavailableError("controlled authority mTLS credentials cannot be loaded") from exc
    return UrllibAuthorityHttpTransport(context, cast(str, values["clientIdentity"]))


@dataclass(frozen=True, slots=True)
class Ed25519EnvelopeVerifier:
    """Pinned verification key for one configured authority key identity."""

    key_id: str
    public_key: bytes
    _verifier: Ed25519PublicKey = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id or self.key_id != self.key_id.strip():
            raise ValueError("authority verification key ID must be canonical text")
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise ValueError("authority Ed25519 public key must contain exactly 32 bytes")
        object.__setattr__(self, "_verifier", Ed25519PublicKey.from_public_bytes(self.public_key))

    def verify(self, message: bytes, signature: bytes) -> None:
        try:
            self._verifier.verify(signature, message)
        except InvalidSignature as exc:
            raise RemoteAuthorityError("controlled authority response signature is invalid") from exc


@dataclass(frozen=True, slots=True)
class RemoteAuthorityHandshake:
    protocol: str
    authority_id: str
    key_id: str
    publication_store_id: PublicationStoreId
    candidate_store_id: CandidateStoreId
    retention_store_id: RetentionStoreId
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if self.protocol != _PROTOCOL:
            raise RemoteAuthorityError("controlled authority protocol is unsupported")
        if not isinstance(self.authority_id, str) or not self.authority_id:
            raise RemoteAuthorityError("controlled authority identity is missing")
        if not isinstance(self.key_id, str) or not self.key_id:
            raise RemoteAuthorityError("controlled authority key identity is missing")
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(value, str) or not value for value in self.capabilities
        ):
            raise RemoteAuthorityError("controlled authority capabilities are malformed")


@dataclass(slots=True)
class VerifiedAuthoritySession:
    """Nonce-bound, signature-verifying session for one expected authority."""

    endpoint: str
    expected_authority_id: str
    verifier: Ed25519EnvelopeVerifier
    transport: AuthorityHttpTransport
    _handshake: RemoteAuthorityHandshake | None = field(default=None, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def handshake(self) -> RemoteAuthorityHandshake:
        with self._lock:
            if self._closed:
                raise RemoteAuthorityUnavailableError("controlled authority session is closed")
            if self.transport.caller_authenticated is not True:
                raise RemoteAuthorityUnavailableError(
                    "controlled authority requires an operator-configured authenticated transport"
                )
            if self._handshake is None:
                result = self._exchange_unpinned("handshake", {})
                handshake = _decode_handshake(result)
                if handshake.authority_id != self.expected_authority_id:
                    raise RemoteAuthorityError("controlled authority identity does not match Project configuration")
                if handshake.key_id != self.verifier.key_id:
                    raise RemoteAuthorityError("controlled authority key identity does not match Project configuration")
                missing = REQUIRED_AUTHORITY_CAPABILITIES - handshake.capabilities
                if missing:
                    raise RemoteAuthorityError(
                        "controlled authority lacks required capabilities: " + ", ".join(sorted(missing))
                    )
                self._handshake = handshake
            return self._handshake

    def exchange(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        retry_transport_loss: bool = False,
    ) -> dict[str, object]:
        handshake = self.handshake()
        result = self._exchange_unpinned(operation, payload, retry_transport_loss=retry_transport_loss)
        stores = _mapping(result.pop("stores", None), "authority response store binding")
        expected = {
            "candidate": handshake.candidate_store_id.value,
            "publication": handshake.publication_store_id.value,
            "retention": handshake.retention_store_id.value,
        }
        if stores != expected:
            raise RemoteAuthorityError("controlled authority store identities changed during the session")
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.transport.close()

    def _exchange_unpinned(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        retry_transport_loss: bool = False,
    ) -> dict[str, object]:
        if not isinstance(operation, str) or not operation or not isinstance(payload, dict):
            raise TypeError("authority operation and payload must be canonical JSON values")
        nonce = secrets.token_urlsafe(32)
        request = _canonical_json({"nonce": nonce, "operation": operation, "payload": payload, "protocol": _PROTOCOL})

        def post() -> bytes:
            try:
                return self.transport.post(self.endpoint, request)
            except RemoteAuthorityError:
                raise
            except Exception as exc:
                raise RemoteAuthorityUnavailableError("controlled authority request failed") from exc

        try:
            response_bytes = post()
        except RemoteAuthorityUnavailableError:
            if not retry_transport_loss:
                raise
            # Reuse the exact nonce and bytes once.  The host's durable replay
            # binding makes this a result recovery, not a second mutation.
            response_bytes = post()
        envelope = _json_mapping(response_bytes, "controlled authority response envelope")
        if set(envelope) != {"key_id", "payload", "signature"} or envelope.get("key_id") != self.verifier.key_id:
            raise RemoteAuthorityError("controlled authority response envelope is malformed or uses another key")
        signed = _decode_base64url(envelope["payload"], "authority response payload")
        signature = _decode_base64url(envelope["signature"], "authority response signature")
        self.verifier.verify(signed, signature)
        response = _json_mapping(signed, "signed authority response")
        if (
            response.get("protocol") != _PROTOCOL
            or response.get("authority_id") != self.expected_authority_id
            or response.get("key_id") != self.verifier.key_id
            or response.get("nonce") != nonce
            or response.get("operation") != operation
        ):
            raise RemoteAuthorityError("signed authority response does not bind this exact request")
        if response.get("ok") is not True:
            error = _mapping(response.get("error"), "authority error")
            kind = error.get("kind")
            message = error.get("message")
            if not isinstance(message, str) or not message:
                raise RemoteAuthorityError("controlled authority returned a malformed error")
            if kind == "unknown":
                raise RemotePublicationExecutionUnknownError(message)
            if kind == "not-found":
                raise PublicationRecoveryNotFoundError(message)
            raise RemoteAuthorityError(message)
        return _mapping(response.get("result"), "authority result")


class _RemoteCandidateWorkspace(MutableWorkspace):
    def __init__(self, owner: object, base: ImmutableWorkspace | None, parent: SnapshotId | None) -> None:
        self._owner = owner
        self.parent_snapshot_id = parent
        self.workspace = InMemoryWorkspace(
            () if base is None else base.list_entries(),
            capabilities=WorkspaceCapabilities(executable_mode=True),
            mutable=True,
        )

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        return self.workspace.capabilities

    @property
    def is_mutable(self) -> bool:
        return self.workspace.is_mutable

    @property
    def content_id(self):  # type: ignore[no-untyped-def]
        return self.workspace.content_id

    def list_entries(self, prefix: str | None = None) -> tuple[WorkspaceEntry, ...]:
        return self.workspace.list_entries(prefix)

    def list(self, prefix: str | None = None) -> tuple[WorkspaceEntry, ...]:
        return self.workspace.list(prefix)

    def get_entry(self, key: str) -> WorkspaceEntry:
        return self.workspace.get_entry(key)

    def inspect(self, key: str) -> WorkspaceEntry:
        return self.workspace.inspect(key)

    def read(self, key: str) -> bytes:
        return self.workspace.read(key)

    def entry_content_ids(self):  # type: ignore[no-untyped-def]
        return self.workspace.entry_content_ids()

    def write(self, key: str, content: bytes, *, executable: bool = False) -> None:
        self.workspace.write(key, content, executable=executable)

    def mkdir(self, key: str) -> None:
        raise RemoteAuthorityError("controlled Git candidates do not support explicit directories")

    def symlink(self, key: str, target: str) -> None:
        raise RemoteAuthorityError("controlled Git candidates do not support symbolic links")

    def copy_from(self, source: ImmutableWorkspace, source_key: str, destination_key: str) -> None:
        self.workspace.copy_from(source, source_key, destination_key)

    def delete(self, key: str, *, recursive: bool = False) -> None:
        self.workspace.delete(key, recursive=recursive)


@dataclass(slots=True)
class ControlledGitPublicationAuthority:
    """Remote CandidateStore, publication authority, and exact snapshot reader."""

    session: VerifiedAuthoritySession
    _candidate_token: object = field(default_factory=object, init=False, repr=False)
    _proof_issuer: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        handshake = self.session.handshake()
        client_store_id = _client_publication_store_id(handshake)
        object.__setattr__(
            self,
            "_proof_issuer",
            _client_proof_issuer(client_store_id),
        )

    def begin_candidate(
        self, base: ImmutableWorkspace | None = None, parent_snapshot_id: SnapshotId | None = None
    ) -> MutableWorkspace:
        if base is not None and (not isinstance(base, ImmutableWorkspace) or base.is_mutable):
            raise TypeError("candidate base must be an immutable workspace")
        if base is not None and any(entry.kind is not WorkspaceEntryKind.FILE for entry in base.list_entries()):
            raise RemoteAuthorityError("controlled Git candidates require file-only logical workspaces")
        return _RemoteCandidateWorkspace(self._candidate_token, base, parent_snapshot_id)

    def seal_candidate(self, workspace: MutableWorkspace) -> SealedCandidate:
        if not isinstance(workspace, _RemoteCandidateWorkspace) or workspace._owner is not self._candidate_token:
            raise ValueError("candidate workspace was not issued by this controlled authority")
        workspace._owner = None
        result = self.session.exchange(
            "seal_candidate",
            {
                "entries": _entries_wire(workspace.list_entries()),
                "parent": workspace.parent_snapshot_id.value if workspace.parent_snapshot_id is not None else None,
            },
            retry_transport_loss=True,
        )
        candidate = _candidate_from_wire(result.get("candidate"), self.session.handshake().candidate_store_id)
        if candidate.content_id != workspace.content_id:
            raise RemoteAuthorityError("authority sealed content differs from uploaded candidate")
        return candidate

    def prepare_head(self, channel_id: ChannelId) -> HeadObservation:
        return _head_from_wire(
            self.session.exchange("prepare_head", {"channel": channel_id.value}).get("head"), channel_id
        )

    def resolve_head(self, channel_id: ChannelId) -> HeadObservation:
        return _head_from_wire(
            self.session.exchange("resolve_head", {"channel": channel_id.value}).get("head"), channel_id
        )

    def ownership(self, source: SourceSnapshotId) -> OwnershipObservation:
        return _ownership_from_wire(self.session.exchange("ownership", _source_snapshot_wire(source)).get("ownership"))

    def coordination(self, key: str) -> CoordinationObservation:
        return _coordination_from_wire(self.session.exchange("coordination", {"key": key}).get("coordination"))

    def recovery_locator(self, intent):  # type: ignore[no-untyped-def]
        intent._validate()
        return PublicationRecoveryLocator(_client_publication_store_id(self.session.handshake()), intent.attempt_id)

    def execute(self, intent):  # type: ignore[no-untyped-def]
        intent._validate()
        try:
            result = self.session.exchange("execute", {"intent": self._host_intent_wire(intent)})
        except RemoteAuthorityUnavailableError as exc:
            # The transport cannot prove whether the authenticated host crossed
            # its commit point before the response was lost.  Force the
            # application coordinator down its exact verification path and
            # preserve every source needed for durable recovery.
            raise RemotePublicationExecutionUnknownError(
                "controlled authority publication may have committed before the response was lost"
            ) from exc
        return self._outcome(result.get("outcome"), intent)

    def verify(self, intent):  # type: ignore[no-untyped-def]
        intent._validate()
        try:
            result = self.session.exchange("verify", {"intent": self._host_intent_wire(intent)})
        except (PublicationExecutionUnknownError, RemoteAuthorityUnavailableError):
            # Verification is observational.  Losing its response cannot turn
            # an ambiguous publication into a definite failure or trigger
            # retained-source cleanup.
            return PublicationOutcome(PublicationOutcomeState.UNKNOWN)
        return self._outcome(result.get("outcome"), intent)

    def recover_publication(self, locator: PublicationRecoveryLocator) -> PublicationRecovery:
        self._validate_client_locator(locator)
        host_locator = PublicationRecoveryLocator(self.session.handshake().publication_store_id, locator.attempt_id)
        result = self.session.exchange("recover_publication", {"locator": json.loads(host_locator.to_wire())})
        intent = _intent_from_wire(result.get("intent"), self.session.handshake(), self._proof_issuer)
        return PublicationRecovery(intent, self._outcome(result.get("outcome"), intent))

    def observe_review_acceptance(self, locator: PublicationRecoveryLocator):  # type: ignore[no-untyped-def]
        self._validate_client_locator(locator)
        handshake = self.session.handshake()
        host_locator = PublicationRecoveryLocator(handshake.publication_store_id, locator.attempt_id)
        result = self.session.exchange("observe_review_acceptance", {"locator": json.loads(host_locator.to_wire())})
        value = _mapping(result.get("acceptance"), "review acceptance")
        if value.get("store") != handshake.publication_store_id.value:
            raise RemoteAuthorityError("review acceptance belongs to another publication store")
        store = _client_publication_store_id(handshake)
        base = _head_from_wire(value.get("accepted_base"), ChannelId(cast(str, value.get("desired_channel"))))
        return _issue_review_acceptance_observation(
            store,
            PublicationRecoveryLocator(store, PublicationAttemptId(cast(str, value.get("review_attempt")))),
            PublicationProofId(cast(str, value.get("proof"))),
            ChannelId(cast(str, value.get("desired_channel"))),
            base,
            SnapshotId(cast(str, value.get("candidate_snapshot"))),
            ContentId(cast(str, value.get("candidate_content"))),
            EnvironmentId(cast(str, value.get("environment"))),
            cast(str, value.get("incarnation")),
            self._proof_issuer,
        )

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        result = self.session.exchange("open_snapshot", {"snapshot": snapshot_id.value})
        workspace = _workspace_from_wire(result.get("entries"))
        view = SnapshotView(snapshot_id, ContentId(cast(str, result.get("content"))), workspace)
        if view.content_id != workspace.content_id:
            raise RemoteAuthorityError("signed snapshot content identity does not match its entries")
        return view

    def is_ancestor_snapshot(self, ancestor: SnapshotId, descendant: SnapshotId) -> bool:
        result = self.session.exchange(
            "is_ancestor_snapshot",
            {"ancestor": ancestor.value, "descendant": descendant.value},
        )
        selected = result.get("is_ancestor")
        if type(selected) is not bool:
            raise RemoteAuthorityError("signed snapshot ancestry response is malformed")
        return selected

    @staticmethod
    def revision_for_snapshot(snapshot_id: SnapshotId) -> str:
        if not isinstance(snapshot_id, SnapshotId) or not snapshot_id.value.startswith(_GIT_SNAPSHOT_PREFIX):
            raise RemoteAuthorityError("snapshot was not issued as a controlled Git commit")
        revision = snapshot_id.value.removeprefix(_GIT_SNAPSHOT_PREFIX)
        if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
            raise RemoteAuthorityError("snapshot does not contain a canonical Git commit")
        return revision

    @staticmethod
    def snapshot_id_for_revision(revision: str) -> SnapshotId:
        if (
            not isinstance(revision, str)
            or len(revision) != 40
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise RemoteAuthorityError("controlled Git revision must be one canonical commit ID")
        return SnapshotId(f"{_GIT_SNAPSHOT_PREFIX}{revision}")

    def close(self) -> None:
        self.session.close()

    def _outcome(self, value: object, intent):  # type: ignore[no-untyped-def]
        outcome = _mapping(value, "publication outcome")
        state = PublicationOutcomeState(cast(str, outcome.get("state")))
        proof_wire = outcome.get("proof")
        if proof_wire is None:
            return PublicationOutcome(state)
        proof_data = _mapping(proof_wire, "publication proof")
        resulting_head = _head_from_wire(proof_data.get("resulting_head"), intent.channel_id)
        ownership_results = tuple(_ownership_result_from_wire(item) for item in _list(proof_data.get("ownership")))
        coordination_results = tuple(
            _coordination_result_from_wire(item) for item in _list(proof_data.get("coordination"))
        )
        proof = _issue_publication_proof(
            _client_publication_store_id(self.session.handshake()),
            self._proof_issuer,
            intent,
            resulting_head,
            ownership_results,
            coordination_results,
            PublicationProofId(cast(str, proof_data.get("proof_id"))),
        )
        return PublicationOutcome(state, proof)

    def _validate_client_locator(self, locator: PublicationRecoveryLocator) -> None:
        if not isinstance(
            locator, PublicationRecoveryLocator
        ) or locator.publication_store_id != _client_publication_store_id(self.session.handshake()):
            raise ValueError("publication recovery locator belongs to another controlled authority")

    def _host_intent_wire(self, intent: PublicationIntent) -> dict[str, object]:
        wire = intent._wire_data()
        acceptance = wire.get("review_acceptance")
        if isinstance(acceptance, dict):
            expected = _client_publication_store_id(self.session.handshake()).value
            if acceptance.get("store") != expected:
                raise ValueError("review acceptance belongs to another controlled authority")
            acceptance["store"] = self.session.handshake().publication_store_id.value
        return wire


@dataclass(slots=True)
class ControlledGitSourceRetention:
    """Shared durable source retention hosted by the controlled authority."""

    session: VerifiedAuthoritySession

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        if not isinstance(source, SourceSnapshot):
            raise TypeError("source must be a SourceSnapshot")
        result = self.session.exchange(
            "retain_source",
            {
                "content": source.content_id.value,
                "entries": _entries_wire(source.workspace.list_entries()),
                **_source_snapshot_wire(source.source_snapshot_id),
            },
            retry_transport_loss=True,
        )
        return _retained_from_wire(result.get("retained"), self.session.handshake().retention_store_id)

    def reissue(self, locator: RetainedSourceLocator) -> RetainedSource:
        result = self.session.exchange("reissue_source", {"locator": json.loads(locator.to_wire())})
        return _retained_from_wire(result.get("retained"), self.session.handshake().retention_store_id)

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        retained._validate()
        result = self.session.exchange(
            "recover_source", {"locator": json.loads(RetainedSourceLocator.from_retained(retained).to_wire())}
        )
        source_id = retained.source_snapshot_id.source_id
        source_snapshot = SourceSnapshotId(source_id, SnapshotId(cast(str, result.get("snapshot"))))
        workspace = _workspace_from_wire(result.get("entries"))
        source = SourceSnapshot(source_snapshot, ContentId(cast(str, result.get("content"))), workspace)
        if source.source_snapshot_id != retained.source_snapshot_id or source.content_id != retained.content_id:
            raise SourceRetentionError("signed retained source response does not match its capability")
        return source

    def release(self, retained: RetainedSource) -> None:
        retained._validate()
        self.session.exchange(
            "release_source", {"locator": json.loads(RetainedSourceLocator.from_retained(retained).to_wire())}
        )

    def retained_snapshot(self, source_snapshot_id: SourceSnapshotId) -> tuple[RetainedSource, SourceSnapshot] | None:
        result = self.session.exchange("find_retained_source", _source_snapshot_wire(source_snapshot_id))
        if result.get("retained") is None:
            return None
        retained = _retained_from_wire(result.get("retained"), self.session.handshake().retention_store_id)
        return retained, self.recover(retained)

    def close(self) -> None:
        """The publication authority owns the shared session lifecycle."""


@dataclass(slots=True)
class ControlledGitSourceRepository:
    """Resolve from an exact local Git source and retain it only on the authority."""

    resolver: SourceRepository
    retention: ControlledGitSourceRetention
    accepts_external_sources: bool = field(default=True, init=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        return self.resolver.resolve(request)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        return self.retention.retain(source)

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        return self.retention.recover(retained)

    def release(self, retained: RetainedSource) -> None:
        self.retention.release(retained)

    def reissue(self, locator: RetainedSourceLocator) -> RetainedSource:
        return self.retention.reissue(locator)

    def retained_snapshot(self, source_snapshot_id: SourceSnapshotId) -> tuple[RetainedSource, SourceSnapshot] | None:
        return self.retention.retained_snapshot(source_snapshot_id)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.resolver.close()


@dataclass(slots=True)
class ControlledGitApplyService:
    coordinator: ApplyCoordinator

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        return self.coordinator.apply(command, changes)

    def recover(self, locator: PublicationRecoveryLocator) -> ApplyResult:
        return self.coordinator.recover(locator)

    def close(self) -> None:
        self.coordinator.close()


@dataclass(slots=True)
class ControlledGitReviewAdoptionService:
    coordinator: ReviewAdoptionCoordinator

    def adopt(self, command: ReviewAdoptionCommand) -> ReviewAdoptionResult:
        return self.coordinator.adopt(command)

    def recover(self, locator: PublicationRecoveryLocator) -> ReviewAdoptionResult:
        return self.coordinator.recover(locator)

    def close(self) -> None:
        self.coordinator.authority.close()


@dataclass(frozen=True, slots=True)
class ControlledRootIncarnationIssuer(RootIncarnationIssuer):
    session: VerifiedAuthoritySession

    @property
    def issuer_id(self) -> str:
        return f"controlled-root:{self.session.handshake().authority_id}"

    def issue(self, request: RootIdentityRequest) -> IssuedRootIdentity:
        request._validate()
        result = self.session.exchange(
            "issue_root_identity",
            {
                "api_version": request.api_version,
                "authored_content": request.authored_content_id.value,
                "environment": request.environment_id.value,
                "kind": request.kind,
                "qualified_name": request.qualified_name,
                "source": (
                    _source_snapshot_wire(request.source_snapshot_id)
                    if request.source_snapshot_id is not None
                    else None
                ),
                "tombstones": list(request.finalized_tombstone_uids),
            },
        )
        if result.get("issuer_id") != self.issuer_id:
            raise RemoteAuthorityError("signed root identity uses another authority issuer")
        return _issue_root_identity(request, self.issuer_id, cast(str, result.get("uid")))


@dataclass(frozen=True, slots=True)
class ControlledApplyPublicationIdentityIssuer(ApplyPublicationIdentityIssuer):
    session: VerifiedAuthoritySession

    def issue_attempt(
        self, environment: str, target: ChannelId, base: HeadObservation, candidate: SealedCandidate
    ) -> PublicationAttemptId:
        result = self.session.exchange(
            "issue_publication_identity",
            {
                "base": base._wire_data(),
                "candidate": {
                    "content": candidate.content_id.value,
                    "handle": candidate.handle.value,
                    "snapshot": candidate.snapshot_id.value,
                    "store": candidate.candidate_store_id.value,
                },
                "environment": environment,
                "target": target.value,
            },
        )
        return PublicationAttemptId(cast(str, result.get("attempt")))

    def issue_owner(self, environment: str, candidate: SealedCandidate) -> OwnershipId:
        result = self.session.exchange(
            "issue_publication_owner",
            {
                "candidate": {
                    "content": candidate.content_id.value,
                    "handle": candidate.handle.value,
                    "snapshot": candidate.snapshot_id.value,
                    "store": candidate.candidate_store_id.value,
                },
                "environment": environment,
            },
        )
        return OwnershipId(cast(str, result.get("owner")))


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _json_mapping(value: bytes, description: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAuthorityError(f"{description} is not canonical JSON") from exc
    return _mapping(decoded, description)


def _mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RemoteAuthorityError(f"{description} must be an object")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise RemoteAuthorityError("authority response requires an array")
    return value


def _decode_base64url(value: object, description: str) -> bytes:
    if not isinstance(value, str):
        raise RemoteAuthorityError(f"{description} must be base64url text")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise RemoteAuthorityError(f"{description} must be base64url text") from exc


def _git_config_value(repository: Path, key: str) -> str | None:
    completed = subprocess.run(
        ("git", "-C", str(repository), "config", "--local", "--get", key),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise RemoteAuthorityUnavailableError("local Git authority credential configuration cannot be read")
    value = completed.stdout.rstrip("\n")
    if not value or value != value.strip() or "\x00" in value:
        raise RemoteAuthorityUnavailableError("local Git authority credential configuration is malformed")
    return value


def _credential_file(value: str, description: str, *, private: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise RemoteAuthorityUnavailableError(f"controlled authority {description} must be an absolute regular file")
    try:
        metadata = path.stat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RemoteAuthorityUnavailableError(f"controlled authority {description} cannot be opened") from exc
    if not stat.S_ISREG(metadata.st_mode) or resolved != path:
        raise RemoteAuthorityUnavailableError(f"controlled authority {description} must be an absolute regular file")
    if private and metadata.st_mode & 0o077:
        raise RemoteAuthorityUnavailableError("controlled authority client private key permissions are too broad")
    return resolved


def _decode_handshake(value: dict[str, object]) -> RemoteAuthorityHandshake:
    stores = _mapping(value.get("stores"), "authority handshake stores")
    return RemoteAuthorityHandshake(
        cast(str, value.get("protocol")),
        cast(str, value.get("authority_id")),
        cast(str, value.get("key_id")),
        PublicationStoreId(cast(str, stores.get("publication"))),
        CandidateStoreId(cast(str, stores.get("candidate"))),
        RetentionStoreId(cast(str, stores.get("retention"))),
        frozenset(cast(list[str], value.get("capabilities"))),
    )


def _client_publication_store_id(handshake: RemoteAuthorityHandshake) -> PublicationStoreId:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "authority": handshake.authority_id,
                "store": handshake.publication_store_id.value,
            }
        )
    ).hexdigest()
    return PublicationStoreId(f"controlled-publication-store:{digest}")


def _client_proof_issuer(store_id: PublicationStoreId) -> object:
    with _CLIENT_ISSUER_LOCK:
        secret = _CLIENT_ISSUER_SECRETS.setdefault(store_id.value, secrets.token_bytes(32))
    return _open_publication_proof_issuer(store_id, secret)


def _entries_wire(entries: tuple[WorkspaceEntry, ...]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for entry in entries:
        if entry.kind is not WorkspaceEntryKind.FILE or entry.content is None:
            raise RemoteAuthorityError("controlled authority workspaces support regular files only")
        result.append(
            {
                "content": base64.urlsafe_b64encode(entry.content).decode().rstrip("="),
                "executable": entry.executable,
                "key": entry.key,
            }
        )
    return result


def _workspace_from_wire(value: object) -> InMemoryWorkspace:
    entries: list[WorkspaceEntry] = []
    for raw in _list(value):
        item = _mapping(raw, "workspace entry")
        if set(item) != {"content", "executable", "key"} or type(item.get("executable")) is not bool:
            raise RemoteAuthorityError("signed workspace entry is malformed")
        entries.append(
            WorkspaceEntry.file(
                cast(str, item.get("key")),
                _decode_base64url(item.get("content"), "workspace content"),
                executable=cast(bool, item.get("executable")),
            )
        )
    return InMemoryWorkspace(entries, capabilities=WorkspaceCapabilities(executable_mode=True), mutable=False)


def _source_snapshot_wire(source: SourceSnapshotId) -> dict[str, object]:
    return {"source": source.source_id.value, "snapshot": source.snapshot_id.value}


def _candidate_from_wire(value: object, expected_store: CandidateStoreId) -> SealedCandidate:
    item = _mapping(value, "sealed candidate")
    if item.get("store") != expected_store.value:
        raise RemoteAuthorityError("sealed candidate belongs to another candidate store")
    return _issue_sealed_candidate(
        SealedCandidateHandle(cast(str, item.get("handle"))),
        expected_store,
        SnapshotId(cast(str, item.get("snapshot"))),
        ContentId(cast(str, item.get("content"))),
    )


def _retained_from_wire(value: object, expected_store: RetentionStoreId) -> RetainedSource:
    item = _mapping(value, "retained source")
    if item.get("store") != expected_store.value:
        raise RemoteAuthorityError("retained source belongs to another retention store")
    return _issue_retained_source(
        RetainedSourceHandle(cast(str, item.get("handle"))),
        expected_store,
        SourceSnapshotId(SourceId(cast(str, item.get("source"))), SnapshotId(cast(str, item.get("snapshot")))),
        ContentId(cast(str, item.get("content"))),
    )


def _head_from_wire(value: object, expected_channel: ChannelId) -> HeadObservation:
    item = _mapping(value, "head observation")
    if item.get("channel") != expected_channel.value:
        raise RemoteAuthorityError("head observation belongs to another channel")
    snapshot = item.get("snapshot")
    return HeadObservation(
        expected_channel,
        SnapshotId(snapshot) if isinstance(snapshot, str) else None,
        cast(str, item.get("incarnation")),
    )


def _ownership_from_wire(value: object) -> OwnershipObservation:
    item = _mapping(value, "ownership observation")
    owner = item.get("owner")
    return OwnershipObservation(
        OwnershipId(owner) if isinstance(owner, str) else None, cast(str, item.get("incarnation"))
    )


def _coordination_from_wire(value: object) -> CoordinationObservation:
    item = _mapping(value, "coordination observation")
    next_value = item.get("value")
    return CoordinationObservation(
        next_value if isinstance(next_value, str) else None, cast(str, item.get("incarnation"))
    )


def _ownership_result_from_wire(value: object) -> SourceOwnershipResult:
    item = _mapping(value, "ownership result")
    requested = item.get("requested_next")
    return SourceOwnershipResult(
        SourceSnapshotId(SourceId(cast(str, item.get("source"))), SnapshotId(cast(str, item.get("snapshot")))),
        OwnershipId(requested) if isinstance(requested, str) else None,
        _ownership_from_wire(item.get("result")),
    )


def _coordination_result_from_wire(value: object) -> CoordinationResult:
    item = _mapping(value, "coordination result")
    requested = item.get("requested_next")
    return CoordinationResult(
        cast(str, item.get("key")),
        requested if isinstance(requested, str) else None,
        _coordination_from_wire(item.get("result")),
    )


def _intent_from_wire(value: object, handshake: RemoteAuthorityHandshake, issuer: object) -> PublicationIntent:
    """Reissue a signed historical intent without granting live source authority.

    Recovery results are immediately verified by the same signed server result;
    later execution sends this exact wire back to the host, which independently
    reissues every live candidate and source capability from durable records.
    """

    wire = _mapping(value, "recovered publication intent")
    try:
        candidate_wire = _mapping(wire.get("candidate"), "recovered candidate")
        candidate = _candidate_from_wire(candidate_wire, handshake.candidate_store_id)
        expected_wire = _mapping(wire.get("expected_head"), "recovered expected head")
        channel = ChannelId(cast(str, wire.get("channel")))
        expected = _head_from_wire(expected_wire, channel)
        ownership_changes: list[SourceOwnershipChange] = []
        for raw in _list(wire.get("ownership")):
            change = _mapping(raw, "recovered ownership change")
            retained_wire = _mapping(change.get("retained"), "recovered retained source")
            retained = _retained_from_wire(retained_wire, handshake.retention_store_id)
            ownership_changes.append(
                SourceOwnershipChange(
                    retained,
                    _ownership_from_wire(change.get("expected")),
                    OwnershipId(cast(str, change.get("next"))) if isinstance(change.get("next"), str) else None,
                )
            )
        coordination_changes: list[CoordinationChange] = []
        for raw in _list(wire.get("coordination")):
            change = _mapping(raw, "recovered coordination change")
            coordination_changes.append(
                CoordinationChange(
                    cast(str, change.get("key")),
                    _coordination_from_wire(change.get("expected")),
                    cast(str | None, change.get("next")),
                )
            )
        acceptance_wire = wire.get("review_acceptance")
        acceptance = None
        client_store = _client_publication_store_id(handshake)
        if acceptance_wire is not None:
            item = _mapping(acceptance_wire, "recovered review acceptance")
            if item.get("store") != handshake.publication_store_id.value:
                raise RemoteAuthorityError("recovered review acceptance belongs to another authority")
            desired_channel = ChannelId(cast(str, item.get("desired_channel")))
            acceptance = _issue_review_acceptance_observation(
                client_store,
                PublicationRecoveryLocator(client_store, PublicationAttemptId(cast(str, item.get("review_attempt")))),
                PublicationProofId(cast(str, item.get("proof"))),
                desired_channel,
                _head_from_wire(item.get("accepted_base"), desired_channel),
                SnapshotId(cast(str, item.get("candidate_snapshot"))),
                ContentId(cast(str, item.get("candidate_content"))),
                EnvironmentId(cast(str, item.get("environment"))),
                cast(str, item.get("incarnation")),
                issuer,
            )
        review_base_wire = wire.get("review_base_head")
        review_base = None
        if review_base_wire is not None:
            review_item = _mapping(review_base_wire, "recovered review base")
            review_base = _head_from_wire(review_item, ChannelId(cast(str, review_item.get("channel"))))
        environment = wire.get("environment")
        return PublicationIntent(
            PublicationAttemptId(cast(str, wire.get("attempt"))),
            channel,
            expected,
            candidate,
            tuple(ownership_changes),
            OwnershipId(cast(str, wire.get("owner"))),
            tuple(coordination_changes),
            PublicationTarget(cast(str, wire.get("target"))),
            PublicationMode(cast(str, wire.get("mode"))),
            review_base_head=review_base,
            review_acceptance=acceptance,
            environment_id=EnvironmentId(environment) if isinstance(environment, str) else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RemoteAuthorityError):
            raise
        raise RemoteAuthorityError("signed recovered publication intent is malformed") from exc
