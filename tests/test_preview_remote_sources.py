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

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests.stack_support import commit, git, project_repository


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
            if git(self.base, "ls-remote", self.url, check=False):
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
    try:
        daemon.start()
        yield daemon
    finally:
        daemon.stop()


def _remote_repository(
    tmp_path: Path, *, include_template: bool = True, parameterized: bool = False
) -> tuple[Path, Path, str]:
    remote = tmp_path / "stack-templates.git"
    working = tmp_path / "stack-templates-working"
    working.mkdir()
    git(tmp_path, "init", "--bare", str(remote))
    git(working, "init", "-b", "main")
    git(working, "remote", "add", "origin", str(remote))
    (working / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "remote"},
                "spec": {"effectLease": None},
            }
        )
    )
    templates = working / "deployment/stack-templates"
    templates.mkdir(parents=True)
    if include_template:
        (templates / "application.json").write_text(json.dumps(_template("A", parameterized=parameterized)))
    revision_a = commit(working, "template A")
    git(working, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, working, revision_a


def _template(marker: str, *, parameterized: bool = False) -> dict[str, object]:
    template: dict[str, object] = {
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
    if parameterized:
        template["spec"]["parameters"] = [{"name": "target", "type": "string"}]
        template["spec"]["unitTemplates"]["deploy"]["spec"]["terraform"]["variables"]["target"] = {
            "fromParameter": {"name": "target"}
        }
    return template


def _target_repository(tmp_path: Path, source: dict[str, object]) -> tuple[Path, str]:
    target = tmp_path / "target"
    environment = project_repository(target)
    (environment / "stacks").mkdir()
    (environment / "stacks/application.json").write_text(json.dumps(source))
    git(target, "init", "-b", "main")
    origin = tmp_path / "target-origin.git"
    git(tmp_path, "init", "--bare", str(origin))
    git(target, "remote", "add", "origin", str(origin))
    revision = commit(target, "Stack source")
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
    return controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name="application"
    )


def _advance(root: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, bool]:
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", root)
    desired, changed = controller.advance_desired("dev", revision, verbose=False, summarize=False)
    assert desired is not None
    return desired, changed


def test_remote_ref_pins_commit_and_reconcile_ignores_moved_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote, working, revision_a = _remote_repository(tmp_path)
    with _git_daemon(tmp_path, remote) as daemon:
        target, target_revision = _target_repository(tmp_path, _stack_source(daemon.url))
        desired_a, changed_a = _advance(target, target_revision, monkeypatch)
        assert changed_a
        current = tmp_path / "current"
        controller.materialize_revision(desired_a, current)
        resolved_source = _stack(current).spec.resolvedSource
        assert resolved_source is not None
        assert resolved_source.fromGit.commit == revision_a

        (working / "deployment/stack-templates/application.json").write_text(json.dumps(_template("B")))
        revision_b = commit(working, "template B")
        git(working, "push", "origin", "main")
        daemon.stop()
        specifications, _ = controller.load_convergence_specifications(
            target, "dev", current, target_revision, tmp_path / "reconcile"
        )
        assert specifications["application--deploy"].spec.terraform.variables["marker"] == "A"
        daemon.start()
        desired_b, changed_b = _advance(target, target_revision, monkeypatch)
        assert changed_b

    assert desired_b != desired_a
    materialized_b = tmp_path / "current-b"
    controller.materialize_revision(desired_b, materialized_b)
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
        revision_b = commit(working, "template B")
        git(working, "push", "origin", "main")
        desired_b, changed_b = _advance(target, target_revision, monkeypatch)
        desired_c, changed_c = _advance(target, target_revision, monkeypatch)
        assert not changed_b
        assert not changed_c
        assert desired_b == desired_a
        assert desired_c == desired_b

    assert revision_b != revision_a
    materialized_b = tmp_path / "current-b"
    controller.materialize_revision(desired_c, materialized_b)
    resolved_source = _stack(materialized_b).spec.resolvedSource
    assert resolved_source is not None
    assert resolved_source.fromGit.commit == revision_a
    specifications, _ = controller.load_convergence_specifications(
        target, "dev", materialized_b, target_revision, tmp_path / "reconcile-fixed"
    )
    assert specifications["application--deploy"].spec.terraform.variables["marker"] == "A"


def test_promoted_remote_source_uses_exact_commit_with_target_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    remote, working, revision_a = _remote_repository(tmp_path, parameterized=True)
    with _git_daemon(tmp_path, remote) as daemon:
        source_stack = _stack_source(daemon.url)
        source_stack["spec"]["parameters"] = {"target": "dev"}
        target, target_revision_a = _target_repository(tmp_path, source_stack)
        monkeypatch.setattr(controller, "REPOSITORY_ROOT", target)
        controller._state_store.cache_clear()
        dev_desired = tmp_path / "dev-desired"
        controller.project_stack_resources(target, "dev", target_revision_a, dev_desired, target)

        (working / "deployment/stack-templates/application.json").write_text(
            json.dumps(_template("B", parameterized=True))
        )
        revision_b = commit(working, "template B")
        git(working, "push", "origin", "main")
        assert revision_b != revision_a

        staging = target / "deployment/environments/staging"
        (staging / "stacks").mkdir(parents=True)
        (staging / "environment.json").write_text(
            json.dumps(
                {
                    "apiVersion": "gitopsctr.io/v1",
                    "kind": "Environment",
                    "metadata": {"name": "staging"},
                    "spec": {},
                }
            )
        )
        promoted_stack = _stack_source(daemon.url)
        promoted_stack["spec"] = {
            "template": {
                "name": "application",
                "source": {"fromPromotion": {"stack": "application"}},
            },
            "parameters": {"target": "staging"},
        }
        (staging / "stacks/application.json").write_text(json.dumps(promoted_stack))
        target_revision_b = commit(target, "staging promotion")
        promotion = controller.PromotionContext(
            source_environment="dev",
            desired_ref="deploy/dev",
            desired_revision="d" * 40,
            observed_ref="observed/dev",
            observed_revision=None,
            specification_revision=target_revision_b,
            desired_root=dev_desired,
        )

        output = tmp_path / "staging-desired"
        projection = controller.project_stack_resources(
            target,
            "staging",
            target_revision_b,
            output,
            target,
            promotion=promotion,
        )
        variables = projection.generated_units["application--deploy"].spec.terraform.variables
        assert variables == {"marker": "A", "target": "staging"}
        resolved = _stack(output).spec.resolvedSource.fromGit
        assert resolved.remote == daemon.url
        assert resolved.commit == revision_a

        daemon.stop()
        with pytest.raises(OperationError, match="could not fetch"):
            controller.project_stack_resources(
                target,
                "staging",
                target_revision_b,
                tmp_path / "unavailable-remote",
                target,
                promotion=promotion,
            )


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("fetch", "could not fetch"),
        ("ref", "does not exist"),
        ("path", "expected exactly one StackTemplate"),
        ("invalid-source", "invalid authored Stack"),
    ),
)
def test_remote_source_failures_do_not_publish_desired_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected: str
):
    if failure == "fetch":
        source = _stack_source("git://127.0.0.1:1/missing.git")
        target, target_revision = _target_repository(tmp_path, source)
        monkeypatch.setattr(controller, "REPOSITORY_ROOT", target)
        with pytest.raises(OperationError, match=expected):
            controller.advance_desired("dev", target_revision, verbose=False, summarize=False)
    elif failure == "invalid-source":
        source = _stack_source("https://user:password@example.invalid/repository.git")
        target, target_revision = _target_repository(tmp_path, source)
        monkeypatch.setattr(controller, "REPOSITORY_ROOT", target)
        with pytest.raises(OperationError, match=expected):
            controller.advance_desired("dev", target_revision, verbose=False, summarize=False)
    else:
        remote, _, _ = _remote_repository(tmp_path, include_template=failure != "path")
        with _git_daemon(tmp_path, remote) as daemon:
            source = _stack_source(daemon.url, ref="missing" if failure == "ref" else "main")
            target, target_revision = _target_repository(tmp_path, source)
            monkeypatch.setattr(controller, "REPOSITORY_ROOT", target)
            with pytest.raises(OperationError, match=expected):
                controller.advance_desired("dev", target_revision, verbose=False, summarize=False)
    assert controller.fetch_ref("gitopsctr/desired/dev") is None
