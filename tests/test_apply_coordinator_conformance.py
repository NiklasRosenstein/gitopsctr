"""Conformance for the backend-neutral apply publication coordinator."""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest
from dulwich.repo import Repo

from gitopsctr.adapters.git.apply import UnsupportedGitPublicationAuthority, local_bare_publication_authority
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application.apply import (
    ApplyCommand,
    AuthoredChangeSet,
    _issue_authored_document,
    _issue_authored_source_acquisition,
)
from gitopsctr.application.apply_orchestration import (
    ApplyCoordinationRequest,
    ApplyCoordinator,
    ApplyEnvironmentConfiguration,
    HmacApplyPublicationIdentityIssuer,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionPolicy,
    CandidateTransformation,
    CanonicalUnitProjectionCompiler,
    HmacRootIncarnationIssuer,
    ProjectedDocument,
    RetainedSourceDescriptor,
    SourceBindingRole,
    _issue_retained_source_descriptor,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationRecovery,
    PublicationRecoveryLocator,
    PublicationStoreId,
    PublicationTarget,
    RetainedSource,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.sources import SourceRequest, SourceRetentionError, SourceSnapshot
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace, WorkspaceEntry
from gitopsctr.resource_api import JsonObject
from gitopsctr.resource_model import ResourcePlane

DESIRED = ChannelId("desired/dev")
OBSERVED = ChannelId("observed/dev")
REVIEW = ChannelId("candidate/dev")


def _document(value: str = "next") -> JsonObject:
    return {
        "apiVersion": "unit.example.test/v1",
        "kind": "Example",
        "metadata": {"name": "app"},
        "spec": {"value": value},
    }


def _changes(value: str = "next") -> AuthoredChangeSet:
    return AuthoredChangeSet((_issue_authored_document("input:app", _document(value), ContentId(f"authored:{value}")),))


def _command(*, dry_run: bool = False, source_request: SourceRequest | None = None) -> ApplyCommand:
    return ApplyCommand(
        EnvironmentId("dev"),
        ("input:app",),
        DESIRED,
        OBSERVED,
        REVIEW,
        source_request,
        dry_run=dry_run,
    )


@dataclass
class _EnvironmentResolver:
    review_required: bool = False
    primary_source: RetainedSourceDescriptor | None = None
    coordination_requests: tuple[ApplyCoordinationRequest, ...] = ()
    closed: bool = False

    def resolve(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyEnvironmentConfiguration:
        del command, changes
        return ApplyEnvironmentConfiguration(
            DESIRED,
            OBSERVED,
            REVIEW,
            ApplyProjectionPolicy(review_required=self.review_required),
            primary_source=self.primary_source,
            coordination_requests=self.coordination_requests,
        )

    def close(self) -> None:
        self.closed = True


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


@dataclass
class _Authority:
    store: InMemorySnapshotStore
    execute_behavior: str = "normal"
    verify_calls: int = 0
    intents: dict[object, PublicationIntent] = field(default_factory=dict)
    outcomes: dict[object, PublicationOutcome] = field(default_factory=dict)
    candidate_parents: dict[SnapshotId, SnapshotId | None] = field(default_factory=dict)
    _pending_candidate_parents: dict[int, SnapshotId | None] = field(default_factory=dict)
    publication_store_id: PublicationStoreId = PublicationStoreId("test-apply-publication")

    def prepare_head(self, channel_id: ChannelId) -> HeadObservation:
        return self.store.resolve_head(channel_id)

    def ownership(self, source: SourceSnapshotId) -> OwnershipObservation:
        return self.store.ownership(source)

    def coordination(self, key: str):  # type: ignore[no-untyped-def]
        return self.store.coordination(key)

    def begin_candidate(
        self,
        base: ImmutableWorkspace | None = None,
        parent_snapshot_id: SnapshotId | None = None,
    ):  # type: ignore[no-untyped-def]
        candidate = self.store.begin_candidate(base, parent_snapshot_id)
        self._pending_candidate_parents[id(candidate)] = parent_snapshot_id
        return candidate

    def seal_candidate(self, workspace):  # type: ignore[no-untyped-def]
        candidate = self.store.seal_candidate(workspace)
        self.candidate_parents[candidate.snapshot_id] = self._pending_candidate_parents.pop(id(workspace))
        if self.execute_behavior == "wrong-content":
            object.__setattr__(candidate, "content_id", ContentId("tampered-candidate-content"))
        return candidate

    def execute(self, intent):  # type: ignore[no-untyped-def]
        self.intents[intent.attempt_id] = intent
        if self.execute_behavior == "stale-head":
            competing = self.store.begin_candidate()
            competing.write("race", b"winner")
            self.store.set_head(intent.channel_id, self.store.seal_candidate(competing).snapshot_id)
        elif self.execute_behavior == "stale-review-base":
            assert intent.review_base_head is not None
            competing = self.store.begin_candidate()
            competing.write("accepted-race", b"winner")
            self.store.set_head(
                intent.review_base_head.channel_id,
                self.store.seal_candidate(competing).snapshot_id,
            )
        elif self.execute_behavior == "stale-ownership":
            raise ValueError("publication source ownership is stale")
        elif self.execute_behavior == "stale-coordination":
            assert intent.coordination_changes
            raise ValueError("publication coordination fence is stale")
        elif self.execute_behavior == "ordinary-error":
            raise ValueError("ordinary validation failure")
        elif self.execute_behavior == "not-committed":
            outcome = PublicationOutcome(PublicationOutcomeState.NOT_COMMITTED)
            self.outcomes[intent.attempt_id] = outcome
            return outcome
        outcome = self.store.execute(intent)
        self.outcomes[intent.attempt_id] = outcome
        return outcome

    def verify(self, intent):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        outcome = self.store.verify(intent)
        self.outcomes[intent.attempt_id] = outcome
        return outcome

    def recovery_locator(self, intent):  # type: ignore[no-untyped-def]
        return PublicationRecoveryLocator(self.publication_store_id, intent.attempt_id)

    def recover_publication(self, locator: PublicationRecoveryLocator) -> PublicationRecovery:
        intent = self.intents[locator.attempt_id]
        outcome = self.outcomes[locator.attempt_id]
        return PublicationRecovery(intent, outcome)

    def close(self) -> None:
        self.store.close()


@dataclass
class _CountingGitAuthority:
    store: GitPublicationStore
    verify_calls: int = 0

    def prepare_head(self, channel_id):  # type: ignore[no-untyped-def]
        return self.store.prepare_head(channel_id)

    def ownership(self, source):  # type: ignore[no-untyped-def]
        return self.store.ownership(source)

    def coordination(self, key):  # type: ignore[no-untyped-def]
        return self.store.coordination(key)

    def begin_candidate(self, base=None, parent_snapshot_id=None):  # type: ignore[no-untyped-def]
        return self.store.begin_candidate(base, parent_snapshot_id)

    def seal_candidate(self, workspace):  # type: ignore[no-untyped-def]
        return self.store.seal_candidate(workspace)

    def execute(self, intent):  # type: ignore[no-untyped-def]
        return self.store.execute(intent)

    def verify(self, intent):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        return self.store.verify(intent)

    def recovery_locator(self, intent):  # type: ignore[no-untyped-def]
        return self.store.recovery_locator(intent)

    def recover_publication(self, locator):  # type: ignore[no-untyped-def]
        return self.store.recover_publication(locator)

    def close(self) -> None:
        self.store.close()


@dataclass
class _CountingSourceRepository:
    delegate: MemorySourceRepository
    resolve_calls: int = 0
    recover_calls: int = 0
    release_calls: int = 0

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        self.resolve_calls += 1
        return self.delegate.resolve(request)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        return self.delegate.retain(source)

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        self.recover_calls += 1
        return self.delegate.recover(retained)

    def release(self, retained: RetainedSource) -> None:
        self.release_calls += 1
        self.delegate.release(retained)

    def reissue(self, locator):  # type: ignore[no-untyped-def]
        return self.delegate.reissue(locator)

    def close(self) -> None:
        self.delegate.close()


def _coordinator(
    *,
    review_required: bool = False,
    authority: _Authority | None = None,
    source_repository: Any | None = None,
    primary_source: RetainedSourceDescriptor | None = None,
    coordination_requests: tuple[ApplyCoordinationRequest, ...] = (),
) -> tuple[ApplyCoordinator, _Authority]:
    selected = authority or _Authority(InMemorySnapshotStore())
    source = source_repository or MemorySourceRepository(SourceId("unused-source"))
    return (
        ApplyCoordinator(
            selected.store,
            selected,
            source,
            _EnvironmentResolver(review_required, primary_source, coordination_requests),
            _Validator(),
            CanonicalUnitProjectionCompiler(),
            _UnusedStackCompiler(),
            HmacRootIncarnationIssuer("coordinator-tests", "root-seed"),
            HmacApplyPublicationIdentityIssuer("coordinator-tests", "publication-seed"),
        ),
        selected,
    )


def _git_coordinator(
    repository: Path,
    authority: _CountingGitAuthority,
    source_repository: _CountingSourceRepository,
    primary_source: RetainedSourceDescriptor,
) -> ApplyCoordinator:
    return ApplyCoordinator(
        GitSnapshotReader.from_path(repository),
        authority,
        source_repository,
        _EnvironmentResolver(primary_source=primary_source),
        _Validator(),
        CanonicalUnitProjectionCompiler(),
        _UnusedStackCompiler(),
        HmacRootIncarnationIssuer("coordinator-tests", "root-seed"),
        HmacApplyPublicationIdentityIssuer("coordinator-tests", "publication-seed"),
    )


def _seed_desired_head(authority: _Authority) -> HeadObservation:
    workspace = authority.store.begin_candidate(InMemoryWorkspace(mutable=False))
    candidate = authority.store.seal_candidate(workspace)
    authority.store.set_head(DESIRED, candidate.snapshot_id)
    return authority.prepare_head(DESIRED)


def _walk(value: object) -> tuple[object, ...]:
    seen: set[int] = set()
    values: list[object] = []

    def visit(item: object) -> None:
        if id(item) in seen or isinstance(item, (str, bytes, int, float, bool, type(None))):
            return
        seen.add(id(item))
        values.append(item)
        if is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, dict):
            for key, nested in item.items():
                visit(key)
                visit(nested)
        elif isinstance(item, (tuple, list, set, frozenset)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(values)


def test_direct_apply_returns_committed_proof_and_is_immediately_readable() -> None:
    coordinator, authority = _coordinator()
    result = coordinator.apply(_command(), _changes())

    assert result.publication_outcome is not None
    assert result.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert result.publication_outcome.proof is not None
    assert result.publication_outcome.proof.intent is result.publication
    assert result.publication is not None
    assert result.publication.target is PublicationTarget.ACCEPTED_DESIRED
    assert result.publication.channel_id == DESIRED
    assert result.snapshot_id == result.publication.candidate.snapshot_id
    assert result.publication.candidate.content_id == authority.store.open_snapshot(result.snapshot_id).content_id
    visible = authority.prepare_head(DESIRED)
    assert visible.snapshot_id == result.snapshot_id
    assert authority.store.open_snapshot(visible.snapshot_id).workspace.read("units/app.json")
    assert result.recovery_locator is not None
    recovered = coordinator.recover(result.recovery_locator)
    assert recovered.publication_outcome == result.publication_outcome
    assert recovered.snapshot_id == result.snapshot_id

    leaked = _walk(result)
    assert not any(isinstance(value, Path) for value in leaked)
    assert not any(type(value).__module__.startswith("gitopsctr.adapters") for value in leaked)
    assert not any(type(value).__module__ == "gitopsctr.controller" for value in leaked)


def test_git_commit_on_local_bare_authority_is_immediately_visible_to_existing_plane_reader(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "authority.git"
    origin.mkdir()
    Repo.init_bare(origin).close()
    source = MemorySourceRepository(SourceId("publication-source"))
    store = GitPublicationStore(origin, source)
    candidate_workspace = store.begin_candidate()
    candidate_workspace.write("units/app.json", b'{"kind":"Example"}')
    candidate = store.seal_candidate(candidate_workspace)
    intent = PublicationIntent(
        PublicationAttemptId("local-bare-visible"),
        DESIRED,
        store.prepare_head(DESIRED),
        candidate,
        (),
        OwnershipId("publication-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )

    outcome = store.execute(intent)

    assert outcome.state is PublicationOutcomeState.COMMITTED
    reader = GitSnapshotReader.from_path(origin)
    provider = GitWorkspacePlaneProvider(origin, reader, candidate.snapshot_id)
    visible = provider.snapshot(ResourcePlane.DESIRED, DESIRED.value)
    assert visible.snapshot_id == candidate.snapshot_id
    assert visible.workspace.read("units/app.json") == b'{"kind":"Example"}'


def test_git_apply_accepts_only_a_local_bare_origin_as_publication_authority(tmp_path: Path) -> None:
    working = tmp_path / "working"
    authority = tmp_path / "authority.git"
    Repo.init(working, mkdir=True).close()
    authority.mkdir()
    Repo.init_bare(authority).close()
    subprocess.run(("git", "-C", str(working), "remote", "add", "origin", str(authority)), check=True)

    assert local_bare_publication_authority(working) == authority.resolve()

    subprocess.run(
        ("git", "-C", str(working), "remote", "set-url", "origin", "https://example.test/project.git"),
        check=True,
    )
    with pytest.raises(UnsupportedGitPublicationAuthority, match="non-local origins"):
        local_bare_publication_authority(working)


def test_review_apply_targets_only_candidate_and_binds_the_accepted_base() -> None:
    request = ApplyCoordinationRequest("review/request", "request-1")
    coordinator, authority = _coordinator(review_required=True, coordination_requests=(request,))
    absent_before = authority.prepare_head(DESIRED)
    result = coordinator.apply(_command(), _changes())
    accepted = authority.prepare_head(DESIRED)

    assert absent_before.is_absent
    assert not accepted.is_absent
    assert result.publication is not None
    assert result.publication.target is PublicationTarget.REVIEW_CANDIDATE
    assert result.publication.channel_id == REVIEW
    assert result.publication.review_base_head == accepted
    assert authority.candidate_parents[result.publication.candidate.snapshot_id] == accepted.snapshot_id
    assert result.publication.coordination_changes[0].key == request.key
    assert result.publication.coordination_changes[0].next_value == request.next_value
    assert authority.prepare_head(DESIRED) == accepted
    assert authority.prepare_head(REVIEW).snapshot_id == result.snapshot_id
    assert authority.coordination(request.key).value == request.next_value


def test_candidate_content_and_stale_target_head_fail_closed() -> None:
    wrong_authority = _Authority(InMemorySnapshotStore(), "wrong-content")
    coordinator, _ = _coordinator(authority=wrong_authority)
    with pytest.raises(ValueError, match="sealed candidate differs"):
        coordinator.apply(_command(), _changes())

    racing_authority = _Authority(InMemorySnapshotStore(), "stale-head")
    coordinator, _ = _coordinator(authority=racing_authority)
    with pytest.raises(ValueError, match="expected head is stale"):
        coordinator.apply(_command(), _changes())


def test_stale_coordination_fence_is_rejected_before_publication() -> None:
    authority = _Authority(InMemorySnapshotStore(), "stale-coordination")
    _seed_desired_head(authority)
    coordinator, _ = _coordinator(
        review_required=True,
        authority=authority,
        coordination_requests=(ApplyCoordinationRequest("review/request", "request-1"),),
    )
    with pytest.raises(ValueError, match="coordination fence is stale"):
        coordinator.apply(_command(), _changes())


def test_review_publication_rejects_a_stale_accepted_base() -> None:
    authority = _Authority(InMemorySnapshotStore(), "stale-review-base")
    _seed_desired_head(authority)
    coordinator, _ = _coordinator(review_required=True, authority=authority)
    with pytest.raises(ValueError, match="accepted desired base head is stale"):
        coordinator.apply(_command(), _changes())


def test_unknown_publication_is_verified_once_and_never_claimed_committed() -> None:
    committed_authority = _Authority(InMemorySnapshotStore())
    committed_authority.store.make_next_publication_ambiguous()
    coordinator, _ = _coordinator(authority=committed_authority)
    recovered = coordinator.apply(_command(), _changes())
    assert committed_authority.verify_calls == 1
    assert recovered.publication_outcome is not None
    assert recovered.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert recovered.snapshot_id is not None

    unknown_authority = _Authority(InMemorySnapshotStore())
    unknown_authority.store.make_next_publication_unknown()
    coordinator, _ = _coordinator(authority=unknown_authority)
    unknown = coordinator.apply(_command(), _changes())
    assert unknown_authority.verify_calls == 1
    assert unknown.publication_outcome is not None
    assert unknown.publication_outcome.state is PublicationOutcomeState.UNKNOWN
    assert unknown.snapshot_id is None
    assert unknown.publication is not None


def _source_changes(
    value: str = "next",
) -> tuple[AuthoredChangeSet, _CountingSourceRepository, RetainedSourceDescriptor]:
    delegate = MemorySourceRepository(SourceId("authored-source"))
    workspace = InMemoryWorkspace((WorkspaceEntry.file("unit.json", b"{}"),), mutable=False)
    snapshot = delegate.install(SnapshotId("source-v1"), workspace)
    retained = delegate.retain(snapshot)
    acquisition = _issue_authored_source_acquisition(snapshot, retained)
    descriptor = _issue_retained_source_descriptor(
        retained,
        "authored",
        SourceBindingRole.PRIMARY_AUTHORED,
        "unit.json",
        ContentId("selector-evidence"),
    )
    changes = AuthoredChangeSet(
        (_issue_authored_document("source:unit.json", _document(value), ContentId(f"authored:source:{value}")),),
        snapshot.source_snapshot_id,
        acquisition,
    )
    return changes, _CountingSourceRepository(delegate), descriptor


def test_source_backed_apply_recovers_exact_decoded_source_without_resolving_selector_again() -> None:
    changes, source, descriptor = _source_changes()
    coordinator, _ = _coordinator(source_repository=source, primary_source=descriptor)
    result = coordinator.apply(
        _command(dry_run=True, source_request=SourceRequest(SourceId("authored-source"), "moving-main")),
        changes,
    )

    assert result.publication is None
    assert source.resolve_calls == 0
    assert source.recover_calls == 1
    assert source.release_calls == 1


def test_stale_source_ownership_and_definite_noncommit_never_return_a_snapshot() -> None:
    changes, source, descriptor = _source_changes()
    stale_authority = _Authority(InMemorySnapshotStore(), "stale-ownership")
    coordinator, _ = _coordinator(
        authority=stale_authority,
        source_repository=source,
        primary_source=descriptor,
    )
    with pytest.raises(ValueError, match="ownership is stale"):
        coordinator.apply(
            _command(source_request=SourceRequest(SourceId("authored-source"), "moving-main")),
            changes,
        )
    assert source.resolve_calls == 0
    assert source.recover_calls == 1
    assert source.release_calls == 1

    not_committed = _Authority(InMemorySnapshotStore(), "not-committed")
    coordinator, _ = _coordinator(authority=not_committed)
    result = coordinator.apply(_command(), _changes())
    assert result.snapshot_id is None
    assert result.publication_outcome is not None
    assert result.publication_outcome.state is PublicationOutcomeState.NOT_COMMITTED


@pytest.mark.parametrize("definite_failure", (False, True), ids=("no-change", "ordinary-failure"))
def test_existing_snapshot_owner_does_not_adopt_a_fresh_retention_handle(definite_failure: bool) -> None:
    authority = _Authority(InMemorySnapshotStore())
    baseline, _ = _coordinator(authority=authority)
    assert baseline.apply(_command(), _changes()).publication_outcome is not None

    value = "changed" if definite_failure else "next"
    changes, source, descriptor = _source_changes(value)
    acquisition = changes.source_acquisition
    assert acquisition is not None
    existing_retained = source.delegate.retain(acquisition.snapshot)
    authority.store.set_ownership(acquisition.snapshot.source_snapshot_id, OwnershipId("existing-owner"))
    authority.execute_behavior = "ordinary-error" if definite_failure else "normal"
    coordinator, _ = _coordinator(
        authority=authority,
        source_repository=source,
        primary_source=descriptor,
    )

    if definite_failure:
        with pytest.raises(ValueError, match="ordinary validation failure"):
            coordinator.apply(
                _command(source_request=SourceRequest(SourceId("authored-source"), "moving-main")),
                changes,
            )
    else:
        result = coordinator.apply(
            _command(source_request=SourceRequest(SourceId("authored-source"), "moving-main")),
            changes,
        )
        assert result.publication is None
        assert result.publication_outcome is None

    assert authority.verify_calls == 0
    assert source.release_calls == 1
    with pytest.raises(SourceRetentionError):
        source.delegate.recover(acquisition.retained)
    assert source.delegate.recover(existing_retained).source_snapshot_id == acquisition.snapshot.source_snapshot_id


def test_git_post_ref_crash_verifies_once_preserves_source_and_recovers_with_fresh_store(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "authority.git"
    origin.mkdir()
    Repo.init_bare(origin).close()
    changes, source, descriptor = _source_changes()
    acquisition = changes.source_acquisition
    assert acquisition is not None
    store = GitPublicationStore(origin, source)
    store.make_next_publication_crash_after_ref()
    authority = _CountingGitAuthority(store)
    coordinator = _git_coordinator(origin, authority, source, descriptor)

    result = coordinator.apply(
        _command(source_request=SourceRequest(SourceId("authored-source"), "moving-main")),
        changes,
    )

    assert authority.verify_calls == 1
    assert result.publication_outcome is not None
    assert result.publication_outcome.state is PublicationOutcomeState.COMMITTED
    assert result.snapshot_id is not None
    assert result.recovery_locator is not None
    assert source.release_calls == 0
    assert source.delegate.recover(acquisition.retained).source_snapshot_id == acquisition.snapshot.source_snapshot_id

    fresh_store = GitPublicationStore(origin, source)
    fresh_authority = _CountingGitAuthority(fresh_store)
    fresh_coordinator = _git_coordinator(origin, fresh_authority, source, descriptor)
    recovered = fresh_coordinator.recover(result.recovery_locator)

    assert recovered.publication_outcome == result.publication_outcome
    assert recovered.snapshot_id == result.snapshot_id
    visible = fresh_authority.prepare_head(DESIRED)
    assert visible.snapshot_id == result.snapshot_id
    assert GitSnapshotReader.from_path(origin).open_snapshot(result.snapshot_id).workspace.read("units/app.json")


def _application_import_targets(path: Path, node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if not node.level:
        base = node.module.split(".") if node.module else []
    else:
        relative = path.relative_to(Path(__file__).parents[1] / "src" / "gitopsctr").with_suffix("")
        package = ["gitopsctr", *relative.parts[:-1]]
        package = package[: len(package) - node.level + 1]
        base = [*package, *(node.module.split(".") if node.module else ())]
    targets = {".".join(base)} if base else set()
    targets.update(".".join((*base, alias.name)) for alias in node.names if alias.name != "*")
    return targets


def test_apply_coordinator_helper_chain_has_no_controller_path_or_git_dependency() -> None:
    application_root = Path(__file__).parents[1] / "src" / "gitopsctr" / "application"
    pending = [application_root / "apply_orchestration.py"]
    visited: set[Path] = set()
    violations: list[str] = []
    forbidden = ("pathlib", "dulwich", "git", "gitopsctr.adapters", "gitopsctr.controller", "gitopsctr.git_local")

    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _application_import_targets(path, node):
                if any(target == item or target.startswith(f"{item}.") for item in forbidden):
                    violations.append(f"{path.name} imports {target}")
                prefix = "gitopsctr.application."
                if target.startswith(prefix):
                    helper = application_root / f"{target.removeprefix(prefix).replace('.', '/')}.py"
                    if helper.is_file():
                        pending.append(helper)

    assert application_root / "apply_orchestration.py" in visited
    assert application_root / "apply_projection.py" in visited
    assert not violations, "apply coordinator helper chain reaches backend details: " + "; ".join(violations)
