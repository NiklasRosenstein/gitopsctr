"""Shared Stack repository and desired-state test builders."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from gitopsctr import controller
from gitopsctr.resources import ResourceMetadata


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
    templates = environment / "stack-templates"
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
                    lifecycle=controller.DesiredLifecycle(owner=projection.owners[name]),
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
