"""Controlled authority host for signed remote publication operations."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gitopsctr.adapters.git.publication import CandidateLocator, GitPublicationStore
from gitopsctr.adapters.git.remote_authority import (
    REQUIRED_AUTHORITY_CAPABILITIES,
    _canonical_json,
    _entries_wire,
    _mapping,
    _workspace_from_wire,
)
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application.apply_orchestration import ApplyPublicationIdentityIssuer
from gitopsctr.application.apply_projection import RootIdentityRequest, RootIncarnationIssuer
from gitopsctr.application.model import (
    CandidateStoreId,
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    PublicationOutcome,
    PublicationRecoveryLocator,
    SealedCandidateHandle,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.ports import PublicationExecutionUnknownError, PublicationRecoveryNotFoundError
from gitopsctr.application.sources import (
    RetainedSourceLocator,
    SourceRepository,
    SourceRequest,
    SourceSnapshot,
    same_source_payload,
)

_PROTOCOL = "gitopsctr-authority/v1"
_GIT_SOURCE_SNAPSHOT = re.compile(r"git-source:([0-9a-f]{40})$")
_REPLAY_JOURNAL_VERSION = 1
_REPLAY_JOURNAL_FILENAME = "authority-replay-v1.json"
_REPLAY_LOCK_FILENAME = "authority-replay-v1.lock"


@dataclass(frozen=True, slots=True)
class AuthenticatedAuthorityPrincipal:
    """Identity established by the HTTPS boundary before host dispatch."""

    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity or self.identity != self.identity.strip():
            raise ValueError("authority principal identity must be canonical text")


@dataclass(frozen=True, slots=True)
class Ed25519EnvelopeSigner:
    """Private host-side signer; key material never crosses the transport."""

    key_id: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id or self.key_id != self.key_id.strip():
            raise ValueError("authority signing key ID must be canonical text")
        if not isinstance(self.private_key, Ed25519PrivateKey):
            raise TypeError("authority signer requires an Ed25519 private key")

    def envelope(self, payload: dict[str, object]) -> bytes:
        signed = _canonical_json(payload)
        return _canonical_json(
            {
                "key_id": self.key_id,
                "payload": _base64url(signed),
                "signature": _base64url(self.private_key.sign(signed)),
            }
        )


@dataclass(slots=True)
class GitAuthorityHost:
    """Execute every sensitive operation next to one local Git authority store."""

    authority_id: str
    endpoint: str
    key_id: str
    signer: Ed25519EnvelopeSigner
    publication_store: GitPublicationStore
    source_repository: GitSourceRepository
    root_identity_issuer: RootIncarnationIssuer
    publication_identity_issuer: ApplyPublicationIdentityIssuer
    authorized_principals: frozenset[str]
    source_repositories: Mapping[SourceId, SourceRepository] | None = None
    replay_journal_capacity: int = 100_000
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_id, str)
            or not self.authority_id
            or self.authority_id != self.authority_id.strip()
        ):
            raise ValueError("authority host identity must be canonical text")
        if self.signer.key_id != self.key_id:
            raise ValueError("authority host signer key ID does not match its advertised key")
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("https://"):
            raise ValueError("authority host endpoint must be HTTPS")
        if self.publication_store.source_repository is not self.source_repository:
            raise ValueError("authority host publication and retention stores must share one source repository")
        repositories = (
            {self.source_repository.source_id: self.source_repository}
            if self.source_repositories is None
            else dict(self.source_repositories)
        )
        if repositories.get(self.source_repository.source_id) is not self.source_repository:
            raise ValueError("authority source registry must include its exact primary source repository")
        if any(not isinstance(key, SourceId) or value is None for key, value in repositories.items()):
            raise TypeError("authority source registry must map SourceId values to repositories")
        self.source_repositories = repositories
        if self.root_identity_issuer is None or self.publication_identity_issuer is None:
            raise TypeError("authority host requires stable private identity issuers")
        if (
            not isinstance(self.authorized_principals, frozenset)
            or not self.authorized_principals
            or any(not isinstance(value, str) or not value for value in self.authorized_principals)
        ):
            raise ValueError("authority host requires an explicit nonempty principal allowlist")
        if type(self.replay_journal_capacity) is not int or self.replay_journal_capacity < 1:
            raise ValueError("authority host replay journal capacity must be a positive integer")
        retention_store = self.source_repository.retention_store
        if retention_store is None:
            raise ValueError("authority source retention is unavailable")
        with retention_store._locked():
            retention_store._write(retention_store._load())
        with self._locked_replay_journal() as replay:
            self._write_replay_journal(replay)

    def handle(self, request: bytes, *, principal: AuthenticatedAuthorityPrincipal) -> bytes:
        """Handle one message only after the HTTPS boundary authenticated its caller."""

        operation = "invalid"
        nonce = "invalid"
        try:
            value = json.loads(request)
            message = _mapping(value, "authority request")
            if set(message) != {"nonce", "operation", "payload", "protocol"}:
                raise ValueError("authority request has an unsupported shape")
            if message.get("protocol") != _PROTOCOL:
                raise ValueError("authority request protocol is unsupported")
            operation = cast(str, message.get("operation"))
            nonce = cast(str, message.get("nonce"))
            if not operation or not nonce:
                raise ValueError("authority request operation and nonce are required")
            if not isinstance(principal, AuthenticatedAuthorityPrincipal):
                raise TypeError("authority host requires an authenticated principal")
            if principal.identity not in self.authorized_principals:
                raise PermissionError("authenticated principal is not authorized for this authority")
            payload = _mapping(message.get("payload"), "authority request payload")
            request_digest = hashlib.sha256(_canonical_json(message)).hexdigest()
            return self._handle_bound_request(operation, nonce, payload, principal.identity, request_digest)
        except PublicationExecutionUnknownError as exc:
            response = self._response(operation, nonce, ok=False, error={"kind": "unknown", "message": str(exc)})
        except PublicationRecoveryNotFoundError as exc:
            response = self._response(operation, nonce, ok=False, error={"kind": "not-found", "message": str(exc)})
        except Exception as exc:
            response = self._response(
                operation,
                nonce,
                ok=False,
                error={"kind": "rejected", "message": str(exc) or "authority request was rejected"},
            )
        return self.signer.envelope(response)

    def prune_replay_journal(self, *, retain_latest: int) -> int:
        """Discard oldest replay results after the operator's retry window.

        The journal never evicts entries implicitly: reaching its configured
        bound fails closed.  An operator may prune only after callers can no
        longer retry those nonces.  This makes the storage lifecycle explicit
        without silently re-executing an old mutating request.
        """

        if type(retain_latest) is not int or retain_latest < 0:
            raise ValueError("retained replay result count must be a nonnegative integer")
        with self._locked_replay_journal() as replay:
            entries = cast(dict[str, dict[str, object]], replay["entries"])
            ordered = sorted(entries.items(), key=lambda item: cast(int, item[1]["sequence"]), reverse=True)
            retained_nonces = {nonce for nonce, _record in ordered[:retain_latest]}
            removed = len(entries) - len(retained_nonces)
            replay["entries"] = {nonce: record for nonce, record in entries.items() if nonce in retained_nonces}
            self._write_replay_journal(replay)
            return removed

    def _handle_bound_request(
        self,
        operation: str,
        nonce: str,
        payload: dict[str, object],
        principal: str,
        request_digest: str,
    ) -> bytes:
        with self._locked_replay_journal() as replay:
            entries = cast(dict[str, dict[str, object]], replay["entries"])
            prior: dict[str, object] | None = entries.get(nonce)
            if prior is not None:
                if (
                    prior.get("principal") != principal
                    or prior.get("operation") != operation
                    or prior.get("request_digest") != request_digest
                ):
                    response = self._response(
                        operation,
                        nonce,
                        ok=False,
                        error={"kind": "rejected", "message": "authority request nonce was already bound"},
                    )
                    return self.signer.envelope(response)
                cached_value = prior.get("response")
                if cached_value is not None:
                    cached = _cached_replay_response(cached_value)
                    return self.signer.envelope(
                        self._response(
                            operation,
                            nonce,
                            ok=cast(bool, cached["ok"]),
                            result=cast(dict[str, object] | None, cached["result"]),
                            error=cast(dict[str, object] | None, cached["error"]),
                        )
                    )
            elif len(entries) >= self.replay_journal_capacity:
                response = self._response(
                    operation,
                    nonce,
                    ok=False,
                    error={
                        "kind": "rejected",
                        "message": "authority replay journal capacity is exhausted; operator pruning is required",
                    },
                )
                return self.signer.envelope(response)
            else:
                sequence = cast(int, replay["next_sequence"])
                prior = {
                    "operation": operation,
                    "principal": principal,
                    "request_digest": request_digest,
                    "response": None,
                    "sequence": sequence,
                }
                entries[nonce] = prior
                replay["next_sequence"] = sequence + 1
                # The binding is durable before any operation can mutate its
                # own store.  A fresh host resumes this exact request after a
                # crash instead of minting another handle.
                self._write_replay_journal(replay)
            assert prior is not None
            request_token = self._request_token(principal, nonce, request_digest)
            try:
                with self._lock:
                    result = self._dispatch(operation, payload, request_token=request_token)
                response = self._response(operation, nonce, ok=True, result=result)
            except PublicationExecutionUnknownError as exc:
                response = self._response(operation, nonce, ok=False, error={"kind": "unknown", "message": str(exc)})
            except PublicationRecoveryNotFoundError as exc:
                response = self._response(
                    operation,
                    nonce,
                    ok=False,
                    error={"kind": "not-found", "message": str(exc)},
                )
            except Exception as exc:
                response = self._response(
                    operation,
                    nonce,
                    ok=False,
                    error={"kind": "rejected", "message": str(exc) or "authority request was rejected"},
                )
            prior["response"] = {
                "error": response["error"],
                "ok": response["ok"],
                "result": response["result"],
            }
            self._write_replay_journal(replay)
            return self.signer.envelope(response)

    @contextmanager
    def _locked_replay_journal(self) -> Iterator[dict[str, object]]:
        root = self.publication_store._state_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        with os.fdopen(os.open(root / _REPLAY_LOCK_FILENAME, flags, 0o600), "a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._load_replay_journal(root)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load_replay_journal(self, root: Path) -> dict[str, object]:
        path = root / _REPLAY_JOURNAL_FILENAME
        if not path.exists():
            return {"entries": {}, "next_sequence": 1, "version": _REPLAY_JOURNAL_VERSION}
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
            replay = json.loads(path.read_text())
            if not isinstance(replay, dict) or replay.get("version") != _REPLAY_JOURNAL_VERSION:
                raise ValueError
            entries = replay.get("entries")
            next_sequence = replay.get("next_sequence")
            if not isinstance(entries, dict) or type(next_sequence) is not int or next_sequence < 1:
                raise ValueError
            mac = replay.pop("mac", None)
            if not isinstance(mac, str) or not hmac.compare_digest(mac, self._replay_mac(replay)):
                raise ValueError
            for nonce, raw in entries.items():
                if not isinstance(nonce, str) or not nonce or not isinstance(raw, dict):
                    raise ValueError
                _validate_replay_record(raw, next_sequence)
            replay["mac"] = mac
            return cast(dict[str, object], replay)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("authority replay journal is corrupted or unreadable") from exc

    def _write_replay_journal(self, replay: dict[str, object]) -> None:
        root = self.publication_store._state_root()
        temporary_path: Path | None = None
        try:
            unsigned = {key: value for key, value in replay.items() if key != "mac"}
            stored = {**unsigned, "mac": self._replay_mac(unsigned)}
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, delete=False) as temporary:
                os.fchmod(temporary.fileno(), 0o600)
                json.dump(stored, temporary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, root / _REPLAY_JOURNAL_FILENAME)
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            replay.clear()
            replay.update(stored)
        except OSError as exc:
            raise ValueError("authority replay journal cannot be written") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _replay_mac(self, replay: dict[str, object]) -> str:
        secret = hmac.new(
            self.publication_store._state_secret,
            b"gitopsctr-authority-replay-v1",
            hashlib.sha256,
        ).digest()
        return hmac.new(secret, _canonical_json(replay), hashlib.sha256).hexdigest()

    def _request_token(self, principal: str, nonce: str, request_digest: str) -> str:
        secret = hmac.new(
            self.publication_store._state_secret,
            b"gitopsctr-authority-request-v1",
            hashlib.sha256,
        ).digest()
        return hmac.new(
            secret,
            _canonical_json({"digest": request_digest, "nonce": nonce, "principal": principal}),
            hashlib.sha256,
        ).hexdigest()

    def _dispatch(self, operation: str, payload: dict[str, object], *, request_token: str) -> dict[str, object]:
        if operation == "handshake":
            return {
                "authority_id": self.authority_id,
                "capabilities": sorted(REQUIRED_AUTHORITY_CAPABILITIES),
                "key_id": self.key_id,
                "protocol": _PROTOCOL,
                "stores": self._stores(),
            }
        if operation == "prepare_head":
            return self._bound(
                {"head": _head_wire(self.publication_store.prepare_head(ChannelId(_text(payload, "channel"))))}
            )
        if operation == "resolve_head":
            return self._bound(
                {"head": _head_wire(self.publication_store.resolve_head(ChannelId(_text(payload, "channel"))))}
            )
        if operation == "ownership":
            source = _source_snapshot(payload)
            return self._bound({"ownership": _ownership_wire(self.publication_store.ownership(source))})
        if operation == "coordination":
            return self._bound(
                {"coordination": _coordination_wire(self.publication_store.coordination(_text(payload, "key")))}
            )
        if operation == "seal_candidate":
            workspace = _workspace_from_wire(payload.get("entries"))
            parent_value = payload.get("parent")
            parent = SnapshotId(parent_value) if isinstance(parent_value, str) else None
            candidate = self.publication_store.seal_candidate_for_request(workspace, parent, request_token)
            return self._bound({"candidate": _candidate_wire(candidate)})
        if operation in {"execute", "verify"}:
            intent = self.publication_store._intent_from_wire(payload.get("intent"))
            outcome = (
                self.publication_store.execute(intent)
                if operation == "execute"
                else self.publication_store.verify(intent)
            )
            return self._bound({"outcome": _outcome_wire(outcome)})
        if operation == "recover_publication":
            locator = PublicationRecoveryLocator.from_wire(_canonical_text(payload.get("locator")))
            recovery = self.publication_store.recover_publication(locator)
            return self._bound({"intent": recovery.intent._wire_data(), "outcome": _outcome_wire(recovery.outcome)})
        if operation == "observe_review_acceptance":
            locator = PublicationRecoveryLocator.from_wire(_canonical_text(payload.get("locator")))
            acceptance = self.publication_store.observe_review_acceptance(locator)
            return self._bound({"acceptance": _acceptance_wire(acceptance)})
        if operation == "open_snapshot":
            snapshot = SnapshotId(_text(payload, "snapshot"))
            reader = GitSnapshotReader.from_path(self.publication_store.repository)
            try:
                view = reader.open_snapshot(snapshot)
            finally:
                reader.close()
            return self._bound(
                {"content": view.content_id.value, "entries": _entries_wire(view.workspace.list_entries())}
            )
        if operation == "is_ancestor_snapshot":
            reader = GitSnapshotReader.from_path(self.publication_store.repository)
            try:
                is_ancestor = reader.is_ancestor_snapshot(
                    SnapshotId(_text(payload, "ancestor")),
                    SnapshotId(_text(payload, "descendant")),
                )
            finally:
                reader.close()
            return self._bound({"is_ancestor": is_ancestor})
        if operation == "issue_root_identity":
            source_wire = payload.get("source")
            request = RootIdentityRequest(
                EnvironmentId(_text(payload, "environment")),
                _text(payload, "api_version"),
                _text(payload, "kind"),
                _text(payload, "qualified_name"),
                _source_snapshot(_mapping(source_wire, "root source identity")) if source_wire is not None else None,
                ContentId(_text(payload, "authored_content")),
                tuple(cast(list[str], payload.get("tombstones"))),
            )
            issued = self.root_identity_issuer.issue(request)
            return self._bound(
                {
                    "issuer_id": f"controlled-root:{self.authority_id}",
                    "uid": issued.uid,
                }
            )
        if operation in {"issue_publication_identity", "issue_publication_owner"}:
            candidate_wire = _mapping(payload.get("candidate"), "publication identity candidate")
            candidate = self.publication_store.reissue_candidate(
                _candidate_locator(candidate_wire, self._stores()["candidate"])
            )
            environment = _text(payload, "environment")
            if operation == "issue_publication_owner":
                owner = self.publication_identity_issuer.issue_owner(environment, candidate)
                return self._bound({"owner": owner.value})
            target = ChannelId(_text(payload, "target"))
            base = _request_head(payload.get("base"), target)
            attempt = self.publication_identity_issuer.issue_attempt(environment, target, base, candidate)
            return self._bound({"attempt": attempt.value})
        if operation == "retain_source":
            workspace = _workspace_from_wire(payload.get("entries"))
            source = SourceSnapshot(
                _source_snapshot(payload),
                ContentId(_text(payload, "content")),
                workspace,
            )
            if source.content_id != workspace.content_id:
                raise ValueError("uploaded source content identity does not match its exact workspace")
            match = _GIT_SOURCE_SNAPSHOT.fullmatch(source.source_snapshot_id.snapshot_id.value)
            repositories = self.source_repositories
            assert repositories is not None
            resolver = repositories.get(source.source_snapshot_id.source_id)
            if match is None or resolver is None:
                raise ValueError("uploaded source is not authorized by the authority source registry")
            canonical = resolver.resolve(SourceRequest(source.source_snapshot_id.source_id, match.group(1)))
            if not same_source_payload(canonical, source):
                raise ValueError("uploaded source differs from the authority-resolved exact source snapshot")
            retention_store = self.source_repository.retention_store
            if retention_store is None:
                raise ValueError("authority source retention is unavailable")
            retained = retention_store.retain_for_request(canonical, request_token)
            return self._bound({"retained": _retained_wire(retained)})
        if operation in {"reissue_source", "recover_source", "release_source"}:
            locator = RetainedSourceLocator.from_wire(_canonical_text(payload.get("locator")))
            retained = self.source_repository.reissue(locator)
            if operation == "reissue_source":
                return self._bound({"retained": _retained_wire(retained)})
            if operation == "release_source":
                self.source_repository.release(retained)
                return self._bound({})
            source = self.source_repository.recover(retained)
            return self._bound(
                {
                    "content": source.content_id.value,
                    "entries": _entries_wire(source.workspace.list_entries()),
                    "snapshot": source.source_snapshot_id.snapshot_id.value,
                }
            )
        if operation == "find_retained_source":
            retention_store = self.source_repository.retention_store
            if retention_store is None:
                raise ValueError("authority source retention is unavailable")
            found = retention_store.retained_snapshot(_source_snapshot(payload))
            return self._bound({"retained": _retained_wire(found[0]) if found is not None else None})
        raise ValueError("authority operation is unsupported")

    def _stores(self) -> dict[str, str]:
        with self.publication_store._locked() as state:
            publication = state["publication_store_id"]
            candidate = state["candidate_store_id"]
        retention_store = self.source_repository.retention_store
        if retention_store is None:
            raise ValueError("authority source retention is unavailable")
        with retention_store._locked():
            retention = retention_store._load()["store_id"]
        return {"candidate": candidate, "publication": publication, "retention": retention}

    def _bound(self, result: dict[str, object]) -> dict[str, object]:
        return {**result, "stores": self._stores()}

    def _response(
        self,
        operation: str,
        nonce: str,
        *,
        ok: bool,
        result: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "error": error,
            "key_id": self.key_id,
            "nonce": nonce,
            "ok": ok,
            "operation": operation,
            "protocol": _PROTOCOL,
            "result": result,
        }


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _text(value: dict[str, object], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise ValueError(f"authority request requires {key}")
    return selected


def _canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _cached_replay_response(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"error", "ok", "result"} or type(value.get("ok")) is not bool:
        raise ValueError("authority replay journal response is malformed")
    ok = cast(bool, value["ok"])
    result = value["result"]
    error = value["error"]
    if ok:
        if not isinstance(result, dict) or error is not None:
            raise ValueError("authority replay journal success is malformed")
    elif not isinstance(error, dict) or result is not None:
        raise ValueError("authority replay journal rejection is malformed")
    return cast(dict[str, object], value)


def _validate_replay_record(value: dict[str, object], next_sequence: int) -> None:
    if set(value) != {"operation", "principal", "request_digest", "response", "sequence"}:
        raise ValueError("authority replay journal record has an unsupported shape")
    operation = value.get("operation")
    principal = value.get("principal")
    digest = value.get("request_digest")
    sequence = value.get("sequence")
    if not isinstance(operation, str) or not operation or not isinstance(principal, str) or not principal:
        raise ValueError("authority replay journal binding is malformed")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("authority replay journal request digest is malformed")
    if type(sequence) is not int or sequence < 1 or sequence >= next_sequence:
        raise ValueError("authority replay journal sequence is malformed")
    response = value.get("response")
    if response is not None:
        _cached_replay_response(response)


def _source_snapshot(value: dict[str, object]) -> SourceSnapshotId:
    return SourceSnapshotId(SourceId(_text(value, "source")), SnapshotId(_text(value, "snapshot")))


def _candidate_locator(value: dict[str, object], expected_store: str) -> CandidateLocator:
    if value.get("store") != expected_store:
        raise ValueError("publication identity candidate belongs to another store")
    return CandidateLocator(
        SealedCandidateHandle(_text(value, "handle")),
        CandidateStoreId(expected_store),
        SnapshotId(_text(value, "snapshot")),
        ContentId(_text(value, "content")),
    )


def _request_head(value: object, expected_channel: ChannelId) -> HeadObservation:
    item = _mapping(value, "publication identity base head")
    if item.get("channel") != expected_channel.value:
        raise ValueError("publication identity base head belongs to another channel")
    snapshot = item.get("snapshot")
    return HeadObservation(
        expected_channel,
        SnapshotId(snapshot) if isinstance(snapshot, str) else None,
        _text(item, "incarnation"),
    )


def _head_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return value._wire_data()


def _ownership_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return {"incarnation": value.incarnation, "owner": value.owner.value if value.owner is not None else None}


def _coordination_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return {"incarnation": value.incarnation, "value": value.value}


def _candidate_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return {
        "content": value.content_id.value,
        "handle": value.handle.value,
        "snapshot": value.snapshot_id.value,
        "store": value.candidate_store_id.value,
    }


def _retained_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return {
        "content": value.content_id.value,
        "handle": value.handle.value,
        "snapshot": value.source_snapshot_id.snapshot_id.value,
        "source": value.source_snapshot_id.source_id.value,
        "store": value.retention_store_id.value,
    }


def _outcome_wire(value: PublicationOutcome) -> dict[str, object]:
    value._validate()
    proof = value.proof
    return {
        "proof": (
            {
                "coordination": [
                    {
                        "key": item.key,
                        "requested_next": item.requested_next_value,
                        "result": _coordination_wire(item.resulting_observation),
                    }
                    for item in proof.coordination_results
                ],
                "ownership": [
                    {
                        "requested_next": (
                            item.requested_next_owner.value if item.requested_next_owner is not None else None
                        ),
                        "result": _ownership_wire(item.resulting_observation),
                        "snapshot": item.source_snapshot_id.snapshot_id.value,
                        "source": item.source_snapshot_id.source_id.value,
                    }
                    for item in proof.ownership_results
                ],
                "proof_id": proof.proof_id.value,
                "resulting_head": _head_wire(proof.resulting_head),
            }
            if proof is not None
            else None
        ),
        "state": value.state.value,
    }


def _acceptance_wire(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value._validate()
    return {
        "accepted_base": _head_wire(value.accepted_base_head),
        "candidate_content": value.candidate_content_id.value,
        "candidate_snapshot": value.candidate_snapshot_id.value,
        "desired_channel": value.desired_channel.value,
        "environment": value.environment_id.value,
        "incarnation": value.incarnation,
        "proof": value.review_proof_id.value,
        "review_attempt": value.review_publication.attempt_id.value,
        "store": value.publication_store_id.value,
    }
