"""Git commit-graph safety checks for change-gated candidates."""

import subprocess
from pathlib import Path

import pytest

from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore


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
