"""Git commit-graph safety checks for change-gated candidates."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore


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
