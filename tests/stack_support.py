"""Shared Stack repository and desired-state test builders."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from gitopsctr import controller
from gitopsctr.resources import ResourceMetadata
from gitopsctr.state import GitStateStore


def project_repository(root: Path) -> Path:
    """Create a minimal project and return its development environment."""
    root.mkdir(parents=True)
    (root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {"effectLease": None},
            }
        )
    )
    environment = root / "deployment/environments/dev"
    environment.mkdir(parents=True)
    (environment / "environment.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": "dev"},
                "spec": {},
            }
        )
    )
    return environment


def write_stack_source(
    environment: Path,
    *,
    unit_templates: dict[str, dict[str, Any]] | None = None,
    stack_name: str = "web",
    template_name: str = "preview",
) -> None:
    """Write a canonical unitTemplates StackTemplate and a Stack using it."""
    templates = environment.parents[1] / "stack-templates"
    stacks = environment / "stacks"
    templates.mkdir(parents=True, exist_ok=True)
    stacks.mkdir(parents=True, exist_ok=True)
    if unit_templates is None:
        unit_templates = {
            "preview-app": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": {"fromParameter": {"name": "source-path"}}}},
            }
        }
    (templates / f"{template_name}.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": template_name},
                "spec": {
                    "parameters": [{"name": "source-path", "type": "string"}],
                    "unitTemplates": unit_templates,
                },
            }
        )
    )
    (stacks / f"{stack_name}.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": stack_name},
                "spec": {"template": template_name, "parameters": {"source-path": "."}},
            }
        )
    )


def write_projected_units(
    root: Path,
    projection: controller.StackProjection,
    source_root: Path,
    *,
    uid_prefix: str = "d1-",
) -> None:
    """Materialize projected Units with UID-fenced Stack ownership."""
    for name, unit in projection.generated_units.items():
        controller.write_desired_candidate_unit(
            root / "units" / f"{name}.json",
            unit.with_metadata(
                ResourceMetadata(
                    name=name,
                    uid=f"{uid_prefix}{name}",
                    ownerReferences=[projection.owners[name]],
                )
            ),
            source_root,
        )


def git(root: Path, *args: str, check: bool = True) -> str:
    """Run Git with deterministic test identity and return standard output."""
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


def commit(root: Path, message: str) -> str:
    """Commit all changes and return the resulting revision."""
    git(root, "add", "--all")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


@dataclass(frozen=True)
class ProjectRepositorySeed:
    """An immutable remote retained by its owning temporary directory."""

    temporary_directory: TemporaryDirectory[str]
    remote: Path


def _build_project_repository_seed(root: Path) -> Path:
    remote = root / "origin.git"
    source = root / "source"
    git(root, "init", "--bare", str(remote))
    project_repository(source)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    commit(source, "initialize source")
    git(source, "push", "-u", "origin", "main")
    baseline = root / "baseline"
    baseline.mkdir()
    (baseline / ".gitkeep").write_text("")
    GitStateStore(source).publish(
        "deploy/dev",
        baseline,
        None,
        "initialize desired state",
        expected_publication_head=None,
    )
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


@cache
def project_repository_seed() -> ProjectRepositorySeed:
    """Build the common source/desired history once per test process."""
    temporary_directory = TemporaryDirectory(prefix="gitopsctr-project-tests-")
    remote = _build_project_repository_seed(Path(temporary_directory.name))
    return ProjectRepositorySeed(temporary_directory, remote)


def cloned_project_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, GitStateStore, str]:
    """Clone an isolated project and desired-state remote from the shared seed."""
    seed = project_repository_seed()
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "clone", "--bare", "--local", str(seed.remote), str(remote))
    git(tmp_path, "clone", "--local", str(remote), str(source))
    revision = git(source, "rev-parse", "HEAD")
    store = GitStateStore(source)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    return source, store, revision
