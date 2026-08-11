"""Acceptance coverage for remote StackTemplate Git sources."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.errors import OperationError
from tests.test_stack_projection import _project


def _git(root: Path, *args: str, check: bool = True) -> str:
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
        check=check,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class GitDaemon:
    def __init__(self, base: Path, repository: Path) -> None:
        self.base = base
        self.repository = repository
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"git://127.0.0.1:{self.port}/{self.repository.name}"

    def start(self) -> None:
        self.process = subprocess.Popen(
            (
                "git",
                "daemon",
                "--reuseaddr",
                "--export-all",
                f"--base-path={self.base}",
                f"--port={self.port}",
            ),
            cwd=self.base,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if _git(self.base, "ls-remote", self.url, check=False):
                return
            time.sleep(0.01)
        raise AssertionError(f"git daemon did not start: {self.url}")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None


@contextmanager
def _git_daemon(base: Path, repository: Path) -> Iterator[GitDaemon]:
    daemon = GitDaemon(base, repository)
    daemon.start()
    try:
        yield daemon
    finally:
        daemon.stop()


def _remote_repository(tmp_path: Path, *, include_template: bool = True) -> tuple[Path, Path, str]:
    remote = tmp_path / "stack-templates.git"
    working = tmp_path / "stack-templates-working"
    working.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(working, "init", "-b", "main")
    _git(working, "remote", "add", "origin", str(remote))
    (working / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "remote"},
                "spec": {},
            }
        )
    )
    templates = working / "deployment/stack-templates"
    templates.mkdir(parents=True)
    if include_template:
        (templates / "application.json").write_text(json.dumps(_template("A")))
    revision_a = _commit(working, "template A")
    _git(working, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, working, revision_a


def _template(marker: str) -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "application"},
        "spec": {
            "unitTemplates": {
                "deploy": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {
                        "source": {"path": "."},
                        "terraform": {"variables": {"marker": marker}},
                    },
                }
            }
        },
    }


def _target_repository(tmp_path: Path, source: dict[str, object]) -> tuple[Path, str]:
    target = tmp_path / "target"
    environment = _project(target)
    (environment / "stacks").mkdir()
    (environment / "stacks/application.json").write_text(json.dumps(source))
    _git(target, "init", "-b", "main")
    origin = tmp_path / "target-origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(target, "remote", "add", "origin", str(origin))
    revision = _commit(target, "Stack source")
    return target, revision


def _stack_source(remote: str, *, ref: str | None = "main", commit: str | None = None) -> dict[str, object]:
    source: dict[str, object] = {"remote": remote}
    if ref is not None and commit is None:
        source["ref"] = ref
    if commit is not None:
        source["commit"] = commit
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "application"},
        "spec": {"template": {"name": "application", "source": {"fromGit": source}}},
    }


def _stack(root: Path):
    path = next((root / "stacks").glob("application.*"))
    return cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name="application"
    )


def _advance(root: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, bool]:
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", root)
    desired, changed = cli.advance_desired("dev", revision, verbose=False, summarize=False)
    assert desired is not None
    return desired, changed


def test_remote_ref_pins_commit_and_reconcile_ignores_moved_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, working, revision_a = _remote_repository(tmp_path)
    with _git_daemon(tmp_path, remote) as daemon:
        target, target_revision = _target_repository(tmp_path, _stack_source(daemon.url))
        desired_a, changed_a = _advance(target, target_revision, monkeypatch)
        assert changed_a
        current = tmp_path / "current"
        cli.materialize_revision(desired_a, current)
        resolved_source = _stack(current).spec.resolvedSource
        assert resolved_source is not None
        assert resolved_source.fromGit.commit == revision_a

        (working / "deployment/stack-templates/application.json").write_text(json.dumps(_template("B")))
        revision_b = _commit(working, "template B")
        _git(working, "push", "origin", "main")
        daemon.stop()
        specifications, _ = cli.load_convergence_specifications(
            target, "dev", current, target_revision, tmp_path / "reconcile"
        )
        assert specifications["application--deploy"].spec.terraform.variables["marker"] == "A"
        daemon.start()
        desired_b, changed_b = _advance(target, target_revision, monkeypatch)
        assert changed_b

    assert desired_b != desired_a
    materialized_b = tmp_path / "current-b"
    cli.materialize_revision(desired_b, materialized_b)
    resolved_source = _stack(materialized_b).spec.resolvedSource
    assert resolved_source is not None
    assert resolved_source.fromGit.commit == revision_b


def test_fixed_remote_commit_stays_pinned_after_main_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, working, revision_a = _remote_repository(tmp_path)
    with _git_daemon(tmp_path, remote) as daemon:
        target, target_revision = _target_repository(tmp_path, _stack_source(daemon.url, commit=revision_a))
        desired_a, changed_a = _advance(target, target_revision, monkeypatch)
        assert changed_a
        (working / "deployment/stack-templates/application.json").write_text(json.dumps(_template("B")))
        revision_b = _commit(working, "template B")
        _git(working, "push", "origin", "main")
        desired_b, changed_b = _advance(target, target_revision, monkeypatch)

    assert revision_b != revision_a
    assert changed_b or desired_b == desired_a
    materialized_b = tmp_path / "current-b"
    cli.materialize_revision(desired_b, materialized_b)
    resolved_source = _stack(materialized_b).spec.resolvedSource
    assert resolved_source is not None
    assert resolved_source.fromGit.commit == revision_a
    specifications, _ = cli.load_convergence_specifications(
        target, "dev", materialized_b, target_revision, tmp_path / "reconcile-fixed"
    )
    assert specifications["application--deploy"].spec.terraform.variables["marker"] == "A"


@pytest.mark.parametrize("failure", ["fetch", "ref", "path", "invalid-source"])
def test_remote_source_failures_do_not_publish_desired_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    if failure == "fetch":
        source = _stack_source("git://127.0.0.1:1/missing.git")
        target, target_revision = _target_repository(tmp_path, source)
        monkeypatch.setattr(cli, "REPOSITORY_ROOT", target)
        with pytest.raises(OperationError):
            cli.advance_desired("dev", target_revision, verbose=False, summarize=False)
    elif failure == "invalid-source":
        source = _stack_source("https://user:password@example.invalid/repository.git")
        target, target_revision = _target_repository(tmp_path, source)
        monkeypatch.setattr(cli, "REPOSITORY_ROOT", target)
        with pytest.raises(OperationError):
            cli.advance_desired("dev", target_revision, verbose=False, summarize=False)
    else:
        remote, _, _ = _remote_repository(tmp_path, include_template=failure != "path")
        with _git_daemon(tmp_path, remote) as daemon:
            source = _stack_source(daemon.url, ref="missing" if failure == "ref" else "main")
            target, target_revision = _target_repository(tmp_path, source)
            monkeypatch.setattr(cli, "REPOSITORY_ROOT", target)
            with pytest.raises(OperationError):
                cli.advance_desired("dev", target_revision, verbose=False, summarize=False)
    assert cli.fetch_ref("gitopsctr/desired/dev") is None
