"""Transactional publication conformance for durable Stack progression."""

from __future__ import annotations

import base64
import subprocess
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from dulwich.refs import Ref
from dulwich.repo import Repo

from gitopsctr.adapters.git import apply as git_apply
from gitopsctr.adapters.git.apply import (
    GitApplyCompatibilityWarning,
    GitApplyService,
    publish_durable_candidate,
)
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.sources import GitSourceRepository, GitSourceRetentionStore
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application.apply import ApplyCommand, AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.apply_orchestration import (
    ApplyCoordinationRequest,
    ApplyCoordinator,
    ApplyEnvironmentConfiguration,
    ApplyOrchestrationError,
    CandidatePublicationCoordinator,
    CandidatePublicationRequest,
    HmacApplyPublicationIdentityIssuer,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionPolicy,
    CandidateTransformation,
    CanonicalUnitProjectionCompiler,
    HmacRootIncarnationIssuer,
    ProjectedDocument,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    PublicationMode,
    PublicationOutcomeState,
    PublicationTarget,
    SnapshotId,
    SourceId,
)
from gitopsctr.application.sources import SourceRequest
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace, WorkspaceEntry
from gitopsctr.errors import OperationError
from gitopsctr.resource_api import JsonObject
from gitopsctr.resource_model import ResourcePlane

DESIRED = ChannelId("desired/dev")
OBSERVED = ChannelId("observed/dev")
CANDIDATE = ChannelId("candidate/dev")


@dataclass
class _EnvironmentResolver:
    def resolve(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyEnvironmentConfiguration:
        del command, changes
        return ApplyEnvironmentConfiguration(DESIRED, OBSERVED, CANDIDATE, ApplyProjectionPolicy())

    def close(self) -> None:
        """No owned resources."""


class _Validator:
    def validate_authored(self, document: JsonObject) -> None:
        del document

    def validate_desired(self, document: JsonObject) -> None:
        del document

    def validate_graph(self, documents: Mapping[tuple[str, str, str], ProjectedDocument]) -> None:
        del documents

    def validate_workspace(self, workspace: ImmutableWorkspace) -> None:
        del workspace


class _UnusedStackCompiler:
    def project(self, *_args: object, **_kwargs: object) -> CandidateTransformation:
        return CandidateTransformation(())


def _issuer() -> HmacApplyPublicationIdentityIssuer:
    return HmacApplyPublicationIdentityIssuer("durable-publication-tests", "publication-seed")


def _workspace(key: str, value: bytes) -> InMemoryWorkspace:
    return InMemoryWorkspace((WorkspaceEntry.file(key, value),), mutable=False)


def _working_repository(tmp_path: Path) -> tuple[Path, Path]:
    working = tmp_path / "working"
    origin = tmp_path / "origin.git"
    Repo.init(working, mkdir=True).close()
    origin.mkdir()
    Repo.init_bare(origin).close()
    subprocess.run(("git", "-C", str(working), "remote", "add", "origin", str(origin)), check=True)
    return working, origin


def _application_coordinator(
    origin: Path,
    store: GitPublicationStore,
    source: MemorySourceRepository,
) -> ApplyCoordinator:
    return ApplyCoordinator(
        GitSnapshotReader.from_path(origin),
        store,
        source,
        _EnvironmentResolver(),
        _Validator(),
        CanonicalUnitProjectionCompiler(),
        _UnusedStackCompiler(),
        HmacRootIncarnationIssuer("durable-publication-tests", "root-seed"),
        _issuer(),
    )


def _changes() -> AuthoredChangeSet:
    return AuthoredChangeSet(
        (
            _issue_authored_document(
                "next",
                {
                    "apiVersion": "unit.example.test/v1",
                    "kind": "Example",
                    "metadata": {"name": "app"},
                    "spec": {"value": "after-progress"},
                },
                ContentId("authored:after-progress"),
            ),
        )
    )


def _command() -> ApplyCommand:
    return ApplyCommand(EnvironmentId("dev"), ("next",), DESIRED, OBSERVED, CANDIDATE, None)


def _revision(snapshot_id: SnapshotId) -> str:
    return snapshot_id.value.removeprefix("git-commit:")


def _assert_public_and_managed_refs_match(origin: Path, channel: ChannelId, snapshot_id: SnapshotId) -> None:
    encoded = base64.urlsafe_b64encode(channel.value.encode()).decode().rstrip("=")
    repository = Repo(origin)
    try:
        public = repository.refs[Ref(f"refs/heads/{channel.value}".encode())]
        managed = repository.refs[Ref(f"refs/gitopsctr/publication/v1/channels/{encoded}".encode())]
        assert public == managed == _revision(snapshot_id).encode()
    finally:
        repository.close()


@pytest.mark.parametrize(
    "overrides",
    (
        {"environment_id": ""},
        {"environment_id": " dev"},
        {"environment_id": "dev\x00bad"},
        {"desired_channel": "desired/dev"},
        {"expected_desired_head": object()},
        {"expected_desired_head": HeadObservation.absent(OBSERVED, "observed-absent")},
        {"candidate": InMemoryWorkspace(mutable=True)},
        {"review_required": 1},
        {"review_required": True},
        {"candidate_channel": CANDIDATE},
        {"review_required": True, "candidate_channel": DESIRED},
        {"coordination_requests": []},
        {"coordination_requests": (object(),)},
        {"retained_sources": []},
        {"retained_sources": (object(),)},
        {"retained_sources": []},
        {"retained_sources": (object(),)},
    ),
)
def test_candidate_publication_request_rejects_open_or_incoherent_inputs(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "environment_id": "dev",
        "desired_channel": DESIRED,
        "expected_desired_head": HeadObservation.absent(DESIRED, "desired-absent"),
        "candidate": _workspace("candidate", b"value"),
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        CandidatePublicationRequest(**cast(Any, values))


def test_candidate_publication_request_validates_and_deduplicates_retained_sources() -> None:
    source = MemorySourceRepository(SourceId("retained-request-source"))
    snapshot = source.install(SnapshotId("retained-snapshot"), _workspace("source", b"source"))
    retained = source.retain(snapshot)
    expected = HeadObservation.absent(DESIRED, "desired-absent")
    candidate = _workspace("candidate", b"value")
    request = CandidatePublicationRequest("dev", DESIRED, expected, candidate, retained_sources=(retained,))
    assert request.retained_sources == (retained,)
    with pytest.raises(ValueError, match="unique snapshots"):
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            expected,
            candidate,
            retained_sources=(retained, retained),
        )


def test_candidate_publication_review_targets_only_candidate_with_coordination(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    store = GitPublicationStore(origin, MemorySourceRepository(SourceId("review-source")))
    publisher = CandidatePublicationCoordinator(store, _issuer())
    base = publisher.publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("base", b"base"))
    )
    assert base.snapshot_id is not None
    desired_before = store.prepare_head(DESIRED)

    reviewed = publisher.publish(
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            desired_before,
            _workspace("review", b"review"),
            True,
            CANDIDATE,
            (ApplyCoordinationRequest("review/request", "ready"),),
        )
    )

    assert reviewed.publication is not None
    assert reviewed.publication.target is PublicationTarget.REVIEW_CANDIDATE
    assert reviewed.publication.mode is PublicationMode.REVIEW_REQUIRED
    assert reviewed.publication.review_base_head == desired_before
    assert store.prepare_head(DESIRED) == desired_before
    assert store.prepare_head(CANDIDATE).snapshot_id == reviewed.snapshot_id
    assert store.coordination("review/request").value == "ready"


@dataclass
class _CountingAuthority:
    delegate: GitPublicationStore
    verify_calls: int = 0

    def verify(self, intent):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        return self.delegate.verify(intent)

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def test_candidate_publication_unknown_outcome_is_verified_exactly_once(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    store = GitPublicationStore(origin, MemorySourceRepository(SourceId("one-verify-source")))
    store.make_next_publication_unknown()
    authority = _CountingAuthority(store)
    publisher = CandidatePublicationCoordinator(cast(Any, authority), _issuer())

    result = publisher.publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("pending", b"pending"))
    )

    assert authority.verify_calls == 1
    assert result.publication_outcome is not None
    assert result.publication_outcome.state is PublicationOutcomeState.UNKNOWN


def test_candidate_publication_rejects_wrong_request_and_changed_sealed_content(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    store = GitPublicationStore(origin, MemorySourceRepository(SourceId("candidate-integrity-source")))
    publisher = CandidatePublicationCoordinator(store, _issuer())

    with pytest.raises(TypeError, match="request must be a CandidatePublicationRequest"):
        publisher.publish(cast(Any, object()))

    @dataclass
    class _ChangedContentAuthority:
        delegate: GitPublicationStore

        def seal_candidate(self, workspace):  # type: ignore[no-untyped-def]
            sealed = self.delegate.seal_candidate(workspace)
            object.__setattr__(sealed, "content_id", ContentId("changed-after-validation"))
            return sealed

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

    changed = CandidatePublicationCoordinator(cast(Any, _ChangedContentAuthority(store)), _issuer())
    with pytest.raises(ApplyOrchestrationError, match="sealed candidate differs"):
        changed.publish(
            CandidatePublicationRequest(
                "dev",
                DESIRED,
                store.prepare_head(DESIRED),
                _workspace("candidate", b"validated"),
            )
        )


@pytest.mark.parametrize(
    ("issuer_id", "identity_seed"),
    (("", "seed"), (" issuer", "seed"), ("issuer", ""), ("issuer", "seed\x00bad")),
)
def test_publication_identity_issuer_requires_canonical_configuration(issuer_id: str, identity_seed: str) -> None:
    with pytest.raises(ValueError, match="must be canonical text"):
        HmacApplyPublicationIdentityIssuer(issuer_id, identity_seed)


def test_durable_progress_publication_is_visible_and_chains_into_normal_apply(tmp_path: Path) -> None:
    working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("durable-source"))
    initial_store = GitPublicationStore(origin, source)
    initial = CandidatePublicationCoordinator(initial_store, _issuer()).publish(
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            initial_store.prepare_head(DESIRED),
            _workspace("baseline.txt", b"baseline"),
        )
    )
    assert initial.snapshot_id is not None
    candidate = tmp_path / "progressed"
    (candidate / "progress").mkdir(parents=True)
    (candidate / "baseline.txt").write_bytes(b"baseline")
    (candidate / "progress" / "state.json").write_bytes(b'{"ready":true}')

    progressed = publish_durable_candidate(
        working,
        "dev",
        DESIRED,
        _revision(initial.snapshot_id),
        candidate,
    )

    assert progressed.publication_outcome is not None
    assert progressed.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert progressed.snapshot_id is not None
    reader = GitSnapshotReader.from_path(origin)
    visible = GitWorkspacePlaneProvider(origin, reader, progressed.snapshot_id).snapshot(
        ResourcePlane.DESIRED, DESIRED.value
    )
    assert visible.snapshot_id == progressed.snapshot_id
    assert visible.workspace.read("progress/state.json") == b'{"ready":true}'
    _assert_public_and_managed_refs_match(origin, DESIRED, progressed.snapshot_id)

    next_source = MemorySourceRepository(SourceId("next-apply-source"))
    next_store = GitPublicationStore(origin, next_source)
    applied = _application_coordinator(origin, next_store, next_source).apply(_command(), _changes())

    assert applied.publication is not None
    assert applied.publication.expected_head.snapshot_id == progressed.snapshot_id
    assert applied.snapshot_id is not None
    after = GitSnapshotReader.from_path(origin).open_snapshot(applied.snapshot_id).workspace
    assert after.read("progress/state.json") == b'{"ready":true}'
    assert after.read("units/app.json")
    _assert_public_and_managed_refs_match(origin, DESIRED, applied.snapshot_id)


def test_candidate_progress_publication_rejects_a_stale_expected_head(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("stale-source"))
    store = GitPublicationStore(origin, source)
    publisher = CandidatePublicationCoordinator(store, _issuer())
    stale = store.prepare_head(DESIRED)
    competitor = publisher.publish(CandidatePublicationRequest("dev", DESIRED, stale, _workspace("winner", b"winner")))
    assert competitor.snapshot_id is not None

    with pytest.raises(ValueError, match="expected head is stale"):
        publisher.publish(CandidatePublicationRequest("dev", DESIRED, stale, _workspace("loser", b"loser")))

    assert store.prepare_head(DESIRED).snapshot_id == competitor.snapshot_id
    _assert_public_and_managed_refs_match(origin, DESIRED, competitor.snapshot_id)


@dataclass
class _StaleOwnershipAuthority:
    delegate: GitPublicationStore
    source_id: object
    moved: bool = False

    def ownership(self, source_id):  # type: ignore[no-untyped-def]
        observed = self.delegate.ownership(source_id)
        if not self.moved:
            self.delegate.set_ownership(source_id, OwnershipId("competing-owner"))
            self.moved = True
        return observed

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def test_candidate_publication_transfers_retained_ownership_and_rejects_stale_owner(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("owned-durable-source"))
    snapshot = source.install(SnapshotId("owned-v1"), _workspace("source.txt", b"source"))
    retained = source.retain(snapshot)
    store = GitPublicationStore(origin, source)
    store.set_ownership(snapshot.source_snapshot_id, OwnershipId("prior-owner"))

    published = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            store.prepare_head(DESIRED),
            _workspace("candidate.txt", b"candidate"),
            retained_sources=(retained,),
        )
    )

    assert published.publication is not None
    assert published.publication.source_ownership_changes[0].retained_source == retained
    assert store.ownership(snapshot.source_snapshot_id).owner == published.publication.publication_owner

    stale_channel = ChannelId("desired/stale-owner")
    stale_store = GitPublicationStore(origin, source)
    authority = _StaleOwnershipAuthority(stale_store, snapshot.source_snapshot_id)
    with pytest.raises(ValueError, match="source ownership is stale"):
        CandidatePublicationCoordinator(cast(Any, authority), _issuer()).publish(
            CandidatePublicationRequest(
                "dev",
                stale_channel,
                stale_store.prepare_head(stale_channel),
                _workspace("stale.txt", b"stale"),
                retained_sources=(retained,),
            )
        )
    assert stale_store.prepare_head(stale_channel).is_absent


def test_retained_ownership_survives_crash_verification_and_fresh_recovery(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("owned-recovery-source"))
    snapshot = source.install(SnapshotId("owned-recovery-v1"), _workspace("source.txt", b"source"))
    retained = source.retain(snapshot)
    store = GitPublicationStore(origin, source)
    store.make_next_publication_crash_after_ref()

    crashed = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            store.prepare_head(DESIRED),
            _workspace("candidate.txt", b"candidate"),
            retained_sources=(retained,),
        )
    )

    assert crashed.snapshot_id is not None and crashed.recovery_locator is not None
    fresh_store = GitPublicationStore(origin, source)
    recovered = CandidatePublicationCoordinator(fresh_store, _issuer()).recover(crashed.recovery_locator)
    assert recovered.snapshot_id == crashed.snapshot_id
    assert recovered.publication is not None
    assert fresh_store.ownership(snapshot.source_snapshot_id).owner == recovered.publication.publication_owner


def test_durable_git_helper_derives_and_atomically_owns_retained_unit_source(tmp_path: Path) -> None:
    working, origin = _working_repository(tmp_path)
    subprocess.run(("git", "-C", str(working), "config", "user.email", "test@example.test"), check=True)
    subprocess.run(("git", "-C", str(working), "config", "user.name", "Test"), check=True)
    (working / "source.txt").write_bytes(b"source")
    subprocess.run(("git", "-C", str(working), "add", "source.txt"), check=True)
    subprocess.run(("git", "-C", str(working), "commit", "-m", "source"), check=True, capture_output=True)
    revision = subprocess.run(
        ("git", "-C", str(working), "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()
    retention_root = git_apply._ensure_retention_root(working)
    source_repository = GitSourceRepository.from_path(git_apply._DEFAULT_GIT_SOURCE_ID, working, retention_root)
    retained = source_repository.retain(
        source_repository.resolve(SourceRequest(git_apply._DEFAULT_GIT_SOURCE_ID, revision))
    )
    store = GitPublicationStore(origin, source_repository)
    base = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("base", b"base"))
    )
    assert base.snapshot_id is not None
    candidate = tmp_path / "owned-candidate"
    (candidate / "units").mkdir(parents=True)
    (candidate / "units" / "producer.json").write_text(
        '{"apiVersion":"unit.gitopsctr.io/v1","kind":"Terraform","metadata":{"name":"producer"},'
        f'"spec":{{"source":{{"path":".","revision":"{revision}"}}}}}}'
    )

    result = publish_durable_candidate(working, "dev", DESIRED, _revision(base.snapshot_id), candidate)

    assert result.publication is not None
    assert tuple(
        change.retained_source.source_snapshot_id for change in result.publication.source_ownership_changes
    ) == (retained.source_snapshot_id,)
    assert GitPublicationStore(origin, source_repository).ownership(retained.source_snapshot_id).owner == (
        result.publication.publication_owner
    )


def test_missing_durable_source_transport_releases_earlier_fresh_retention(tmp_path: Path) -> None:
    working, origin = _working_repository(tmp_path)
    subprocess.run(("git", "-C", str(working), "config", "user.email", "test@example.test"), check=True)
    subprocess.run(("git", "-C", str(working), "config", "user.name", "Test"), check=True)
    (working / "source.txt").write_bytes(b"source")
    subprocess.run(("git", "-C", str(working), "add", "source.txt"), check=True)
    subprocess.run(("git", "-C", str(working), "commit", "-m", "source"), check=True, capture_output=True)
    revision = subprocess.run(
        ("git", "-C", str(working), "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()
    retention_root = git_apply._ensure_retention_root(working)
    source_repository = GitSourceRepository.from_path(git_apply._DEFAULT_GIT_SOURCE_ID, working, retention_root)
    store = GitPublicationStore(origin, source_repository)
    base = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("base", b"base"))
    )
    assert base.snapshot_id is not None
    candidate = tmp_path / "missing-source-candidate"
    (candidate / "units").mkdir(parents=True)
    (candidate / "stack-templates").mkdir()
    (candidate / "units" / "producer.json").write_text(
        '{"apiVersion":"unit.gitopsctr.io/v1","kind":"Terraform","metadata":{"name":"producer"},'
        f'"spec":{{"source":{{"path":".","revision":"{revision}"}}}}}}'
    )
    missing_repository = (tmp_path / "missing-external.git").resolve().as_uri()
    (candidate / "stack-templates" / "external.json").write_text(
        '{"apiVersion":"gitopsctr.io/v1","kind":"StackTemplate","metadata":{"name":"external"},'
        f'"spec":{{"sourceContext":{{"repository":"{missing_repository}","revision":"{"f" * 40}"}}}}}}'
    )

    with pytest.raises(OperationError, match="unavailable"):
        publish_durable_candidate(working, "dev", DESIRED, _revision(base.snapshot_id), candidate)

    source_snapshot_id = source_repository.resolve(
        SourceRequest(git_apply._DEFAULT_GIT_SOURCE_ID, revision)
    ).source_snapshot_id
    assert GitSourceRetentionStore(retention_root).retained_snapshot(source_snapshot_id) is None
    assert GitPublicationStore(origin, source_repository).prepare_head(DESIRED).snapshot_id == base.snapshot_id


@dataclass
class _PublishedApply:
    result: object
    snapshot_reader: GitSnapshotReader

    def apply(self, _command: object, _changes: object):  # type: ignore[no-untyped-def]
        return self.result

    def close(self) -> None:
        self.snapshot_reader.close()


@pytest.mark.parametrize("failed_step", ("cache", "pins"))
def test_committed_apply_result_survives_retryable_compatibility_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: str,
) -> None:
    working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("compatibility-source"))
    store = GitPublicationStore(origin, source)
    committed = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("state", b"accepted"))
    )
    assert committed.recovery_locator is not None
    service = GitApplyService(working)
    service._coordinator = cast(Any, _PublishedApply(committed, GitSnapshotReader.from_path(origin)))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OperationError(f"injected {failed_step} failure")

    if failed_step == "cache":
        monkeypatch.setattr(git_apply, "_cache_published_snapshot", fail)
    else:
        monkeypatch.setattr(git_apply, "_mirror_legacy_controller_pins", fail)
    with pytest.warns(GitApplyCompatibilityWarning, match=f".*injected {failed_step}"):
        returned = service.apply(_command(), _changes())

    assert returned is committed
    assert returned.recovery_locator == committed.recovery_locator


@pytest.mark.parametrize("failed_step", ("cache", "pins"))
def test_committed_apply_result_survives_hostile_warning_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: str,
) -> None:
    working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("hostile-warning-source"))
    store = GitPublicationStore(origin, source)
    committed = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("state", b"accepted"))
    )
    service = GitApplyService(working)
    service._coordinator = cast(Any, _PublishedApply(committed, GitSnapshotReader.from_path(origin)))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OperationError(f"injected {failed_step} failure")

    if failed_step == "cache":
        monkeypatch.setattr(git_apply, "_cache_published_snapshot", fail)
    else:
        monkeypatch.setattr(git_apply, "_mirror_legacy_controller_pins", fail)

    if failed_step == "cache":
        with warnings.catch_warnings():
            warnings.simplefilter("error", GitApplyCompatibilityWarning)
            returned = service.apply(_command(), _changes())
    else:
        monkeypatch.setattr(warnings, "showwarning", fail)
        returned = service.apply(_command(), _changes())

    assert returned is committed
    assert returned.recovery_locator == committed.recovery_locator


def test_durable_git_helper_supports_review_and_rejects_stale_or_symlink_candidates(tmp_path: Path) -> None:
    working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("durable-helper-source"))
    store = GitPublicationStore(origin, source)
    base = CandidatePublicationCoordinator(store, _issuer()).publish(
        CandidatePublicationRequest("dev", DESIRED, store.prepare_head(DESIRED), _workspace("base", b"base"))
    )
    assert base.snapshot_id is not None
    candidate = tmp_path / "review-candidate"
    candidate.mkdir()
    (candidate / "payload").write_bytes(b"reviewed")

    reviewed = publish_durable_candidate(
        working,
        "dev",
        DESIRED,
        _revision(base.snapshot_id),
        candidate,
        candidate_channel=CANDIDATE,
    )

    assert reviewed.publication is not None
    assert reviewed.publication.target is PublicationTarget.REVIEW_CANDIDATE
    assert reviewed.publication.review_base_head == store.prepare_head(DESIRED)
    assert GitPublicationStore(origin, source).prepare_head(DESIRED).snapshot_id == base.snapshot_id
    assert GitPublicationStore(origin, source).prepare_head(CANDIDATE).snapshot_id == reviewed.snapshot_id

    with pytest.raises(OperationError, match="expected desired head is stale"):
        publish_durable_candidate(working, "dev", DESIRED, "f" * 40, candidate)

    unsafe = tmp_path / "unsafe-candidate"
    unsafe.mkdir()
    (unsafe / "target").write_bytes(b"target")
    (unsafe / "link").symlink_to("target")
    with pytest.raises(OperationError, match="cannot contain symbolic links"):
        publish_durable_candidate(
            working,
            "dev",
            DESIRED,
            _revision(base.snapshot_id),
            unsafe,
        )


def test_candidate_progress_crash_and_unknown_are_recoverable_from_durable_locators(tmp_path: Path) -> None:
    _working, origin = _working_repository(tmp_path)
    source = MemorySourceRepository(SourceId("recovery-source"))
    store = GitPublicationStore(origin, source)
    publisher = CandidatePublicationCoordinator(store, _issuer())
    store.make_next_publication_crash_after_ref()

    crashed = publisher.publish(
        CandidatePublicationRequest(
            "dev",
            DESIRED,
            store.prepare_head(DESIRED),
            _workspace("progress/crash.json", b"{}"),
        )
    )

    assert crashed.publication_outcome is not None
    assert crashed.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert crashed.snapshot_id is not None and crashed.recovery_locator is not None
    fresh_store = GitPublicationStore(origin, MemorySourceRepository(SourceId("fresh-recovery-source")))
    recovered = CandidatePublicationCoordinator(fresh_store, _issuer()).recover(crashed.recovery_locator)
    assert recovered.snapshot_id == crashed.snapshot_id
    assert recovered.publication_outcome == crashed.publication_outcome
    _assert_public_and_managed_refs_match(origin, DESIRED, crashed.snapshot_id)

    unknown_channel = ChannelId("desired/pending")
    pending_store = GitPublicationStore(origin, MemorySourceRepository(SourceId("pending-source")))
    pending_store.make_next_publication_unknown()
    pending = CandidatePublicationCoordinator(pending_store, _issuer()).publish(
        CandidatePublicationRequest(
            "dev",
            unknown_channel,
            pending_store.prepare_head(unknown_channel),
            _workspace("progress/pending.json", b"{}"),
        )
    )
    assert pending.publication_outcome is not None
    assert pending.publication_outcome.state is PublicationOutcomeState.UNKNOWN
    assert pending.snapshot_id is None and pending.recovery_locator is not None
    fresh_pending = GitPublicationStore(origin, MemorySourceRepository(SourceId("fresh-pending-source")))
    still_unknown = CandidatePublicationCoordinator(fresh_pending, _issuer()).recover(pending.recovery_locator)
    assert still_unknown.publication_outcome is not None
    assert still_unknown.publication_outcome.state is PublicationOutcomeState.UNKNOWN
    assert still_unknown.snapshot_id is None
    assert fresh_pending.prepare_head(unknown_channel).is_absent
