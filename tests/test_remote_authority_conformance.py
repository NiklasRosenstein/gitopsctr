from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dulwich.repo import Repo

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
    VerifiedAuthoritySession,
)
from gitopsctr.adapters.git.remote_workspace_planes import ControlledGitWorkspacePlaneProvider
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application import (
    ChannelId,
    ContentId,
    CoordinationChange,
    EnvironmentId,
    HmacApplyPublicationIdentityIssuer,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcomeState,
    PublicationTarget,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceOwnershipChange,
    SourceSnapshotId,
)
from gitopsctr.application.apply_projection import HmacRootIncarnationIssuer
from gitopsctr.application.review_adoption import (
    ReviewAdoptionCommand,
    ReviewAdoptionConfiguration,
    ReviewAdoptionCoordinator,
    ReviewAdoptionEnvironmentResolver,
    ReviewAdoptionError,
)
from gitopsctr.application.sources import RetainedSourceLocator, SourceRequest, SourceSnapshot, same_source_payload
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.git_local import DulwichLocalRepository
from gitopsctr.resource_model import ResourcePlane

_DESIRED = ChannelId("desired/dev")
_REVIEW = ChannelId("review/dev")
_ENVIRONMENT = EnvironmentId("dev")
_COORDINATION_KEY = "reviews/dev"


@dataclass
class _HostTransport(AuthorityHttpTransport):
    host: GitAuthorityHost
    caller_authenticated: bool = True

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.host.endpoint
        return self.host.handle(request, principal=AuthenticatedAuthorityPrincipal("conformance-client"))

    def close(self) -> None:
        pass


@dataclass
class _MutatingTransport(AuthorityHttpTransport):
    host: GitAuthorityHost
    mutate: Callable[[dict[str, object], dict[str, object]], dict[str, object]]
    caller_authenticated: bool = True

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.host.endpoint
        raw_response = self.host.handle(request, principal=AuthenticatedAuthorityPrincipal("conformance-client"))
        envelope = cast(dict[str, object], json.loads(raw_response))
        signed = _decode_base64url(cast(str, envelope["payload"]))
        response = cast(dict[str, object], json.loads(signed))
        changed = self.mutate(cast(dict[str, object], json.loads(request)), response)
        return self.host.signer.envelope(changed)

    def close(self) -> None:
        pass


@dataclass
class _ReplayTransport(AuthorityHttpTransport):
    host: GitAuthorityHost
    caller_authenticated: bool = True
    response: bytes | None = None

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.host.endpoint
        if self.response is None:
            self.response = self.host.handle(
                request,
                principal=AuthenticatedAuthorityPrincipal("conformance-client"),
            )
        return self.response

    def close(self) -> None:
        pass


@dataclass
class _UnauthenticatedPrincipalTransport(AuthorityHttpTransport):
    host: GitAuthorityHost
    caller_authenticated: bool = True

    def post(self, endpoint: str, request: bytes) -> bytes:
        assert endpoint == self.host.endpoint
        return self.host.handle(request, principal=cast(Any, "untrusted-header-only-name"))

    def close(self) -> None:
        pass


@dataclass
class _Harness:
    source_root: Path
    source: GitSourceRepository
    revision: str
    store: GitPublicationStore
    host: GitAuthorityHost
    verifier: Ed25519EnvelopeVerifier

    def session(
        self,
        transport: AuthorityHttpTransport | None = None,
        *,
        authority_id: str | None = None,
        verifier: Ed25519EnvelopeVerifier | None = None,
    ) -> VerifiedAuthoritySession:
        return VerifiedAuthoritySession(
            self.host.endpoint,
            self.host.authority_id if authority_id is None else authority_id,
            self.verifier if verifier is None else verifier,
            _HostTransport(self.host) if transport is None else transport,
        )


def _harness(tmp_path: Path, name: str = "authority") -> _Harness:
    source_root = tmp_path / f"{name}-source"
    source_root.mkdir(parents=True)
    Repo.init(source_root).close()
    source_file = source_root / "source.bin"
    source_file.write_bytes(b"\x00exact source\xff\n")
    source_file.chmod(0o755)
    local = DulwichLocalRepository(source_root)
    revision = local.create_commit(
        local.write_tree(source_root),
        None,
        "source",
        "tests",
        "tests@example.invalid",
        timestamp=1,
    )
    local.close()
    retention_root = tmp_path / f"{name}-retention"
    retention_root.mkdir()
    source = GitSourceRepository.from_path(SourceId(f"{name}-source"), source_root, retention_root)
    authority_root = tmp_path / f"{name}.git"
    authority_root.mkdir()
    Repo.init_bare(authority_root).close()
    store = GitPublicationStore(authority_root, source)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = f"{name}-key"
    host = GitAuthorityHost(
        f"test/{name}",
        f"https://{name}.example.invalid/gitopsctr",
        key_id,
        Ed25519EnvelopeSigner(key_id, private_key),
        store,
        source,
        HmacRootIncarnationIssuer(f"{name}-root", f"{name}-private-root-seed"),
        HmacApplyPublicationIdentityIssuer(
            f"{name}-publication",
            f"{name}-private-publication-seed",
        ),
        frozenset({"conformance-client"}),
    )
    return _Harness(source_root, source, revision, store, host, Ed25519EnvelopeVerifier(key_id, public_key))


def _candidate(
    authority: ControlledGitPublicationAuthority,
    content: bytes,
    *,
    executable: bool = False,
    parent: SnapshotId | None = None,
):  # type: ignore[no-untyped-def]
    workspace = authority.begin_candidate(parent_snapshot_id=parent)
    workspace.write("units/app.bin", content, executable=executable)
    return authority.seal_candidate(workspace)


def _direct_intent(
    authority: ControlledGitPublicationAuthority,
    attempt: str,
    channel: ChannelId,
    candidate,  # type: ignore[no-untyped-def]
    *,
    expected=None,  # type: ignore[no-untyped-def]
    owner: OwnershipId | None = None,
    source_changes: tuple[SourceOwnershipChange, ...] = (),
    coordination: tuple[CoordinationChange, ...] = (),
) -> PublicationIntent:
    return PublicationIntent(
        PublicationAttemptId(attempt),
        channel,
        authority.prepare_head(channel) if expected is None else expected,
        candidate,
        source_changes,
        OwnershipId(f"owner-{attempt}") if owner is None else owner,
        coordination,
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )


def _publish_base(authority: ControlledGitPublicationAuthority):  # type: ignore[no-untyped-def]
    candidate = _candidate(authority, b"base\n")
    intent = _direct_intent(authority, "base", _DESIRED, candidate)
    assert authority.execute(intent).state is PublicationOutcomeState.COMMITTED
    return authority.prepare_head(_DESIRED)


def _publish_review(authority: ControlledGitPublicationAuthority):  # type: ignore[no-untyped-def]
    base = _publish_base(authority)
    candidate = _candidate(authority, b"#!/bin/reviewed\x00\xff\n", executable=True, parent=base.snapshot_id)
    intent = PublicationIntent(
        PublicationAttemptId("review"),
        _REVIEW,
        authority.prepare_head(_REVIEW),
        candidate,
        (),
        OwnershipId("review-owner"),
        (CoordinationChange(_COORDINATION_KEY, authority.coordination(_COORDINATION_KEY), "pending"),),
        PublicationTarget.REVIEW_CANDIDATE,
        PublicationMode.REVIEW_REQUIRED,
        review_base_head=base,
        environment_id=_ENVIRONMENT,
    )
    assert authority.execute(intent).state is PublicationOutcomeState.COMMITTED
    return intent, authority.recovery_locator(intent)


@dataclass(frozen=True)
class _ReviewEnvironment(ReviewAdoptionEnvironmentResolver):
    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        if (environment_id, desired_channel, candidate_channel) != (_ENVIRONMENT, _DESIRED, _REVIEW):
            raise ReviewAdoptionError("review policy does not authorize these exact channels")
        return ReviewAdoptionConfiguration(_DESIRED, _REVIEW)


def _review_coordinator(authority: ControlledGitPublicationAuthority) -> ReviewAdoptionCoordinator:
    return ReviewAdoptionCoordinator(
        authority,
        HmacApplyPublicationIdentityIssuer("remote-adoption-conformance", "remote-adoption-seed"),
        _ReviewEnvironment(),
    )


def _decode_base64url(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _signed_mutation(
    harness: _Harness,
    mutate: Callable[[dict[str, object], dict[str, object]], dict[str, object]],
) -> VerifiedAuthoritySession:
    return harness.session(_MutatingTransport(harness.host, mutate))


@pytest.mark.parametrize("field", ["nonce", "operation", "authority_id"])
def test_signed_response_must_bind_exact_nonce_operation_and_authority(tmp_path: Path, field: str):
    harness = _harness(tmp_path)

    def mutate(_request: dict[str, object], response: dict[str, object]) -> dict[str, object]:
        response[field] = "signed-but-wrong"
        return response

    with pytest.raises(RemoteAuthorityError, match="exact request"):
        _signed_mutation(harness, mutate).handshake()


def test_handshake_rejects_wrong_key_identity_and_missing_capability(tmp_path: Path):
    harness = _harness(tmp_path)
    wrong_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    with pytest.raises(RemoteAuthorityError, match="signature"):
        harness.session(verifier=Ed25519EnvelopeVerifier(harness.host.key_id, wrong_key)).handshake()
    with pytest.raises(RemoteAuthorityError, match="exact request|identity"):
        harness.session(authority_id="test/other").handshake()

    def omit_capability(_request: dict[str, object], response: dict[str, object]) -> dict[str, object]:
        result = cast(dict[str, object], response["result"])
        capabilities = cast(list[str], result["capabilities"])
        result["capabilities"] = capabilities[1:]
        return response

    with pytest.raises(RemoteAuthorityError, match="lacks required capabilities"):
        _signed_mutation(harness, omit_capability).handshake()


def test_session_rejects_signed_response_replay_and_mid_session_store_change(tmp_path: Path):
    harness = _harness(tmp_path)
    replayed = harness.session(_ReplayTransport(harness.host))
    replayed.handshake()
    with pytest.raises(RemoteAuthorityError, match="exact request"):
        replayed.exchange("prepare_head", {"channel": _DESIRED.value})

    calls = 0

    def change_store(_request: dict[str, object], response: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls > 1:
            result = cast(dict[str, object], response["result"])
            stores = cast(dict[str, object], result["stores"])
            stores["publication"] = "foreign-publication-store"
        return response

    session = _signed_mutation(harness, change_store)
    authority = ControlledGitPublicationAuthority(session)
    with pytest.raises(RemoteAuthorityError, match="store identities changed"):
        authority.prepare_head(_DESIRED)


def test_host_requires_an_authenticated_transport_principal_not_a_claimed_name(tmp_path: Path):
    harness = _harness(tmp_path)
    session = harness.session(_UnauthenticatedPrincipalTransport(harness.host))
    with pytest.raises(RemoteAuthorityError, match="authenticated principal"):
        session.handshake()


def test_two_fresh_clients_share_exact_store_recovery_and_reader_visibility(tmp_path: Path):
    harness = _harness(tmp_path)
    first = ControlledGitPublicationAuthority(harness.session())
    content = b"#!/bin/sh\x00\xff\n"
    candidate = _candidate(first, content, executable=True)
    intent = _direct_intent(first, "fresh-client", _DESIRED, candidate)
    outcome = first.execute(intent)
    assert outcome.state is PublicationOutcomeState.COMMITTED
    locator = first.recovery_locator(intent)

    second = ControlledGitPublicationAuthority(harness.session())
    recovery = second.recover_publication(locator)
    assert recovery.outcome.state is PublicationOutcomeState.COMMITTED
    assert recovery.intent._wire_data() == intent._wire_data()
    assert second.recovery_locator(recovery.intent) == locator
    visible = second.open_snapshot(candidate.snapshot_id)
    assert visible.content_id == candidate.content_id
    assert visible.workspace.read("units/app.bin") == content
    assert visible.workspace.get_entry("units/app.bin").executable is True


def test_candidate_upload_is_exact_and_foreign_store_candidates_fail_closed(tmp_path: Path):
    harness = _harness(tmp_path, "first")
    authority = ControlledGitPublicationAuthority(harness.session())

    def forge_content(_request: dict[str, object], response: dict[str, object]) -> dict[str, object]:
        if response.get("operation") == "seal_candidate":
            result = cast(dict[str, object], response["result"])
            candidate = cast(dict[str, object], result["candidate"])
            candidate["content"] = "sha256:" + "f" * 64
        return response

    tampered = ControlledGitPublicationAuthority(_signed_mutation(harness, forge_content))
    workspace = tampered.begin_candidate()
    workspace.write("units/app.bin", b"exact")
    with pytest.raises(RemoteAuthorityError, match="sealed content differs"):
        tampered.seal_candidate(workspace)

    other = _harness(tmp_path, "second")
    other_authority = ControlledGitPublicationAuthority(other.session())
    foreign_candidate = _candidate(authority, b"first authority")
    intent = _direct_intent(other_authority, "foreign-candidate", _DESIRED, foreign_candidate)
    with pytest.raises(RemoteAuthorityError, match="another candidate store|intent record is malformed"):
        other_authority.execute(intent)


def test_atomic_remote_publication_commits_head_source_ownership_and_coordination(tmp_path: Path):
    harness = _harness(tmp_path)
    authority = ControlledGitPublicationAuthority(harness.session())
    retention = ControlledGitSourceRetention(authority.session)
    exact_source = harness.source.resolve(SourceRequest(harness.source.source_id, harness.revision))
    retained = retention.retain(exact_source)
    owner = OwnershipId("atomic-owner")
    source_change = SourceOwnershipChange(retained, authority.ownership(retained.source_snapshot_id), owner)
    coordination = CoordinationChange(_COORDINATION_KEY, authority.coordination(_COORDINATION_KEY), "held")
    candidate = _candidate(authority, b"atomic\n")
    intent = _direct_intent(
        authority,
        "atomic",
        _DESIRED,
        candidate,
        owner=owner,
        source_changes=(source_change,),
        coordination=(coordination,),
    )

    outcome = authority.execute(intent)

    assert outcome.state is PublicationOutcomeState.COMMITTED
    assert outcome.proof is not None
    assert authority.prepare_head(_DESIRED) == outcome.proof.resulting_head
    assert authority.ownership(retained.source_snapshot_id) == outcome.proof.ownership_results[0].resulting_observation
    assert authority.coordination(_COORDINATION_KEY) == outcome.proof.coordination_results[0].resulting_observation


def test_remote_head_cas_rejects_same_snapshot_after_aba(tmp_path: Path):
    harness = _harness(tmp_path)
    authority = ControlledGitPublicationAuthority(harness.session())
    candidate_a = _candidate(authority, b"a")
    candidate_b = _candidate(authority, b"b")
    candidate_c = _candidate(authority, b"c")
    assert (
        authority.execute(_direct_intent(authority, "head-a", _DESIRED, candidate_a)).state
        is PublicationOutcomeState.COMMITTED
    )
    stale_a = authority.prepare_head(_DESIRED)
    assert (
        authority.execute(_direct_intent(authority, "head-b", _DESIRED, candidate_b)).state
        is PublicationOutcomeState.COMMITTED
    )
    assert (
        authority.execute(_direct_intent(authority, "head-a-again", _DESIRED, candidate_a)).state
        is PublicationOutcomeState.COMMITTED
    )
    assert authority.prepare_head(_DESIRED).snapshot_id == stale_a.snapshot_id
    assert authority.prepare_head(_DESIRED).incarnation != stale_a.incarnation

    stale = _direct_intent(authority, "head-stale", _DESIRED, candidate_c, expected=stale_a)
    with pytest.raises(RemoteAuthorityError, match="expected head is stale"):
        authority.execute(stale)


@pytest.mark.parametrize("fence", ["ownership", "coordination"])
def test_remote_source_and_coordination_cas_reject_current_value_after_aba(tmp_path: Path, fence: str):
    harness = _harness(tmp_path)
    authority = ControlledGitPublicationAuthority(harness.session())
    retention = ControlledGitSourceRetention(authority.session)
    retained = retention.retain(harness.source.resolve(SourceRequest(harness.source.source_id, harness.revision)))

    def advance(attempt: str, owner_value: str, coordination_value: str) -> None:
        owner = OwnershipId(owner_value)
        intent = _direct_intent(
            authority,
            attempt,
            ChannelId("accepted/fence"),
            _candidate(authority, attempt.encode()),
            owner=owner,
            source_changes=(SourceOwnershipChange(retained, authority.ownership(retained.source_snapshot_id), owner),),
            coordination=(
                CoordinationChange(_COORDINATION_KEY, authority.coordination(_COORDINATION_KEY), coordination_value),
            ),
        )
        assert authority.execute(intent).state is PublicationOutcomeState.COMMITTED

    advance("fence-a", "owner-a", "a")
    old_owner = authority.ownership(retained.source_snapshot_id)
    old_coordination = authority.coordination(_COORDINATION_KEY)
    advance("fence-b", "owner-b", "b")
    advance("fence-a-again", "owner-a", "a")
    assert authority.ownership(retained.source_snapshot_id).owner == old_owner.owner
    assert authority.coordination(_COORDINATION_KEY).value == old_coordination.value

    owner = OwnershipId("owner-c")
    source_changes = (
        SourceOwnershipChange(
            retained,
            old_owner if fence == "ownership" else authority.ownership(retained.source_snapshot_id),
            owner,
        ),
    )
    coordination = (
        CoordinationChange(
            _COORDINATION_KEY,
            old_coordination if fence == "coordination" else authority.coordination(_COORDINATION_KEY),
            "c",
        ),
    )
    stale = _direct_intent(
        authority,
        f"stale-{fence}",
        ChannelId("accepted/fence"),
        _candidate(authority, f"stale-{fence}".encode()),
        owner=owner,
        source_changes=source_changes,
        coordination=coordination,
    )
    with pytest.raises(RemoteAuthorityError, match=f"{fence}.*stale"):
        authority.execute(stale)


def test_live_source_registry_rejects_foreign_or_changed_payload_and_reissues_exactly(tmp_path: Path):
    harness = _harness(tmp_path)
    first = ControlledGitSourceRetention(harness.session())
    exact = harness.source.resolve(SourceRequest(harness.source.source_id, harness.revision))
    retained = first.retain(exact)
    locator = RetainedSourceLocator.from_retained(retained)
    second = ControlledGitSourceRetention(harness.session())
    reissued = second.reissue(locator)
    assert same_source_payload(second.recover(reissued), exact)

    foreign = SourceSnapshot(
        SourceSnapshotId(SourceId("unregistered"), exact.source_snapshot_id.snapshot_id),
        exact.content_id,
        exact.workspace,
    )
    with pytest.raises(RemoteAuthorityError, match="authorized.*registry"):
        second.retain(foreign)

    changed_workspace = InMemoryWorkspace((WorkspaceEntry.file("source.bin", b"changed"),), mutable=False)
    changed = SourceSnapshot(exact.source_snapshot_id, changed_workspace.content_id, changed_workspace)
    with pytest.raises(RemoteAuthorityError, match="differs.*exact source"):
        second.retain(changed)

    forged_locator = RetainedSourceLocator(
        RetainedSourceHandle(locator.handle.value),
        RetentionStoreId(locator.retention_store_id.value),
        locator.source_snapshot_id,
        ContentId("sha256:" + "0" * 64),
    )
    with pytest.raises(RemoteAuthorityError, match="match|unknown|retained"):
        second.reissue(forged_locator)
    second.release(reissued)
    with pytest.raises(RemoteAuthorityError, match="match|unknown|retained"):
        first.recover(retained)


def test_review_raw_merge_is_nonauthoritative_until_compare_only_adoption_and_then_visible(tmp_path: Path):
    harness = _harness(tmp_path)
    first = ControlledGitPublicationAuthority(harness.session())
    review, locator = _publish_review(first)
    base = review.review_base_head
    assert base is not None and base.snapshot_id is not None
    subprocess.run(
        [
            "git",
            "-C",
            str(harness.store.repository),
            "update-ref",
            "refs/heads/desired/dev",
            review.candidate.snapshot_id.value.removeprefix("git-commit:"),
            base.snapshot_id.value.removeprefix("git-commit:"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RemoteAuthorityError, match="drift"):
        first.resolve_head(_DESIRED)

    second = ControlledGitPublicationAuthority(harness.session())
    result = _review_coordinator(second).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))

    assert result.outcome.state is PublicationOutcomeState.COMMITTED
    assert second.resolve_head(_DESIRED).snapshot_id == review.candidate.snapshot_id
    provider = ControlledGitWorkspacePlaneProvider(harness.source_root, second)
    visible = provider.snapshot(ResourcePlane.DESIRED, _DESIRED.value)
    assert visible.snapshot_id == review.candidate.snapshot_id
    assert visible.workspace.read("units/app.bin") == b"#!/bin/reviewed\x00\xff\n"
    assert visible.workspace.get_entry("units/app.bin").executable is True


def test_review_adoption_rejects_external_head_other_than_exact_candidate(tmp_path: Path):
    harness = _harness(tmp_path)
    authority = ControlledGitPublicationAuthority(harness.session())
    review, locator = _publish_review(authority)
    base = review.review_base_head
    assert base is not None and base.snapshot_id is not None
    other = _candidate(authority, b"other externally merged candidate")
    subprocess.run(
        [
            "git",
            "-C",
            str(harness.store.repository),
            "update-ref",
            "refs/heads/desired/dev",
            other.snapshot_id.value.removeprefix("git-commit:"),
            base.snapshot_id.value.removeprefix("git-commit:"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RemoteAuthorityError, match="accepted|candidate|external|drift"):
        _review_coordinator(authority).adopt(ReviewAdoptionCommand(_ENVIRONMENT, locator))
