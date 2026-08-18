"""Focused external and historical evidence acquisition coverage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from dulwich.repo import Repo

from gitopsctr.adapters.git.apply import GitApplySourceEvidenceProvider
from gitopsctr.adapters.git.source_lineage import GitSourceLineageRegistry
from gitopsctr.adapters.memory.sources import MemorySourceRepository
from gitopsctr.application.apply import ApplyCommand, AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.apply_orchestration import ApplyEnvironmentConfiguration
from gitopsctr.application.apply_projection import ApplyProjectionPolicy, ExactPlane, SourceBindingRole
from gitopsctr.application.model import ChannelId, ContentId, EnvironmentId, HeadObservation, SourceId
from gitopsctr.application.workspace import InMemoryWorkspace
from gitopsctr.errors import OperationError
from gitopsctr.resource_api import JsonObject

DESIRED = ChannelId("desired/dev")
OBSERVED = ChannelId("observed/dev")


def _external_repository(root: Path) -> tuple[Path, str]:
    Repo.init(root, mkdir=True).close()
    subprocess.run(("git", "-C", str(root), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.test"), check=True)
    template = root / "templates" / "shared.json"
    template.parent.mkdir()
    template.write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "shared"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {"source": {"path": "workload"}},
                        }
                    },
                },
            }
        )
    )
    workload = root / "workload"
    workload.mkdir()
    (workload / "main.tf").write_text('resource "null_resource" "app" {}\n')
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    subprocess.run(("git", "-C", str(root), "commit", "-m", "external source"), check=True)
    revision = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()
    return root, revision


def _document(name: str, kind: str, spec: dict[str, object]) -> JsonObject:
    return cast(
        JsonObject,
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": kind,
            "metadata": {"name": name},
            "spec": spec,
        },
    )


def _external_changes(repository: Path, revision: str, *, duplicate: bool = False) -> AuthoredChangeSet:
    documents = []
    names = ("one", "two") if duplicate else ("one",)
    for index, name in enumerate(names):
        documents.extend(
            (
                _issue_authored_document(
                    f"template-{name}",
                    _document(
                        name,
                        "StackTemplate",
                        {
                            "source": {
                                "fromGit": {
                                    "repository": str(repository),
                                    "revision": revision,
                                    "path": "templates/shared.json",
                                }
                            }
                        },
                    ),
                    ContentId(f"external-template-{index}"),
                ),
                _issue_authored_document(
                    f"stack-{name}",
                    _document(f"stack-{name}", "Stack", {"template": name}),
                    ContentId(f"external-stack-{index}"),
                ),
            )
        )
    return AuthoredChangeSet(tuple(documents))


def _plane(channel: ChannelId) -> ExactPlane:
    workspace = InMemoryWorkspace(mutable=False)
    return ExactPlane(HeadObservation.absent(channel, f"absent-{channel.value}"), workspace)


def _configuration() -> ApplyEnvironmentConfiguration:
    return ApplyEnvironmentConfiguration(DESIRED, OBSERVED, None, ApplyProjectionPolicy())


def _command() -> ApplyCommand:
    return ApplyCommand(EnvironmentId("dev"), ("inputs",), DESIRED, OBSERVED, None, None)


def _provider(tmp_path: Path) -> tuple[GitApplySourceEvidenceProvider, GitSourceLineageRegistry]:
    retention = tmp_path / "retention"
    retention.mkdir(mode=0o700)
    registry = GitSourceLineageRegistry()
    source = MemorySourceRepository(SourceId("default-git-source"))
    return GitApplySourceEvidenceProvider(source, source.source_id, tmp_path, retention, registry), registry


def test_external_evidence_deduplicates_one_revision_and_reissues_retention(tmp_path: Path) -> None:
    external, revision = _external_repository(tmp_path / "external")
    changes = _external_changes(external, revision, duplicate=True)
    provider, registry = _provider(tmp_path)

    first = provider.prepare(_command(), changes, _configuration(), _plane(DESIRED), _plane(OBSERVED))

    assert len(first.retained_planes) == 1
    assert len(first.release_on_nonpublication) == 1
    descriptors = first.retained_planes[0].descriptors
    assert {item.binding_key for item in descriptors if item.role is SourceBindingRole.STACK_TEMPLATE} == {
        "one",
        "two",
    }
    assert {item.binding_key for item in descriptors if item.role is SourceBindingRole.WORKLOAD} == {
        "stack-one/app",
        "stack-two/app",
    }
    assert len(registry.repositories) == 1

    recovered = provider.prepare(_command(), changes, _configuration(), _plane(DESIRED), _plane(OBSERVED))

    assert len(recovered.retained_planes) == 1
    assert recovered.retained_planes[0].retained.handle == first.retained_planes[0].retained.handle
    assert recovered.release_on_nonpublication == ()


def test_external_evidence_requires_configuration_and_available_repository(tmp_path: Path) -> None:
    missing = tmp_path / "missing.git"
    changes = _external_changes(missing, "a" * 40)
    source = MemorySourceRepository(SourceId("default-git-source"))
    unconfigured = GitApplySourceEvidenceProvider(source, source.source_id)

    with pytest.raises(OperationError, match="external Git source acquisition is not configured"):
        unconfigured.prepare(_command(), changes, _configuration(), _plane(DESIRED), _plane(OBSERVED))

    configured, _registry = _provider(tmp_path)
    with pytest.raises(OperationError, match="external Git source .* is unavailable"):
        configured.prepare(_command(), changes, _configuration(), _plane(DESIRED), _plane(OBSERVED))


def test_external_acquisition_preserves_primary_error_when_cleanup_also_fails(tmp_path: Path) -> None:
    external, revision = _external_repository(tmp_path / "external")
    external_changes = _external_changes(external, revision)
    local_template = _issue_authored_document(
        "local-template",
        _document(
            "local",
            "StackTemplate",
            {
                "parameters": [],
                "unitTemplates": {
                    "app": {
                        "apiVersion": "unit.gitopsctr.io/v1",
                        "kind": "Terraform",
                        "spec": {"source": {"path": ".", "revision": "d" * 40}},
                    }
                },
            },
        ),
        ContentId("local-template"),
    )
    local_stack = _issue_authored_document(
        "local-stack",
        _document("local-stack", "Stack", {"template": "local"}),
        ContentId("local-stack"),
    )
    changes = AuthoredChangeSet((*external_changes.documents, local_template, local_stack))
    provider, _registry = _provider(tmp_path)

    with pytest.raises(OperationError, match="source revision .* is unavailable") as captured:
        provider.prepare(_command(), changes, _configuration(), _plane(DESIRED), _plane(OBSERVED))

    assert captured.value.__notes__
    assert "also failed to release historical source handle" in captured.value.__notes__[0]


def test_repository_backed_stack_unit_requires_an_exact_source_revision(tmp_path: Path) -> None:
    template = _issue_authored_document(
        "inline-template",
        _document(
            "inline",
            "StackTemplate",
            {
                "parameters": [],
                "unitTemplates": {
                    "app": {
                        "apiVersion": "unit.gitopsctr.io/v1",
                        "kind": "Terraform",
                        "spec": {"source": {"path": "workload"}},
                    }
                },
            },
        ),
        ContentId("inline-template"),
    )
    stack = _issue_authored_document(
        "inline-stack",
        _document("inline-stack", "Stack", {"template": "inline"}),
        ContentId("inline-stack"),
    )
    provider, _registry = _provider(tmp_path)

    with pytest.raises(OperationError, match="requires an exact retained source revision"):
        provider.prepare(
            _command(),
            AuthoredChangeSet((template, stack)),
            _configuration(),
            _plane(DESIRED),
            _plane(OBSERVED),
        )
