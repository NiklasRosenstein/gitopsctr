from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dulwich.repo import Repo

from gitopsctr import composition
from gitopsctr.adapters.git.authority_host import (
    AuthenticatedAuthorityPrincipal,
    Ed25519EnvelopeSigner,
    GitAuthorityHost,
)
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.remote_authority import (
    AuthorityHttpTransport,
    ControlledGitPublicationAuthority,
    ControlledGitSourceRetention,
    Ed25519EnvelopeVerifier,
    RemoteAuthorityError,
    RemoteAuthorityUnavailableError,
    VerifiedAuthoritySession,
    authenticated_transport_from_git_config,
)
from gitopsctr.adapters.git.remote_workspace_planes import ControlledGitWorkspacePlaneProvider
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application.apply import ApplyCommand
from gitopsctr.application.apply_orchestration import HmacApplyPublicationIdentityIssuer
from gitopsctr.application.apply_projection import HmacRootIncarnationIssuer
from gitopsctr.application.model import (
    ChannelId,
    EnvironmentId,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcomeState,
    PublicationTarget,
    SourceId,
)
from gitopsctr.application.ports import PublicationExecutionUnknownError
from gitopsctr.application.review_adoption import (
    ReviewAdoptionCommand,
    ReviewAdoptionConfiguration,
    ReviewAdoptionCoordinator,
    ReviewAdoptionError,
)
from gitopsctr.application.sources import SourceRequest, SourceSnapshot, same_source_payload
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.composition import create_default_application
from gitopsctr.git_local import DulwichLocalRepository
from gitopsctr.resource_model import ResourcePlane


@dataclass
class _HostTransport(AuthorityHttpTransport):
    host: GitAuthorityHost
    caller_authenticated: bool = True
    principal_identity: str = "test-client"

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.host.endpoint
        return self.host.handle(request, principal=AuthenticatedAuthorityPrincipal(self.principal_identity))

    def close(self) -> None:
        pass


@dataclass
class _UnauthenticatedTransport:
    caller_authenticated: bool = False

    def post(self, endpoint: str, request: bytes) -> bytes:
        del endpoint, request
        raise AssertionError("unauthenticated transport must fail before network access")

    def close(self) -> None:
        pass


@dataclass
class _ProcessTransport:
    connection: Connection
    endpoint: str
    caller_authenticated: bool = True

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.endpoint
        self.connection.send_bytes(request)
        return self.connection.recv_bytes()

    def close(self) -> None:
        pass


class _DropResponseTransport(_HostTransport):
    def __init__(self, host: GitAuthorityHost, operation: str) -> None:
        super().__init__(host)
        self.operation = operation
        self.dropped = False

    def post(self, endpoint: str, request: bytes) -> bytes:
        response = super().post(endpoint, request)
        operation = json.loads(request).get("operation")
        if operation == self.operation and not self.dropped:
            self.dropped = True
            raise ConnectionResetError("response lost after host dispatch")
        return response


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    source_root = tmp_path / "source"
    source_root.mkdir()
    Repo.init(source_root).close()
    (source_root / "source.txt").write_text("exact source")
    local = DulwichLocalRepository(source_root)
    revision = local.create_commit(
        local.write_tree(source_root), None, "source", "tests", "tests@example.invalid", timestamp=1
    )
    local.close()
    retention_root = tmp_path / "retention"
    retention_root.mkdir()
    source_id = SourceId("default-git-source")
    source = GitSourceRepository.from_path(source_id, source_root, retention_root)
    authority_root = tmp_path / "authority.git"
    authority_root.mkdir()
    Repo.init_bare(authority_root).close()
    store = GitPublicationStore(authority_root, source)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    host = GitAuthorityHost(
        "test/authority",
        "https://authority.example.invalid/gitopsctr",
        "test-key",
        Ed25519EnvelopeSigner("test-key", private_key),
        store,
        source,
        HmacRootIncarnationIssuer("test-root", "test-private-root-seed"),
        HmacApplyPublicationIdentityIssuer("test-publication", "test-private-publication-seed"),
        frozenset({"test-client"}),
    )
    session = VerifiedAuthoritySession(
        host.endpoint,
        host.authority_id,
        Ed25519EnvelopeVerifier(host.key_id, public_key),
        _HostTransport(host),
    )
    return source, revision, store, host, session


def _fresh_host(host: GitAuthorityHost, *, replay_journal_capacity: int | None = None) -> GitAuthorityHost:
    return GitAuthorityHost(
        host.authority_id,
        host.endpoint,
        host.key_id,
        host.signer,
        host.publication_store,
        host.source_repository,
        host.root_identity_issuer,
        host.publication_identity_issuer,
        host.authorized_principals,
        host.source_repositories,
        host.replay_journal_capacity if replay_journal_capacity is None else replay_journal_capacity,
    )


def _authority_request(operation: str, nonce: str, payload: dict[str, object]) -> bytes:
    return json.dumps(
        {"nonce": nonce, "operation": operation, "payload": payload, "protocol": "gitopsctr-authority/v1"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _signed_authority_response(response: bytes) -> dict[str, object]:
    envelope = json.loads(response)
    encoded = envelope["payload"]
    payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    decoded = json.loads(payload)
    assert isinstance(decoded, dict)
    return decoded


def _response_mapping(response: Mapping[str, object], field: str) -> dict[str, object]:
    selected = response[field]
    assert isinstance(selected, dict)
    return selected


def _source_entries(source: SourceSnapshot) -> list[dict[str, object]]:
    return [
        {
            "content": base64.urlsafe_b64encode(entry.content or b"").decode().rstrip("="),
            "executable": entry.executable,
            "key": entry.key,
        }
        for entry in source.workspace.list_entries()
    ]


def _serve_authority_process(
    connection: Connection,
    authority_root: str,
    source_root: str,
    retention_root: str,
    private_key: bytes,
) -> None:
    source_id = SourceId("default-git-source")
    source = GitSourceRepository.from_path(source_id, Path(source_root), Path(retention_root))
    store = GitPublicationStore(Path(authority_root), source)
    host = GitAuthorityHost(
        "test/authority",
        "https://authority.example.invalid/gitopsctr",
        "test-key",
        Ed25519EnvelopeSigner("test-key", Ed25519PrivateKey.from_private_bytes(private_key)),
        store,
        source,
        HmacRootIncarnationIssuer("test-root", "test-private-root-seed"),
        HmacApplyPublicationIdentityIssuer("test-publication", "test-private-publication-seed"),
        frozenset({"test-client"}),
    )
    try:
        while True:
            request = connection.recv_bytes()
            if not request:
                return
            connection.send_bytes(host.handle(request, principal=AuthenticatedAuthorityPrincipal("test-client")))
    finally:
        source.close()
        connection.close()


def test_signed_remote_authority_publishes_recovers_and_reads_exact_snapshot(tmp_path: Path):
    _source, _revision, _store, _host, session = _fixture(tmp_path)
    authority = ControlledGitPublicationAuthority(session)
    channel = ChannelId("gitopsctr/desired/dev")
    candidate_workspace = authority.begin_candidate()
    candidate_workspace.write("units/app.json", b'{"kind":"Unit"}', executable=True)
    candidate = authority.seal_candidate(candidate_workspace)
    expected = authority.prepare_head(channel)
    intent = PublicationIntent(
        PublicationAttemptId("remote-attempt"),
        channel,
        expected,
        candidate,
        (),
        OwnershipId("remote-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )

    outcome = authority.execute(intent)

    assert outcome.state is PublicationOutcomeState.COMMITTED
    assert outcome.proof is not None
    assert authority.resolve_head(channel) == outcome.proof.resulting_head
    view = authority.open_snapshot(candidate.snapshot_id)
    assert view.content_id == candidate.content_id
    assert view.workspace.read("units/app.json") == b'{"kind":"Unit"}'
    assert view.workspace.get_entry("units/app.json").executable is True
    locator = authority.recovery_locator(intent)
    recovery = authority.recover_publication(locator)
    assert recovery.outcome.state is PublicationOutcomeState.COMMITTED
    assert recovery.intent._wire_data() == intent._wire_data()
    planes = ControlledGitWorkspacePlaneProvider(tmp_path / "source", authority)
    desired = planes.snapshot(ResourcePlane.DESIRED, channel.value)
    assert desired.revision == candidate.snapshot_id.value.removeprefix("git-commit:")
    assert desired.workspace.read("units/app.json") == b'{"kind":"Unit"}'


def test_remote_execute_and_verify_response_loss_preserve_unknown_and_durable_recovery(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    execute_transport = _DropResponseTransport(host, "execute")
    authority = ControlledGitPublicationAuthority(
        VerifiedAuthoritySession(host.endpoint, host.authority_id, session.verifier, execute_transport)
    )
    channel = ChannelId("gitopsctr/desired/lost-response")
    workspace = authority.begin_candidate()
    workspace.write("units/app.json", b"committed before disconnect")
    candidate = authority.seal_candidate(workspace)
    intent = PublicationIntent(
        PublicationAttemptId("lost-response-attempt"),
        channel,
        authority.prepare_head(channel),
        candidate,
        (),
        OwnershipId("lost-response-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    locator = authority.recovery_locator(intent)

    with pytest.raises(PublicationExecutionUnknownError, match="may have committed"):
        authority.execute(intent)

    fresh = ControlledGitPublicationAuthority(
        VerifiedAuthoritySession(host.endpoint, host.authority_id, session.verifier, _HostTransport(host))
    )
    assert fresh.recover_publication(locator).outcome.state is PublicationOutcomeState.COMMITTED
    assert fresh.resolve_head(channel).snapshot_id == candidate.snapshot_id

    verifying = ControlledGitPublicationAuthority(
        VerifiedAuthoritySession(
            host.endpoint,
            host.authority_id,
            session.verifier,
            _DropResponseTransport(host, "verify"),
        )
    )
    assert verifying.verify(intent).state is PublicationOutcomeState.UNKNOWN
    assert fresh.recover_publication(locator).outcome.state is PublicationOutcomeState.COMMITTED


def test_host_replays_identical_candidate_seal_from_durable_result_without_duplicate(tmp_path: Path):
    _source, _revision, store, host, _session = _fixture(tmp_path)
    request = _authority_request(
        "seal_candidate",
        "seal-replay-nonce",
        {
            "entries": [
                {
                    "content": base64.urlsafe_b64encode(b"exact candidate").decode().rstrip("="),
                    "executable": True,
                    "key": "units/app.bin",
                }
            ],
            "parent": None,
        },
    )

    first = host.handle(request, principal=AuthenticatedAuthorityPrincipal("test-client"))
    replayed = _fresh_host(host).handle(request, principal=AuthenticatedAuthorityPrincipal("test-client"))

    assert replayed == first
    first_candidate = _response_mapping(_response_mapping(_signed_authority_response(first), "result"), "candidate")
    replayed_candidate = _response_mapping(
        _response_mapping(_signed_authority_response(replayed), "result"), "candidate"
    )
    assert replayed_candidate == first_candidate
    with store._locked() as state:
        assert len(state["candidates"]) == 1


def test_host_replays_identical_source_retention_from_durable_result_without_duplicate(tmp_path: Path):
    source, revision, _store, host, _session = _fixture(tmp_path)
    selected = source.resolve(SourceRequest(source.source_id, revision))
    request = _authority_request(
        "retain_source",
        "retain-replay-nonce",
        {
            "content": selected.content_id.value,
            "entries": _source_entries(selected),
            "snapshot": selected.source_snapshot_id.snapshot_id.value,
            "source": selected.source_snapshot_id.source_id.value,
        },
    )

    first = host.handle(request, principal=AuthenticatedAuthorityPrincipal("test-client"))
    replayed = _fresh_host(host).handle(request, principal=AuthenticatedAuthorityPrincipal("test-client"))

    assert replayed == first
    first_retained = _response_mapping(_response_mapping(_signed_authority_response(first), "result"), "retained")
    replayed_retained = _response_mapping(_response_mapping(_signed_authority_response(replayed), "result"), "retained")
    assert replayed_retained == first_retained
    retention_store = source.retention_store
    assert retention_store is not None
    with retention_store._locked():
        assert len(retention_store._load()["records"]) == 1


def test_remote_client_recovers_lost_seal_and_retain_responses_with_exact_request_replay(tmp_path: Path):
    source, revision, store, host, session = _fixture(tmp_path)
    seal_transport = _DropResponseTransport(host, "seal_candidate")
    authority = ControlledGitPublicationAuthority(
        VerifiedAuthoritySession(host.endpoint, host.authority_id, session.verifier, seal_transport)
    )
    workspace = authority.begin_candidate()
    workspace.write("units/app.bin", b"one candidate")

    candidate = authority.seal_candidate(workspace)

    assert seal_transport.dropped is True
    assert candidate.handle.value.startswith("git-candidate:request-")
    with store._locked() as state:
        assert len(state["candidates"]) == 1

    retain_transport = _DropResponseTransport(host, "retain_source")
    retention = ControlledGitSourceRetention(
        VerifiedAuthoritySession(host.endpoint, host.authority_id, session.verifier, retain_transport)
    )
    retained = retention.retain(source.resolve(SourceRequest(source.source_id, revision)))

    assert retain_transport.dropped is True
    assert retained.handle.value.startswith("git-retained:request-")
    retention_store = source.retention_store
    assert retention_store is not None
    with retention_store._locked():
        assert len(retention_store._load()["records"]) == 1


@pytest.mark.parametrize("operation", ["seal_candidate", "retain_source"])
def test_host_resumes_preparing_mutation_after_crash_without_second_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    source, revision, store, host, _session = _fixture(tmp_path)
    if operation == "seal_candidate":
        payload: dict[str, object] = {
            "entries": [
                {
                    "content": base64.urlsafe_b64encode(b"crash-safe candidate").decode().rstrip("="),
                    "executable": False,
                    "key": "units/app.bin",
                }
            ],
            "parent": None,
        }
    else:
        selected = source.resolve(SourceRequest(source.source_id, revision))
        payload = {
            "content": selected.content_id.value,
            "entries": _source_entries(selected),
            "snapshot": selected.source_snapshot_id.snapshot_id.value,
            "source": selected.source_snapshot_id.source_id.value,
        }
    request = _authority_request(operation, f"{operation}-crash-nonce", payload)
    original_write = GitAuthorityHost._write_replay_journal
    writes = 0

    class SimulatedHostCrash(BaseException):
        pass

    def crash_before_completed_replay(self: GitAuthorityHost, replay: dict[str, object]) -> None:
        nonlocal writes
        writes += 1
        if self is host and writes == 2:
            raise SimulatedHostCrash
        original_write(self, replay)

    with monkeypatch.context() as scoped:
        scoped.setattr(GitAuthorityHost, "_write_replay_journal", crash_before_completed_replay)
        with pytest.raises(SimulatedHostCrash):
            host.handle(request, principal=AuthenticatedAuthorityPrincipal("test-client"))

    recovered_response = _fresh_host(host).handle(
        request,
        principal=AuthenticatedAuthorityPrincipal("test-client"),
    )
    recovered_result = _signed_authority_response(recovered_response)["result"]
    assert isinstance(recovered_result, dict)
    if operation == "seal_candidate":
        candidate = _response_mapping(recovered_result, "candidate")
        assert isinstance(candidate["handle"], str)
        assert candidate["handle"].startswith("git-candidate:request-")
        with store._locked() as state:
            assert len(state["candidates"]) == 1
    else:
        retained = _response_mapping(recovered_result, "retained")
        assert isinstance(retained["handle"], str)
        assert retained["handle"].startswith("git-retained:request-")
        retention_store = source.retention_store
        assert retention_store is not None
        with retention_store._locked():
            assert len(retention_store._load()["records"]) == 1


def test_host_rejects_nonce_rebinding_and_bounds_replay_state_until_explicit_prune(tmp_path: Path):
    _source, _revision, _store, host, _session = _fixture(tmp_path)
    host.authorized_principals = frozenset({"test-client", "other-client"})
    nonce = "globally-bound-nonce"
    original = _authority_request("handshake", nonce, {})
    assert (
        _signed_authority_response(host.handle(original, principal=AuthenticatedAuthorityPrincipal("test-client")))[
            "ok"
        ]
        is True
    )

    conflicts = (
        (_authority_request("prepare_head", nonce, {"channel": "desired/dev"}), "test-client"),
        (_authority_request("handshake", nonce, {"changed": True}), "test-client"),
        (original, "other-client"),
    )
    for request, principal in conflicts:
        response = _signed_authority_response(
            host.handle(request, principal=AuthenticatedAuthorityPrincipal(principal))
        )
        assert response["ok"] is False
        error = _response_mapping(response, "error")
        assert isinstance(error["message"], str)
        assert "already bound" in error["message"]

    limited = _fresh_host(host, replay_journal_capacity=1)
    capacity_response = _signed_authority_response(
        limited.handle(
            _authority_request("handshake", "capacity-nonce", {}),
            principal=AuthenticatedAuthorityPrincipal("test-client"),
        )
    )
    assert capacity_response["ok"] is False
    capacity_error = _response_mapping(capacity_response, "error")
    assert isinstance(capacity_error["message"], str)
    assert "capacity" in capacity_error["message"]
    assert limited.prune_replay_journal(retain_latest=0) == 1
    assert (
        _signed_authority_response(
            limited.handle(
                _authority_request("handshake", "capacity-nonce", {}),
                principal=AuthenticatedAuthorityPrincipal("test-client"),
            )
        )["ok"]
        is True
    )


def test_review_locator_recovery_and_adoption_survive_signing_key_rotation(tmp_path: Path):
    source, _revision, store, host, session = _fixture(tmp_path)
    first = ControlledGitPublicationAuthority(session)
    desired = ChannelId("desired/dev")
    review_channel = ChannelId("review/dev")
    environment = EnvironmentId("dev")

    base_workspace = first.begin_candidate()
    base_workspace.write("units/app.json", b"base")
    base_candidate = first.seal_candidate(base_workspace)
    base_intent = PublicationIntent(
        PublicationAttemptId("rotation-base"),
        desired,
        first.prepare_head(desired),
        base_candidate,
        (),
        OwnershipId("rotation-base-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert first.execute(base_intent).state is PublicationOutcomeState.COMMITTED
    base = first.prepare_head(desired)
    review_workspace = first.begin_candidate(parent_snapshot_id=base.snapshot_id)
    review_workspace.write("units/app.json", b"reviewed")
    review_candidate = first.seal_candidate(review_workspace)
    review_intent = PublicationIntent(
        PublicationAttemptId("rotation-review"),
        review_channel,
        first.prepare_head(review_channel),
        review_candidate,
        (),
        OwnershipId("rotation-review-owner"),
        (),
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=environment,
    )
    assert first.execute(review_intent).state is PublicationOutcomeState.COMMITTED
    locator = first.recovery_locator(review_intent)

    next_private_key = Ed25519PrivateKey.generate()
    next_key_id = "rotated-key"
    rotated_host = GitAuthorityHost(
        host.authority_id,
        host.endpoint,
        next_key_id,
        Ed25519EnvelopeSigner(next_key_id, next_private_key),
        store,
        source,
        host.root_identity_issuer,
        host.publication_identity_issuer,
        host.authorized_principals,
    )
    next_public_key = next_private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    rotated = ControlledGitPublicationAuthority(
        VerifiedAuthoritySession(
            rotated_host.endpoint,
            rotated_host.authority_id,
            Ed25519EnvelopeVerifier(next_key_id, next_public_key),
            _HostTransport(rotated_host),
        )
    )
    recovered = rotated.recover_publication(locator)
    assert recovered.outcome.state is PublicationOutcomeState.COMMITTED
    assert rotated.recovery_locator(recovered.intent) == locator

    assert base.snapshot_id is not None
    subprocess.run(
        (
            "git",
            "-C",
            str(store.repository),
            "update-ref",
            "refs/heads/desired/dev",
            review_candidate.snapshot_id.value.removeprefix("git-commit:"),
            base.snapshot_id.value.removeprefix("git-commit:"),
        ),
        check=True,
    )

    class EnvironmentResolver:
        def resolve_review_adoption(
            self,
            environment_id: EnvironmentId,
            desired_channel: ChannelId,
            candidate_channel: ChannelId,
        ) -> ReviewAdoptionConfiguration:
            if (environment_id, desired_channel, candidate_channel) != (
                environment,
                desired,
                review_channel,
            ):
                raise ReviewAdoptionError("unexpected review environment")
            return ReviewAdoptionConfiguration(desired, review_channel)

    result = ReviewAdoptionCoordinator(
        rotated,
        HmacApplyPublicationIdentityIssuer("rotation-adoption", "rotation-adoption-seed"),
        EnvironmentResolver(),
    ).adopt(ReviewAdoptionCommand(environment, locator))
    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert rotated.resolve_head(desired).snapshot_id == review_candidate.snapshot_id


def test_remote_source_retention_revalidates_authority_repository_content(tmp_path: Path):
    source, revision, _store, _host, session = _fixture(tmp_path)
    retention = ControlledGitSourceRetention(session)
    selected = source.resolve(SourceRequest(source.source_id, revision))

    retained = retention.retain(selected)

    assert same_source_payload(retention.recover(retained), selected)
    assert retention.reissue(retention_locator(retained)).source_snapshot_id == selected.source_snapshot_id
    found = retention.retained_snapshot(selected.source_snapshot_id)
    assert found is not None and same_source_payload(found[1], selected)
    retention.release(retained)
    with pytest.raises(RemoteAuthorityError, match="unknown|match|retained"):
        retention.reissue(retention_locator(retained))


def test_remote_source_retention_rejects_caller_asserted_content_for_a_real_commit(tmp_path: Path):
    source, revision, _store, _host, session = _fixture(tmp_path)
    retention = ControlledGitSourceRetention(session)
    selected = source.resolve(SourceRequest(source.source_id, revision))
    forged_workspace = InMemoryWorkspace((WorkspaceEntry.file("source.txt", b"attacker content"),), mutable=False)
    forged = SourceSnapshot(
        selected.source_snapshot_id,
        forged_workspace.content_id,
        forged_workspace,
    )

    with pytest.raises(RemoteAuthorityError, match="differs from the authority-resolved"):
        retention.retain(forged)


def test_remote_session_rejects_unauthenticated_transport_before_exchange(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    unauthenticated = VerifiedAuthoritySession(
        host.endpoint,
        host.authority_id,
        session.verifier,
        _UnauthenticatedTransport(),
    )

    with pytest.raises(RemoteAuthorityUnavailableError, match="authenticated transport"):
        unauthenticated.handshake()


def test_remote_session_rejects_signed_response_tampering(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)

    class TamperingTransport(_HostTransport):
        def post(self, endpoint: str, request: bytes) -> bytes:
            envelope = json.loads(super().post(endpoint, request))
            payload = bytearray(base64.urlsafe_b64decode(envelope["payload"] + "=="))
            payload[-1] ^= 1
            envelope["payload"] = base64.urlsafe_b64encode(payload).decode().rstrip("=")
            return json.dumps(envelope).encode()

    tampered = VerifiedAuthoritySession(
        host.endpoint,
        host.authority_id,
        session.verifier,
        TamperingTransport(host),
    )

    with pytest.raises(RemoteAuthorityError, match="signature"):
        tampered.handshake()


def test_remote_host_rejects_authenticated_but_unauthorized_principal_without_state_change(tmp_path: Path):
    _source, _revision, store, host, session = _fixture(tmp_path)
    channel = ChannelId("gitopsctr/desired/dev")
    before = store.resolve_head(channel)
    unauthorized = VerifiedAuthoritySession(
        host.endpoint,
        host.authority_id,
        session.verifier,
        _HostTransport(host, principal_identity="other-client"),
    )

    with pytest.raises(RemoteAuthorityError, match="not authorized"):
        unauthorized.handshake()

    assert store.resolve_head(channel) == before


def test_default_composition_uses_explicit_project_authority_and_injected_authenticated_transport(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    project = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "test"},
        "spec": {
            "effectLease": None,
            "publicationAuthority": {
                "type": "controlled",
                "endpoint": host.endpoint,
                "authorityId": host.authority_id,
                "verificationKey": {
                    "algorithm": "ed25519",
                    "keyId": host.key_id,
                    "publicKey": base64.urlsafe_b64encode(session.verifier.public_key).decode().rstrip("="),
                },
            },
        },
    }
    source_root = tmp_path / "source"
    (source_root / "gitopsctr.yaml").write_text(json.dumps(project))

    application = create_default_application(source_root, authority_transport=_HostTransport(host))
    try:
        assert isinstance(application.snapshot_reader, ControlledGitPublicationAuthority)
        assert application.apply_service is not None
        assert application.review_adoption_service is not None
    finally:
        application.close()


def test_default_composition_fails_before_handshake_without_operator_credentials(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    project = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "test"},
        "spec": {
            "effectLease": None,
            "publicationAuthority": {
                "type": "controlled",
                "endpoint": host.endpoint,
                "authorityId": host.authority_id,
                "verificationKey": {
                    "algorithm": "ed25519",
                    "keyId": host.key_id,
                    "publicKey": base64.urlsafe_b64encode(session.verifier.public_key).decode().rstrip("="),
                },
            },
        },
    }
    source_root = tmp_path / "source"
    (source_root / "gitopsctr.yaml").write_text(json.dumps(project))

    with pytest.raises(RemoteAuthorityUnavailableError, match="local Git config"):
        create_default_application(source_root)


def test_operator_git_config_rejects_broad_private_key_permissions(tmp_path: Path):
    source_root = tmp_path / "repository"
    Repo.init(source_root, mkdir=True).close()
    certificate = tmp_path / "client.pem"
    private_key = tmp_path / "client.key"
    certificate.write_text("not needed before permission validation")
    private_key.write_text("private")
    os.chmod(private_key, 0o644)
    for key, value in {
        "clientCertificate": certificate,
        "clientKey": private_key,
        "clientIdentity": "operator-client",
    }.items():
        subprocess.run(
            ("git", "-C", str(source_root), "config", f"gitopsctr.authority.{key}", str(value)),
            check=True,
        )

    with pytest.raises(RemoteAuthorityUnavailableError, match="permissions"):
        authenticated_transport_from_git_config(source_root)


def test_separate_process_authority_preserves_signed_store_and_snapshot_visibility(tmp_path: Path):
    source, _revision, store, host, session = _fixture(tmp_path)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    private_bytes = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    authority_root = str(store.repository)
    source_root = str(source.repository.root)
    retention_root = str(source.retention_root)
    source.close()
    context = get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_serve_authority_process,
        args=(child, authority_root, source_root, retention_root, private_bytes),
    )
    process.start()
    child.close()
    remote_session = VerifiedAuthoritySession(
        host.endpoint,
        host.authority_id,
        Ed25519EnvelopeVerifier(host.key_id, public),
        _ProcessTransport(parent, host.endpoint),
    )
    authority = ControlledGitPublicationAuthority(remote_session)
    try:
        channel = ChannelId("gitopsctr/desired/process")
        workspace = authority.begin_candidate()
        workspace.write("units/process.json", b"process", executable=True)
        candidate = authority.seal_candidate(workspace)
        intent = PublicationIntent(
            PublicationAttemptId("process-attempt"),
            channel,
            authority.prepare_head(channel),
            candidate,
            (),
            OwnershipId("process-owner"),
            (),
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.DIRECT_ACCEPTED,
        )
        assert authority.execute(intent).state is PublicationOutcomeState.COMMITTED
        assert authority.open_snapshot(candidate.snapshot_id).workspace.read("units/process.json") == b"process"
        assert (
            authority.recover_publication(authority.recovery_locator(intent)).outcome.state
            is PublicationOutcomeState.COMMITTED
        )
    finally:
        parent.send_bytes(b"")
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent.close()
    assert process.exitcode == 0


def test_controlled_composition_closes_shared_transport_and_source_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    source_root = tmp_path / "source"
    project = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "test"},
        "spec": {
            "effectLease": None,
            "publicationAuthority": {
                "type": "controlled",
                "endpoint": host.endpoint,
                "authorityId": host.authority_id,
                "verificationKey": {
                    "algorithm": "ed25519",
                    "keyId": host.key_id,
                    "publicKey": base64.urlsafe_b64encode(session.verifier.public_key).decode().rstrip("="),
                },
            },
        },
    }
    (source_root / "gitopsctr.yaml").write_text(json.dumps(project))

    class Resolver:
        source_id = SourceId("default-git-source")

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    resolver = Resolver()
    monkeypatch.setattr(composition.GitSourceRepository, "from_path", lambda *_args: resolver)

    class FailingCloseTransport(_HostTransport):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("transport close failed")

    transport = FailingCloseTransport(host)
    application = create_default_application(source_root, authority_transport=transport)

    with pytest.raises(RuntimeError, match="transport close failed"):
        application.close()

    assert transport.close_calls == 1
    assert resolver.close_calls == 1


def test_controlled_default_application_projects_and_publishes_through_host(tmp_path: Path):
    _source, _revision, _store, host, session = _fixture(tmp_path)
    source_root = tmp_path / "source"
    public_key = base64.urlsafe_b64encode(session.verifier.public_key).decode().rstrip("=")
    (source_root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {
                    "effectLease": None,
                    "publicationAuthority": {
                        "type": "controlled",
                        "endpoint": host.endpoint,
                        "authorityId": host.authority_id,
                        "verificationKey": {
                            "algorithm": "ed25519",
                            "keyId": host.key_id,
                            "publicKey": public_key,
                        },
                    },
                },
            }
        )
    )
    environment = source_root / "deployment/environments/dev/environment.yaml"
    environment.parent.mkdir(parents=True)
    environment.write_text("apiVersion: gitopsctr.io/v1\nkind: Environment\nmetadata: {name: dev}\nspec: {}\n")
    unit = source_root / "frontend.yaml"
    unit.write_text(
        """apiVersion: unit.gitopsctr.io/v1
kind: FrontendS3Cloudfront
metadata: {name: frontend}
spec:
  inputs:
    bundle: registry.example/frontend@sha256:0000000000000000000000000000000000000000000000000000000000000000
    bucket: example-frontend
    distributionId: EXAMPLE123
    url: https://www.example.invalid
    runtimeConfig:
      schema: 1
      apiBase: https://api.example.invalid
      auth:
        mode: cognito
        issuer: https://issuer.example.invalid
        clientId: example-client
  pull:
    credentialProvider: {type: aws-ecr}
"""
    )
    application = create_default_application(source_root, authority_transport=_HostTransport(host))
    try:
        command = ApplyCommand(EnvironmentId("dev"), (str(unit),), None, None, None, None)
        result = application.apply(command)
        assert result.snapshot_id is not None
        view = application.snapshot_reader.open_snapshot(result.snapshot_id)
        assert view.workspace.read("units/frontend.json")
    finally:
        application.close()


def retention_locator(retained):  # type: ignore[no-untyped-def]
    from gitopsctr.application.sources import RetainedSourceLocator

    return RetainedSourceLocator.from_retained(retained)
