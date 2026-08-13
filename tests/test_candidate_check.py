"""Tests for the GitHub merge-event candidate freshness check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gitopsctr.errors import OperationError
from tools.verify_github_candidate import verify_event


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=root, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "state.txt").write_text("base\n")
    _git(repository, "add", "state.txt")
    _git(repository, "commit", "-m", "base")
    target = _git(repository, "rev-parse", "HEAD")
    (repository / "state.txt").write_text("candidate\n")
    _git(repository, "commit", "-am", "candidate")
    candidate = _git(repository, "rev-parse", "HEAD")
    return repository, target, candidate


def _event(path: Path, target: str, candidate: str, event_name: str) -> None:
    payload = (
        {"pull_request": {"head": {"sha": candidate}, "base": {"sha": target}}}
        if event_name == "pull_request"
        else {"head_sha": candidate, "base_sha": target}
    )
    path.write_text(json.dumps(payload))


@pytest.mark.parametrize("event_name", ["pull_request", "merge_group"])
def test_verify_event_accepts_exact_one_commit_candidate(tmp_path: Path, event_name: str):
    repository, target, candidate = _repository(tmp_path)
    event = tmp_path / "event.json"
    _event(event, target, candidate, event_name)

    assert "verified candidate" in verify_event(event, event_name, repository)


def test_verify_event_rejects_checked_out_revision_mismatch(tmp_path: Path):
    repository, target, candidate = _repository(tmp_path)
    event = tmp_path / "event.json"
    _event(event, target, target, "pull_request")

    with pytest.raises(OperationError, match="checked-out revision"):
        verify_event(event, "pull_request", repository)


def test_verify_event_rejects_stale_candidate(tmp_path: Path):
    repository, target, candidate = _repository(tmp_path)
    (repository / "state.txt").write_text("second\n")
    _git(repository, "commit", "-am", "second")
    stale = _git(repository, "rev-parse", "HEAD")
    event = tmp_path / "event.json"
    _event(event, target, stale, "pull_request")

    with pytest.raises(OperationError, match="stale or rebased"):
        verify_event(event, "pull_request", repository)
