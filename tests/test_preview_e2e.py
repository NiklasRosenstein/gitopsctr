"""Temporary-repository acceptance coverage for Stack desired-state lifecycle."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from gitopsctr import cli
from gitopsctr.resources import ResourceMetadata
from gitopsctr.state import GitStateStore
from tests.test_stack_projection import _project, _write_stack_source


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class Inventory:
    """Deterministic external inventory that rejects dependency-unsafe teardown."""

    def __init__(self, dependencies: dict[str, tuple[str, ...]]) -> None:
        self.dependencies = dependencies
        self.active: set[str] = set()
        self.events: list[tuple[str, str]] = []

    def deploy(self, order: tuple[str, ...]) -> None:
        for name in order:
            missing = set(self.dependencies[name]) - self.active
            if missing:
                raise AssertionError(f"{name} deployed before {sorted(missing)}")
            self.active.add(name)
            self.events.append(("deploy", name))

    def destroy(self, order: tuple[str, ...]) -> None:
        for name in order:
            active_dependents = {
                consumer
                for consumer, dependencies in self.dependencies.items()
                if name in dependencies and consumer in self.active
            }
            if active_dependents:
                raise AssertionError(f"{name} destroyed before {sorted(active_dependents)}")
            self.active.remove(name)
            self.events.append(("destroy", name))


def _source_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    _git(tmp_path, "init", "--bare", str(remote))
    environment = _project(source)
    _git(source, "init", "-b", "main")
    _git(source, "remote", "add", "origin", str(remote))
    _write_stack_source(environment)
    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["resources"] = [
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "name": "preview-db",
            "spec": {"source": {"path": "."}},
        },
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "name": "preview-app",
            "dependsOn": ["preview-db"],
            "spec": {"source": {"path": "."}},
        },
    ]
    template_path.write_text(json.dumps(template))
    source_revision = _commit(source, "add preview Stack")
    _git(source, "push", "-u", "origin", "main")
    return source, environment, source_revision


def _write_generated_units(root: Path, projection: cli.StackProjection, source_root: Path) -> None:
    for name, unit in projection.generated_units.items():
        cli.write_desired_candidate_unit(
            root / "units" / f"{name}.json",
            unit.with_metadata(
                ResourceMetadata(
                    name=name,
                    uid=f"d1-{name}",
                    lifecycle=cli.DesiredLifecycle(owner=projection.owners[name]),
                )
            ),
            source_root,
        )


def test_stack_lifecycle_survives_git_restart_and_tears_down_in_reverse_order(tmp_path: Path):
    source, environment, source_revision = _source_repository(tmp_path)
    store = GitStateStore(source)
    initial = tmp_path / "initial"
    projection = cli.project_stack_resources(source, "dev", source_revision, initial, source)
    _write_generated_units(initial, projection, source)
    resources = cli.load_desired_resource_graph(initial)
    units = {
        resource.name: resource
        for (api_version, _kind, _name), resource in resources.items()
        if api_version == cli.UNIT_API_VERSION
    }
    order = cli.convergence_order(units, tuple(units), projection.dependencies)
    assert order == ("web--preview-db", "web--preview-app")

    inventory = Inventory(projection.dependencies)
    inventory.deploy(order)
    desired = store.publish("deploy/dev", initial, None, "publish initial Stack desired state")
    initial_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(next((initial / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert initial_stack.metadata.uid is not None

    (environment / "stacks/web.json").unlink()
    source_revision_without_stack = _commit(source, "remove preview Stack")
    current = tmp_path / "current"
    store.materialize(desired.revision, current)
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    observed.mkdir()
    result = cli.build_desired_candidate(
        "dev",
        source,
        source_revision_without_stack,
        current,
        observed,
        None,
        candidate,
        verbose=False,
    )
    assert result.blocked == {}
    intent = cli.load_desired_stack_deletion_intents(candidate)["web"]
    assert intent.uid == initial_stack.metadata.uid
    assert {item.unit_name for item in intent.owned_unit_closure} == set(order)

    deleted = store.publish("deploy/dev", candidate, desired.revision, "retain Stack deletion intent")
    restarted = tmp_path / "restarted"
    GitStateStore(source).materialize(deleted.revision, restarted)
    assert cli.load_desired_stack_deletion_intents(restarted)["web"] == intent

    inventory.destroy(tuple(reversed(order)))
    assert inventory.active == set()
    assert [name for action, name in inventory.events if action == "destroy"] == list(reversed(order))

    finalized = tmp_path / "finalized"
    shutil.copytree(restarted, finalized)
    cli.write_stack_incarnation_tombstone(
        finalized,
        cli.StackIncarnationTombstone(stack_name="web", uid=initial_stack.metadata.uid),
    )
    for path in cli.document_candidates(finalized / "stacks", "web"):
        path.unlink()
    for path in cli.document_candidates(finalized / cli.DESIRED_STACK_DELETION_INTENTS_PATH, "web"):
        path.unlink()

    (environment / "stacks/web.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "web"},
                "spec": {"template": "preview", "parameters": {"source-path": "."}},
            }
        )
    )
    recreated_revision = _commit(source, "recreate preview Stack")
    recreated = tmp_path / "recreated"
    recreated_projection = cli.project_stack_resources(source, "dev", recreated_revision, recreated, source, finalized)
    recreated_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(next((recreated / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert recreated_stack.metadata.uid is not None
    assert recreated_stack.metadata.uid != initial_stack.metadata.uid
    assert recreated_projection.dependencies == projection.dependencies
