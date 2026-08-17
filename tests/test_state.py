"""Git commit-graph safety checks for change-gated candidates."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr.errors import OperationError
from gitopsctr.state import (
    AcceptedDesiredTarget,
    ControllerPin,
    GitRefSnapshot,
    GitSourceRevision,
    GitStateStore,
    PublishedTree,
    RemoteRefQuery,
    RemoteRefSnapshot,
    RemoteRefUpdate,
    canonical_repository_identity,
    remote_ref_snapshot_scope,
)


@dataclass(frozen=True)
class BareRepository:
    working: Path
    remote: Path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        (
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            *args,
        ),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _commit(root: Path, filename: str, content: str, message: str) -> str:
    path = root / filename
    path.write_text(content)
    _git(root, "add", filename)
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("ref", ["deploy/dev", "refs/heads/deploy/dev"])
def test_accepted_desired_target_canonicalizes_public_ref(ref: str):
    target = AcceptedDesiredTarget(ref, "a" * 40)

    assert target.ref == "deploy/dev"
    assert target.revision == "a" * 40
    assert target == AcceptedDesiredTarget("deploy/dev", "a" * 40)


@pytest.mark.parametrize("ref", ["", "refs/", "refs/tags/deploy/dev", "refs/heads/"])
def test_accepted_desired_target_rejects_invalid_ref(ref: str):
    with pytest.raises(OperationError, match="publication ref"):
        AcceptedDesiredTarget(ref, "a" * 40)


@pytest.mark.parametrize("revision", ["a" * 39, "a" * 41, "A" * 40, "g" * 40, ""])
def test_accepted_desired_target_rejects_invalid_revision(revision: str):
    with pytest.raises(OperationError, match="revision"):
        AcceptedDesiredTarget("deploy/dev", revision)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    return tmp_path


@pytest.fixture
def bare_repository(tmp_path: Path) -> BareRepository:
    remote = tmp_path / "remote.git"
    working = tmp_path / "working"
    working.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(working, "init", "-b", "main")
    _git(working, "remote", "add", "origin", str(remote))
    _git(working, "config", "user.name", "test")
    _git(working, "config", "user.email", "test@example.invalid")
    _commit(working, "state", "base\n", "base")
    _git(working, "push", "-u", "origin", "main")
    return BareRepository(working, remote)


@pytest.fixture
def source_repository(tmp_path: Path) -> BareRepository:
    remote = tmp_path / "source.git"
    working = tmp_path / "source-working"
    working.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(working, "init", "-b", "main")
    _git(working, "remote", "add", "origin", str(remote))
    _git(working, "config", "user.name", "test")
    _git(working, "config", "user.email", "test@example.invalid")
    _commit(working, "source", "first\n", "first")
    _git(working, "push", "-u", "origin", "main")
    return BareRepository(working, remote)


def test_remote_ref_snapshot_enforces_declared_coverage(bare_repository: BareRepository):
    store = GitStateStore(bare_repository.working)
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    snapshot = store.remote_ref_snapshot(
        RemoteRefQuery(
            exact_refs=frozenset(("main",)),
            prefixes=frozenset(("refs/heads/gitopsctr/pins/",)),
        )
    )

    assert snapshot.revision("refs/heads/main") == revision
    assert snapshot.revision("refs/heads/gitopsctr/pins/missing") is None
    with pytest.raises(OperationError, match="outside snapshot coverage"):
        snapshot.revision("other")
    with pytest.raises(OperationError, match="undeclared"):
        RemoteRefSnapshot(RemoteRefQuery(exact_refs=frozenset(("main",))), {"other": revision})


def test_local_repository_handle_is_reused_and_refreshed_after_fetch(bare_repository: BareRepository):
    store = GitStateStore(bare_repository.working)
    revision = _git(bare_repository.working, "rev-parse", "HEAD")

    assert store.local_commit(revision) == revision
    opened = store._local._repository
    assert opened is not None
    assert store.local_commit(revision) == revision
    assert store._local._repository is opened

    assert store.fetch("main").revision == revision
    assert store._local._repository is None
    assert store.local_commit(revision) == revision
    assert store._local._repository is not opened
    store.close()
    assert store._local._repository is None


def test_remote_ref_snapshot_scope_memoizes_only_covered_queries(
    bare_repository: BareRepository, monkeypatch: pytest.MonkeyPatch
):
    store = GitStateStore(bare_repository.working)
    calls = 0
    original_git = GitStateStore._run_git

    def recording_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        if self is store and args[:2] == ("ls-remote", "--refs"):
            calls += 1
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", recording_git)
    with remote_ref_snapshot_scope():
        broad = store.remote_ref_snapshot(RemoteRefQuery(prefixes=frozenset(("refs/heads/gitopsctr/",))))
        assert (
            store.remote_ref_snapshot(RemoteRefQuery(exact_refs=frozenset(("refs/heads/gitopsctr/pins/missing",))))
            is broad
        )
        store.remote_ref_snapshot(RemoteRefQuery(exact_refs=frozenset(("main",))))
    assert calls == 2


def test_remote_ref_updates_require_unique_typed_compare_and_swap_entries(bare_repository: BareRepository):
    store = GitStateStore(bare_repository.working)
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    update = RemoteRefUpdate("typed/new", revision, None)
    assert store.update_remote_refs((update,)).returncode == 0
    assert _remote_revision(bare_repository, "refs/heads/typed/new") == revision
    with pytest.raises(OperationError, match="duplicate refs"):
        store.update_remote_refs((update, update))


def _pin_ref(name: str) -> str:
    return f"refs/heads/gitopsctr/pins/{name}"


def _remote_revision(repository: BareRepository, ref: str) -> str | None:
    result = subprocess.run(
        ("git", "show-ref", "--verify", "--hash", ref),
        cwd=repository.remote,
        check=False,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() or None


def _publish(
    store: GitStateStore,
    ref: str,
    directory: Path,
    parent: str | None,
    message: str,
    source_pins: dict[str, str] | None = None,
) -> PublishedTree:
    """Publish against the caller's current remote snapshot in test setup."""

    expected = store._remote_ref_revision(f"refs/heads/{store._publication_branch(ref)}")
    return store.publish(
        ref,
        directory,
        parent,
        message,
        source_pins,
        expected_publication_head=expected,
    )


def test_gated_candidate_must_be_one_commit_on_target_head(repository: Path):
    target = _commit(repository, "state", "base\n", "base")
    candidate = _commit(repository, "state", "candidate\n", "candidate")

    result = GitStateStore(repository).verify_gated_candidate(candidate, target)

    assert result.revision == candidate
    assert result.target_revision == target
    assert result.parent == target


@pytest.mark.parametrize("candidate_kind", ["stale", "multi", "merge"])
def test_gated_candidate_rejects_stale_multi_commit_and_merge_shapes(repository: Path, candidate_kind: str):
    target = _commit(repository, "state", "base\n", "base")
    first = _commit(repository, "state", "first\n", "first")
    if candidate_kind == "stale":
        _git(repository, "checkout", "-b", "stale", target)
        stale = _commit(repository, "state", "stale\n", "stale")
        _git(repository, "checkout", "main")
        candidate_target = _commit(repository, "state", "new-target\n", "new target")
        candidate = stale
    elif candidate_kind == "multi":
        candidate_target = target
        candidate = _commit(repository, "state", "second\n", "second")
    else:
        _git(repository, "checkout", "-b", "side", target)
        side = _commit(repository, "side", "side\n", "side")
        _git(repository, "checkout", "main")
        main = _commit(repository, "state", "main\n", "main")
        _git(repository, "merge", "--no-ff", side, "-m", "merge")
        candidate = _git(repository, "rev-parse", "HEAD")
        candidate_target = main
    with pytest.raises(OperationError, match="gated candidate"):
        GitStateStore(repository).verify_gated_candidate(candidate, candidate_target)
    assert first


@pytest.mark.parametrize("candidate,target", [(None, "a" * 40), ("a" * 40, None)])
def test_gated_candidate_requires_both_heads(repository: Path, candidate: str | None, target: str | None):
    with pytest.raises(OperationError, match="missing"):
        GitStateStore(repository).verify_gated_candidate(candidate, target)


def test_controller_pin_create_and_repeat_are_idempotent(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    store = GitStateStore(bare_repository.working)

    pin = store.create_controller_pin("preview/example", revision)
    repeated = store.create_controller_pin("preview/example", revision)

    assert pin == repeated
    assert pin.ref == _pin_ref("preview/example")
    assert _remote_revision(bare_repository, pin.ref) == revision


def test_controller_pin_batch_is_atomic_and_sorted(bare_repository: BareRepository):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    second = _commit(bare_repository.working, "state", "second\n", "second")
    _git(bare_repository.working, "push", "origin", "main")
    store = GitStateStore(bare_repository.working)

    pins = store.create_controller_pins({"stacks/dev/z": first, "stacks/dev/a": second})

    assert pins == (
        ControllerPin("stacks/dev/a", _pin_ref("stacks/dev/a"), second),
        ControllerPin("stacks/dev/z", _pin_ref("stacks/dev/z"), first),
    )
    assert _remote_revision(bare_repository, _pin_ref("stacks/dev/a")) == second
    assert _remote_revision(bare_repository, _pin_ref("stacks/dev/z")) == first


def test_controller_pin_listing_is_sorted_and_read_only(bare_repository: BareRepository):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    second = _commit(bare_repository.working, "state", "second\n", "second")
    _git(bare_repository.working, "push", "origin", "main")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin("stacks/prod/z", first)
    store.create_controller_pin("stacks/prod/a", second)

    assert store.list_controller_pins() == (
        store.create_controller_pin("stacks/prod/a", second),
        store.create_controller_pin("stacks/prod/z", first),
    )


def test_controller_pin_listing_filters_broader_cached_snapshot(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    store = GitStateStore(bare_repository.working)
    pin = store.create_controller_pin("stacks/dev/example", revision)

    with remote_ref_snapshot_scope():
        snapshot = store.remote_ref_snapshot(RemoteRefQuery(prefixes=frozenset(("refs/heads/",))))
        assert "refs/heads/main" in snapshot.revisions
        assert store.list_controller_pins() == (pin,)


def test_remote_ref_listing_is_sorted_and_read_only(bare_repository: BareRepository):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.working, "branch", "candidate/z")
    _git(bare_repository.working, "branch", "candidate/a")
    _git(bare_repository.working, "push", "origin", "refs/heads/candidate/z:refs/heads/candidate/z")
    _git(bare_repository.working, "push", "origin", "refs/heads/candidate/a:refs/heads/candidate/a")

    assert GitStateStore(bare_repository.working).list_remote_refs() == (
        GitRefSnapshot("candidate/a", first),
        GitRefSnapshot("candidate/z", first),
        GitRefSnapshot("main", first),
    )


def test_controller_pin_mismatched_create_fails_closed(bare_repository: BareRepository):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    second = _commit(bare_repository.working, "state", "second\n", "second")
    _git(bare_repository.working, "push", "origin", "main")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin("preview/example", first)

    with pytest.raises(OperationError, match="already points"):
        store.create_controller_pin("preview/example", second)

    assert _remote_revision(bare_repository, _pin_ref("preview/example")) == first


def test_controller_pin_matching_release_removes_pin(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin("preview/example", revision)

    assert store.release_controller_pin("preview/example", revision)
    assert _remote_revision(bare_repository, _pin_ref("preview/example")) is None
    assert not store.release_controller_pin("preview/example", revision)


def test_controller_pin_stale_release_fails_closed(bare_repository: BareRepository):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    second = _commit(bare_repository.working, "state", "second\n", "second")
    _git(bare_repository.working, "push", "origin", "main")
    store = GitStateStore(bare_repository.working)
    pin = store.create_controller_pin("preview/example", first)
    _git(bare_repository.remote, "update-ref", pin.ref, second)

    with pytest.raises(OperationError, match="fenced"):
        store.release_controller_pin("preview/example", first)

    assert _remote_revision(bare_repository, pin.ref) == second


def test_controller_pin_missing_release_is_idempotent(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")

    assert not GitStateStore(bare_repository.working).release_controller_pin("preview/example", revision)


def test_live_prepublication_claims_are_not_reaped(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    canonical = "stack-templates/dev/preview/template-uid/" + revision
    store = GitStateStore(bare_repository.working, clock=lambda: 100.0)

    claims = store.create_controller_pin_claims({canonical: revision}, "runner-a")
    claims += store.create_controller_pin_claims({canonical: revision}, "runner-b")

    assert all(re.fullmatch(r"claims/[0-9]{20}-runner-[ab]-[0-9a-f]{12}/.*", claim.name) for claim in claims)
    assert store.reap_expired_controller_pin_claims(now=119.0, expiry_seconds=10, grace_seconds=10) == ()
    assert {pin.name for pin in store.list_controller_pins()} == {pin.name for pin in claims}


def test_expired_abandoned_claim_is_reaped_with_a_compare_delete(bare_repository: BareRepository):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    canonical = "stack-templates/dev/preview/template-uid/" + revision
    store = GitStateStore(bare_repository.working, clock=lambda: 100.0)
    claim = store.create_controller_pin_claims({canonical: revision}, "abandoned")[0]

    reaped = store.reap_expired_controller_pin_claims(now=121.0, expiry_seconds=10, grace_seconds=10)

    assert reaped == (claim,)
    assert _remote_revision(bare_repository, claim.ref) is None


def test_publication_owner_binds_custom_ref_and_commit_atomically(bare_repository: BareRepository, tmp_path: Path):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)

    published = _publish(store, "custom/candidate", directory, revision, "candidate", {source_pin: revision})
    owners = store.list_controller_publication_owners()

    assert published.revision
    assert len(owners) == 1
    assert owners[0].publication_ref == "custom/candidate"
    assert owners[0].publication_revision == published.revision
    assert owners[0].source_pin_name == source_pin
    assert owners[0].revision == revision
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision


def test_owner_survives_a_client_error_after_remote_publication(bare_repository: BareRepository, tmp_path: Path):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)

    _publish(store, "custom/desired", directory, revision, "desired", {source_pin: revision})
    try:
        raise RuntimeError("client/PR request failed after push")
    except RuntimeError:
        pass

    owners = store.list_controller_publication_owners()
    assert len(owners) == 1
    assert owners[0].publication_ref == "custom/desired"


def test_first_owned_publication_uses_existing_head_lease(bare_repository: BareRepository, tmp_path: Path, monkeypatch):
    first = _git(bare_repository.working, "rev-parse", "HEAD")
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)
    initial = _publish(store, "custom/candidate", directory, first, "first")
    source_pin = "stack-templates/dev/preview/template-uid/" + first
    pushes: list[tuple[str, ...]] = []
    original_git = GitStateStore._run_git

    def recording_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self is store and args and args[0] == "push":
            pushes.append(args)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", recording_git)
    published = _publish(store, "custom/candidate", directory, first, "second", {source_pin: first})

    assert len(pushes) == 1
    publication_lease = f"--force-with-lease=refs/heads/custom/candidate:{initial.revision}"
    assert pushes[0].count(publication_lease) == 1
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision
    assert store.list_controller_publication_owners()[0].publication_revision == published.revision


@pytest.mark.parametrize("race", ["delete", "move"])
def test_first_owned_publication_fences_existing_head_race(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
):
    base_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    observed_revision = _commit(bare_repository.working, "target", "target\n", "target")
    _git(bare_repository.working, "push", "origin", "main")
    publication_ref = "refs/heads/custom/candidate"
    lock_ref = GitStateStore(bare_repository.working)._publication_lock_ref("custom/candidate")
    _git(bare_repository.remote, "update-ref", publication_ref, observed_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + observed_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    raced = False
    original_git = GitStateStore._run_git

    def racing_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        if self is store and args and args[0] == "push" and not raced:
            raced = True
            if race == "delete":
                _git(bare_repository.remote, "update-ref", "-d", publication_ref)
            else:
                _git(bare_repository.remote, "update-ref", publication_ref, base_revision)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", racing_git)
    with pytest.raises(subprocess.CalledProcessError):
        _publish(store, "custom/candidate", directory, observed_revision, "candidate", {source_pin: observed_revision})

    assert raced
    assert _remote_revision(bare_repository, publication_ref) == (None if race == "delete" else base_revision)
    assert _remote_revision(bare_repository, lock_ref) is None
    assert not store.list_controller_publication_owners()


def test_first_owned_publication_fences_expected_absent_head_creation(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    publication_ref = "refs/heads/custom/candidate"
    lock_ref = GitStateStore(bare_repository.working)._publication_lock_ref("custom/candidate")
    source_pin = "stack-templates/dev/preview/template-uid/" + base_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    raced = False
    original_git = GitStateStore._run_git

    def racing_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        if self is store and args and args[0] == "push" and not raced:
            raced = True
            _git(bare_repository.remote, "update-ref", publication_ref, base_revision)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", racing_git)
    with pytest.raises(subprocess.CalledProcessError):
        _publish(store, "custom/candidate", directory, base_revision, "candidate", {source_pin: base_revision})

    assert raced
    assert _remote_revision(bare_repository, publication_ref) == base_revision
    assert _remote_revision(bare_repository, lock_ref) is None
    assert not store.list_controller_publication_owners()


def test_publication_cas_rejects_target_sibling_before_push(bare_repository: BareRepository, tmp_path: Path):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    sibling_revision = _commit(bare_repository.working, "sibling", "x\n", "sibling")
    _git(bare_repository.working, "push", "origin", "main")
    publication_ref = "refs/heads/deploy/dev"
    _git(bare_repository.remote, "update-ref", publication_ref, target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("target\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)

    # The caller authorized T. X arrives after that snapshot and must remain
    # untouched by the publication attempt.
    _git(bare_repository.remote, "update-ref", publication_ref, sibling_revision)
    with pytest.raises(subprocess.CalledProcessError):
        store.publish(
            "deploy/dev",
            directory,
            target_revision,
            "target",
            {source_pin: target_revision},
            expected_publication_head=target_revision,
        )

    assert _remote_revision(bare_repository, publication_ref) == sibling_revision
    assert not store.list_controller_publication_owners()
    assert _remote_revision(bare_repository, store._publication_lock_ref("deploy/dev")) is None


def test_publication_cas_rejects_candidate_appearing_after_expected_absence(
    bare_repository: BareRepository, tmp_path: Path
):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    candidate_revision = _commit(bare_repository.working, "candidate", "x\n", "candidate")
    _git(bare_repository.working, "push", "origin", "main")
    publication_ref = "refs/heads/custom/candidate"
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)

    # The validated candidate snapshot said absent. A sibling X appears before
    # the atomic push and must not be adopted or overwritten.
    _git(bare_repository.remote, "update-ref", publication_ref, candidate_revision)
    with pytest.raises(subprocess.CalledProcessError):
        store.publish(
            "custom/candidate",
            directory,
            target_revision,
            "candidate",
            {source_pin: target_revision},
            expected_publication_head=None,
        )

    assert _remote_revision(bare_repository, publication_ref) == candidate_revision
    assert not store.list_controller_publication_owners()
    assert _remote_revision(bare_repository, store._publication_lock_ref("custom/candidate")) is None


@pytest.mark.parametrize(
    "ref",
    [
        "gitopsctr/pins/custom",
        "refs/heads/gitopsctr/pins/custom",
        "gitopsctr/owners/custom",
        "refs/heads/gitopsctr/owners/custom",
        "gitopsctr/locks/custom",
        "refs/heads/gitopsctr/locks/custom",
    ],
)
def test_publication_rejects_controller_reserved_namespaces(bare_repository: BareRepository, tmp_path: Path, ref: str):
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")

    with pytest.raises(OperationError, match="reserved"):
        GitStateStore(bare_repository.working).publish(
            ref,
            directory,
            None,
            "reserved",
            expected_publication_head=None,
        )


def test_publication_allows_user_gitopsctr_namespace_outside_reserved_prefixes(
    bare_repository: BareRepository, tmp_path: Path
):
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")

    published = GitStateStore(bare_repository.working).publish(
        "gitopsctr/user-state",
        directory,
        None,
        "user state",
        expected_publication_head=None,
    )

    assert _remote_revision(bare_repository, "refs/heads/gitopsctr/user-state") == published.revision


def test_partial_owner_inspection_fails_closed(bare_repository: BareRepository, tmp_path: Path):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    first_pin = "stack-templates/dev/preview/first/" + revision
    second_pin = "stack-templates/dev/preview/second/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)
    published = _publish(
        store, "custom/candidate", directory, revision, "candidate", {first_pin: revision, second_pin: revision}
    )
    second_owner = next(
        owner for owner in store.list_controller_publication_owners() if owner.source_pin_name == second_pin
    )
    _git(bare_repository.remote, "update-ref", "-d", second_owner.ref)

    with pytest.raises(OperationError, match="owner"):
        store.verify_published_tree_with_owners(
            "custom/candidate", directory, revision, {first_pin: revision, second_pin: revision}
        )
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision


@pytest.mark.parametrize("ownership", ["canonical", "publication-owner", "claim-only"])
def test_fresh_runner_hydrates_each_source_ownership_kind(
    bare_repository: BareRepository, tmp_path: Path, ownership: str
):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    store = GitStateStore(bare_repository.working, clock=lambda: 100.0)
    if ownership == "canonical":
        store.create_controller_pin(source_pin, revision)
    elif ownership == "publication-owner":
        directory = tmp_path / "desired"
        directory.mkdir()
        (directory / "state").write_text("desired\n")
        _publish(store, "custom/candidate", directory, revision, "candidate", {source_pin: revision})
    else:
        store.create_controller_pin_claims({source_pin: revision}, "promotion")

    fresh = tmp_path / f"fresh-{ownership}"
    _git(tmp_path, "clone", str(bare_repository.remote), str(fresh))
    fresh_store = GitStateStore(fresh, clock=lambda: 100.0)

    hydrated = fresh_store.hydrate_source_revision(source_pin, revision)

    assert fresh_store._local_ref_revision(hydrated) == revision


def test_fresh_shallow_runner_creates_claim_from_publication_owner_only(
    bare_repository: BareRepository, tmp_path: Path
):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)
    _publish(store, "custom/owner-only", directory, None, "owner-only", {source_pin: revision})

    fresh = tmp_path / "fresh-owner-only"
    _git(
        tmp_path,
        "clone",
        "--no-local",
        "--depth=1",
        "--single-branch",
        "--branch",
        "custom/owner-only",
        str(bare_repository.remote),
        str(fresh),
    )
    fresh_store = GitStateStore(fresh)

    claim = fresh_store.create_controller_pin_claims({source_pin: revision}, "attempt")[0]

    assert claim.revision == revision
    assert _remote_revision(bare_repository, claim.ref) == revision


def test_orphan_publication_owner_cleanup_removes_owner_canonical_and_lock(
    bare_repository: BareRepository, tmp_path: Path
):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, revision)
    published = _publish(store, "custom/orphan", directory, None, "orphan", {source_pin: revision})
    owner = store.list_controller_publication_owners()[0]
    lock_ref = store._publication_lock_ref("custom/orphan")
    _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/orphan")

    assert not store.publication_owner_is_live(owner)
    assert store.release_publication_owner(owner)
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None
    assert _remote_revision(bare_repository, lock_ref) is None
    assert published.revision


def test_stale_accepted_publication_owner_cleanup_preserves_advanced_ref(
    bare_repository: BareRepository, tmp_path: Path
):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("accepted\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, revision)
    first = _publish(store, "deploy/dev", directory, None, "accepted", {source_pin: revision})
    owner = store.list_controller_publication_owners()[0]

    (directory / "state").write_text("advanced\n")
    advanced = _publish(store, "custom/advanced", directory, None, "advanced")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", advanced.revision)
    accepted_target = AcceptedDesiredTarget("deploy/dev", advanced.revision)

    assert first.revision != advanced.revision
    assert store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, "refs/heads/deploy/dev") == advanced.revision
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None


def test_same_ref_accepted_target_is_deduplicated_in_cleanup_push(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("accepted\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, revision)
    _publish(store, "deploy/dev", directory, None, "accepted", {source_pin: revision})
    owner = store.list_controller_publication_owners()[0]

    (directory / "state").write_text("advanced\n")
    advanced = _publish(store, "custom/advanced", directory, None, "advanced")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", advanced.revision)
    pushes: list[tuple[str, ...]] = []
    original_git = GitStateStore._run_git

    def recording_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self is store and args and args[0] == "push":
            pushes.append(args)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", recording_git)
    assert store.release_publication_owner(owner, AcceptedDesiredTarget("refs/heads/deploy/dev", advanced.revision))

    assert len(pushes) == 1
    push_args = pushes[0]
    assert sum(argument.startswith("--force-with-lease=refs/heads/deploy/dev:") for argument in push_args) == 1
    assert sum(argument.endswith(":refs/heads/deploy/dev") for argument in push_args) == 1
    assert _remote_revision(bare_repository, "refs/heads/deploy/dev") == advanced.revision


def test_advanced_candidate_owner_retry_preserves_current_candidate(bare_repository: BareRepository, tmp_path: Path):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("first\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    first = _publish(store, "custom/candidate", directory, target_revision, "first", {source_pin: target_revision})
    old_owner = store.list_controller_publication_owners()[0]

    (directory / "state").write_text("advanced\n")
    advanced = _publish(
        store, "custom/candidate", directory, target_revision, "advanced", {source_pin: target_revision}
    )
    accepted_target = AcceptedDesiredTarget("deploy/dev", target_revision)

    assert first.revision != advanced.revision
    assert store.publication_owner_is_live_candidate(
        next(
            owner
            for owner in store.list_controller_publication_owners()
            if owner.publication_revision == advanced.revision
        ),
        accepted_target,
    )
    assert store.release_publication_owner(old_owner, accepted_target)
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == advanced.revision
    assert _remote_revision(bare_repository, old_owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision


def test_live_accepted_publication_owner_remains_protected(bare_repository: BareRepository, tmp_path: Path):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("accepted\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, revision)
    published = _publish(store, "deploy/dev", directory, None, "accepted", {source_pin: revision})
    owner = store.list_controller_publication_owners()[0]
    accepted_target = AcceptedDesiredTarget("deploy/dev", published.revision)

    assert not store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, owner.ref) == revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == revision


def test_current_candidate_parent_fence_keeps_publication_owner(bare_repository: BareRepository, tmp_path: Path):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    published = _publish(
        store, "custom/candidate", directory, target_revision, "candidate", {source_pin: target_revision}
    )
    owner = store.list_controller_publication_owners()[0]
    accepted_target = AcceptedDesiredTarget("deploy/dev", target_revision)

    assert published.revision == owner.publication_revision
    assert store.publication_owner_is_live_candidate(owner, accepted_target)
    assert not store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, owner.ref) == target_revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision


def test_merged_candidate_branch_releases_owner_and_canonical_source(bare_repository: BareRepository, tmp_path: Path):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    published = _publish(
        store, "custom/candidate", directory, target_revision, "candidate", {source_pin: target_revision}
    )
    owner = store.list_controller_publication_owners()[0]
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", published.revision)
    accepted_target = AcceptedDesiredTarget("deploy/dev", published.revision)

    assert not store.publication_owner_is_live_candidate(owner, accepted_target)
    assert store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None
    assert _remote_revision(bare_repository, store._publication_lock_ref("custom/candidate")) is not None


def test_candidate_recreation_race_fails_closed_during_owner_release(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    published = _publish(
        store, "custom/candidate", directory, target_revision, "candidate", {source_pin: target_revision}
    )
    owner = store.list_controller_publication_owners()[0]
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", published.revision)
    accepted_target = AcceptedDesiredTarget("deploy/dev", published.revision)
    raced = False
    original_git = GitStateStore._run_git

    def racing_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        if self is store and args and args[0] == "push" and not raced:
            raced = True
            _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/candidate")
            _publish(store, "custom/candidate", directory, target_revision, "recreated", {source_pin: target_revision})
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", racing_git)
    with pytest.raises(OperationError):
        store.release_publication_owner(owner, accepted_target)

    assert raced
    assert _remote_revision(bare_repository, owner.ref) == target_revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") != published.revision

    monkeypatch.undo()
    assert store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") is not None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision


def test_absent_candidate_direct_recreation_race_fails_closed_without_lock_update(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    published = _publish(
        store, "custom/candidate", directory, target_revision, "candidate", {source_pin: target_revision}
    )
    owner = store.list_controller_publication_owners()[0]
    _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/candidate")
    accepted_revision = _commit(bare_repository.working, "tombstone", "deleted\n", "accepted tombstone")
    _git(bare_repository.working, "push", "origin", f"{accepted_revision}:refs/heads/test/tombstone")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", accepted_revision)
    accepted_target = AcceptedDesiredTarget("deploy/dev", accepted_revision)
    lock_ref = store._publication_lock_ref(owner.publication_ref)
    lock_revision = _remote_revision(bare_repository, lock_ref)
    raced = False
    original_git = GitStateStore._run_git

    def racing_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        if self is store and args and args[0] == "push" and not raced:
            raced = True
            # This direct recreation deliberately leaves the publication lock unchanged.
            _git(bare_repository.remote, "update-ref", "refs/heads/custom/candidate", published.revision)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", racing_git)
    with pytest.raises(OperationError):
        store.release_publication_owner(owner, accepted_target)

    assert raced
    assert _remote_revision(bare_repository, owner.ref) == target_revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision
    assert _remote_revision(bare_repository, lock_ref) == lock_revision

    monkeypatch.undo()
    assert store.release_publication_owner(owner, accepted_target)
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") == published.revision


def test_same_ref_target_interleave_uses_one_observation_and_fails_closed(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + source_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("accepted\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, source_revision)
    published = _publish(store, "deploy/dev", directory, None, "accepted", {source_pin: source_revision})
    owner = store.list_controller_publication_owners()[0]
    tombstone = _commit(bare_repository.working, "tombstone", "deleted\n", "accepted tombstone")
    _git(bare_repository.working, "push", "origin", f"{tombstone}:refs/heads/test/tombstone")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", tombstone)
    accepted_target = AcceptedDesiredTarget("refs/heads/deploy/dev", tombstone)
    target_reads = 0
    push_read_counts: list[int] = []
    original_remote_snapshot = GitStateStore.remote_ref_snapshot
    original_git = GitStateStore._run_git

    def racing_remote_snapshot(self: GitStateStore, query, *, fresh: bool = False):
        nonlocal target_reads
        value = original_remote_snapshot(self, query, fresh=fresh)
        if self is store and query.covers_ref(accepted_target.ref):
            target_reads += 1
            if target_reads == 1:
                # The atomic snapshot is leased even if the ref changes immediately after it.
                _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", published.revision)
        return value

    def recording_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self is store and args and args[0] == "push":
            push_read_counts.append(target_reads)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "remote_ref_snapshot", racing_remote_snapshot)
    monkeypatch.setattr(GitStateStore, "_run_git", recording_git)
    with pytest.raises(OperationError):
        store.release_publication_owner(owner, accepted_target)

    assert push_read_counts == [1]
    assert _remote_revision(bare_repository, owner.ref) == source_revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == source_revision

    monkeypatch.undo()
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", tombstone)
    pushes: list[tuple[str, ...]] = []

    def recording_fresh_push(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self is store and args and args[0] == "push":
            pushes.append(args)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", recording_fresh_push)
    assert store.release_publication_owner(owner, accepted_target)
    assert len(pushes) == 1
    assert pushes[0].count(f"--force-with-lease=refs/heads/deploy/dev:{tombstone}") == 1
    assert pushes[0].count(f"{tombstone}:refs/heads/deploy/dev") == 1
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None


def test_publication_recreation_fences_finalization_and_changes_marker(bare_repository: BareRepository, tmp_path: Path):
    revision = _git(bare_repository.working, "rev-parse", "HEAD")
    source_pin = "stack-templates/dev/preview/template-uid/" + revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("desired\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, revision)
    _publish(store, "custom/recreated", directory, None, "recreated", {source_pin: revision})
    first_owner = store.list_controller_publication_owners()[0]
    lock_ref = store._publication_lock_ref("custom/recreated")
    first_marker = _remote_revision(bare_repository, lock_ref)
    _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/recreated")

    # This is the deterministic interleaving: finalization observed the
    # publication absent, then another writer recreated it before cleanup.
    assert not store.publication_owner_is_live(first_owner)
    (directory / "state").write_text("recreated\n")
    _publish(store, "custom/recreated", directory, None, "recreated", {source_pin: revision})
    second_owner = next(owner for owner in store.list_controller_publication_owners() if owner.ref != first_owner.ref)
    second_marker = _remote_revision(bare_repository, lock_ref)
    _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/recreated")

    assert first_marker is not None and second_marker is not None and second_marker != first_marker
    assert store.release_publication_owner(first_owner)
    assert _remote_revision(bare_repository, first_owner.ref) is None
    # The recreated publication has a distinct owner, so its source remains
    # shared and the canonical pin must survive the old-owner cleanup.
    assert _remote_revision(bare_repository, second_owner.ref) == revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == revision


def test_candidate_absence_cleanup_fails_closed_on_target_movement(
    bare_repository: BareRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_revision = _git(bare_repository.working, "rev-parse", "HEAD")
    _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", target_revision)
    moved_target = _commit(bare_repository.working, "target", "moved\n", "moved target")
    _git(bare_repository.working, "push", "origin", "main")
    source_pin = "stack-templates/dev/preview/template-uid/" + target_revision
    directory = tmp_path / "desired"
    directory.mkdir()
    (directory / "state").write_text("candidate\n")
    store = GitStateStore(bare_repository.working)
    store.create_controller_pin(source_pin, target_revision)
    published = _publish(
        store, "custom/candidate", directory, target_revision, "candidate", {source_pin: target_revision}
    )
    owner = store.list_controller_publication_owners()[0]
    _git(bare_repository.remote, "update-ref", "-d", "refs/heads/custom/candidate")
    accepted_target = AcceptedDesiredTarget("deploy/dev", target_revision)
    raced = False
    original_git = GitStateStore._run_git

    def racing_git(self: GitStateStore, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        if self is store and args and args[0] == "push" and not raced:
            raced = True
            _git(bare_repository.remote, "update-ref", "refs/heads/deploy/dev", moved_target)
        return original_git(self, *args, **kwargs)

    monkeypatch.setattr(GitStateStore, "_run_git", racing_git)
    with pytest.raises(OperationError):
        store.release_publication_owner(owner, accepted_target)

    assert raced
    assert _remote_revision(bare_repository, owner.ref) == target_revision
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) == target_revision
    assert _remote_revision(bare_repository, "refs/heads/deploy/dev") == moved_target
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") is None

    monkeypatch.undo()
    assert store.release_publication_owner(owner, AcceptedDesiredTarget("deploy/dev", moved_target))
    assert _remote_revision(bare_repository, owner.ref) is None
    assert _remote_revision(bare_repository, _pin_ref(source_pin)) is None
    assert _remote_revision(bare_repository, "refs/heads/deploy/dev") == moved_target
    assert _remote_revision(bare_repository, "refs/heads/custom/candidate") is None
    assert published.revision


def test_repository_identity_strips_credentials_and_preserves_local_dot():
    assert canonical_repository_identity("https://alice:secret@example.com/org/repo.git") == (
        "https://example.com/org/repo.git"
    )
    assert canonical_repository_identity("https://different:token@example.com/org/repo.git") == (
        "https://example.com/org/repo.git"
    )
    assert canonical_repository_identity(".") == "."


def test_local_dot_source_resolution_does_not_require_origin(repository: Path):
    revision = _commit(repository, "source", "local\n", "local")

    source = GitStateStore(repository).resolve_source(".", "main")

    assert source == GitSourceRevision(".", "main", revision, local=True, _transport=".")


def test_source_resolution_supports_branches_tags_full_refs_head_and_short_commits(repository: Path):
    first = _commit(repository, "source", "first\n", "first")
    _git(repository, "tag", "v1")
    second = _commit(repository, "source", "second\n", "second")
    store = GitStateStore(repository)

    assert store.resolve_source(".", "main").revision == second
    assert store.resolve_source(".", "refs/heads/main").revision == second
    assert store.resolve_source(".", "refs/tags/v1").revision == first
    assert store.resolve_source(".", "HEAD").revision == second
    assert store.resolve_source(".", "main", first[:12]).revision == first
    assert store.resolve_source(".", "main", first).revision == first


def test_unqualified_source_ref_accepts_a_tag_but_rejects_branch_tag_ambiguity(repository: Path):
    first = _commit(repository, "source", "first\n", "first")
    _git(repository, "tag", "release")
    store = GitStateStore(repository)

    assert store.resolve_source(".", "release").revision == first

    _git(repository, "branch", "release")
    with pytest.raises(OperationError, match="ambiguous"):
        store.resolve_source(".", "release")


@pytest.mark.parametrize(
    "repository",
    ["--upload-pack=evil", "ext::sh -c evil", "https://example.invalid/repo.git?token=secret", "user@host:repo.git#x"],
)
def test_source_repository_inputs_reject_options_helpers_and_query_forms(repository: str):
    with pytest.raises(OperationError):
        canonical_repository_identity(repository)


def test_relative_source_repository_is_rooted_at_state_store_root(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    source = tmp_path / "source.git"
    working = tmp_path / "source-working"
    working.mkdir()
    _git(tmp_path, "init", "--bare", str(source))
    _git(working, "init", "-b", "main")
    _git(working, "config", "user.name", "test")
    _git(working, "config", "user.email", "test@example.invalid")
    revision = _commit(working, "source", "rooted\n", "rooted")
    _git(working, "push", str(source), "main")

    resolved = GitStateStore(state).resolve_source("../source.git", "main")

    assert resolved.repository == source.resolve().as_uri()
    assert resolved.revision == revision


def test_external_source_resolution_supports_tags_full_refs_and_head(
    source_repository: BareRepository, bare_repository: BareRepository
):
    first = _git(source_repository.working, "rev-parse", "HEAD")
    _git(source_repository.working, "tag", "v1")
    _git(source_repository.working, "push", "origin", "refs/tags/v1")
    _git(source_repository.remote, "symbolic-ref", "HEAD", "refs/heads/main")
    store = GitStateStore(bare_repository.working)

    assert store.resolve_source(str(source_repository.remote), "refs/tags/v1").revision == first
    assert store.resolve_source(str(source_repository.remote), "HEAD").revision == first


@pytest.mark.parametrize(
    ("ref", "revision", "message"),
    [
        ("missing", None, "source ref does not exist"),
        ("main", "not-a-revision", "source revision is invalid"),
    ],
)
def test_source_resolution_rejects_missing_or_invalid_revisions(
    source_repository: BareRepository,
    bare_repository: BareRepository,
    ref: str,
    revision: str | None,
    message: str,
):
    store = GitStateStore(bare_repository.working)

    with pytest.raises(OperationError, match=message):
        store.resolve_source(str(source_repository.remote), ref, revision)

    assert not _git(store.root, "for-each-ref", "--format=%(refname)", "refs/heads/gitopsctr/source-retention/")


def test_source_identity_and_errors_do_not_expose_credentials():
    identity = canonical_repository_identity("ssh://robot:super-secret@example.com/org/repo.git")

    assert identity == "ssh://example.com/org/repo.git"
    assert "robot" not in identity
    assert "super-secret" not in identity
    with pytest.raises(OperationError) as error:
        canonical_repository_identity("https://robot:super-secret@example.com/repo.git?token=super-secret")
    assert "super-secret" not in str(error.value)


def test_claim_creation_hydrates_missing_object_from_existing_canonical_pin(
    bare_repository: BareRepository, tmp_path: Path
):
    original = GitStateStore(bare_repository.working)
    revision = _commit(bare_repository.working, "state", "canonical-only\n", "canonical-only")
    canonical = original.create_controller_pin("stack-templates/dev/preview/template-uid", revision)

    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", str(bare_repository.remote), str(fresh))
    fresh_store = GitStateStore(fresh)
    _git(fresh, "repack", "-ad")
    claim = fresh_store.create_controller_pin_claims(
        {"stack-templates/dev/preview/template-uid/" + revision: revision}, "attempt"
    )

    assert claim[0].revision == revision
    assert _remote_revision(bare_repository, claim[0].ref) == revision
    assert canonical.revision == revision


def test_claim_creation_hydrates_missing_object_from_exact_claim_ref(bare_repository: BareRepository, tmp_path: Path):
    original = GitStateStore(bare_repository.working)
    revision = _commit(bare_repository.working, "state", "claim-only\n", "claim-only")
    original.create_controller_pin_claims(
        {"stack-templates/dev/preview/template-uid/" + revision: revision}, "old-attempt"
    )

    fresh = tmp_path / "fresh-claim"
    _git(tmp_path, "clone", str(bare_repository.remote), str(fresh))
    fresh_store = GitStateStore(fresh)
    _git(fresh, "repack", "-ad")
    claim = fresh_store.create_controller_pin_claims(
        {"stack-templates/dev/preview/template-uid/" + revision: revision}, "new-attempt"
    )

    assert claim[0].revision == revision
