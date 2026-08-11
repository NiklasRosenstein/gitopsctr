"""Temporary-repository acceptance coverage for Stack desired-state lifecycle."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gitopsctr import controller
from gitopsctr.state import GitStateStore
from tests.stack_support import commit, git, project_repository, write_projected_units, write_stack_source


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
    git(tmp_path, "init", "--bare", str(remote))
    environment = project_repository(source)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    write_stack_source(environment)
    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["unitTemplates"] = {
        "preview-db": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "spec": {"source": {"path": "."}},
        },
        "preview-app": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "dependsOn": ["preview-db"],
            "spec": {"source": {"path": "."}},
        },
    }
    template_path.write_text(json.dumps(template))
    source_revision = commit(source, "add preview Stack")
    git(source, "push", "-u", "origin", "main")
    return source, environment, source_revision


def _template_only_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    source, environment, source_revision = _source_repository(tmp_path)
    for path in (environment / "stacks").glob("web.*"):
        path.unlink()
    source_revision = commit(source, "remove source Stack")
    git(source, "push", "origin", "main")
    return source, environment, source_revision


def test_direct_stack_instantiation_from_template_only_source_is_replay_safe(tmp_path: Path, monkeypatch):
    source, environment, source_revision = _template_only_repository(tmp_path)
    store = GitStateStore(source)
    initial = tmp_path / "initial"
    projection = controller.project_stack_resources(source, "dev", source_revision, initial, source)
    assert projection.generated_units == {}
    source_head = git(source, "rev-parse", "HEAD")
    desired = store.publish("deploy/dev", initial, None, "publish template-only desired state")

    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    args = controller.build_parser().parse_args(
        [
            "instantiate-stack",
            "--environment",
            "dev",
            "--stack",
            "web",
            "--template",
            "preview",
            "--source-revision",
            source_revision,
            "--parameters",
            '{"source-path":"."}',
            "--request-id",
            "github:example/application#123",
            "--desired-ref",
            "deploy/dev",
            "--observed-ref",
            "observed/dev",
        ]
    )

    assert controller.command_instantiate_stack(args) is True
    instantiated_revision = store.fetch("deploy/dev").revision
    assert instantiated_revision is not None
    assert instantiated_revision != desired.revision

    instantiated = tmp_path / "instantiated"
    store.materialize(instantiated_revision, instantiated)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((instantiated / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert stack.metadata.lifecycle is not None
    assert stack.metadata.lifecycle.management is not None
    assert stack.metadata.lifecycle.management.mode == "direct"
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.provenance is not None
    assert stack.spec.provenance.templateRevision == source_revision
    assert stack.spec.provenance.templatePath.endswith("stack-templates/preview.json")
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(next((instantiated / "stack-templates").glob("preview.*"))),
        profile="desired",
        expected_name="preview",
    )
    assert template.metadata.uid is not None
    unit = controller.load_desired_unit(next((instantiated / "units").glob("web--preview-app.*")), "web--preview-app")
    assert unit.metadata.lifecycle is not None
    assert unit.metadata.lifecycle.owner is not None
    assert unit.metadata.lifecycle.owner.uid == stack.metadata.uid
    assert not list((environment / "stacks").glob("web.*"))
    assert git(source, "rev-parse", "HEAD") == source_head

    assert controller.command_instantiate_stack(args) is False
    assert store.fetch("deploy/dev").revision == instantiated_revision


def test_stack_lifecycle_survives_git_restart_and_tears_down_in_reverse_order(tmp_path: Path):
    source, environment, source_revision = _source_repository(tmp_path)
    store = GitStateStore(source)
    initial = tmp_path / "initial"
    projection = controller.project_stack_resources(source, "dev", source_revision, initial, source)
    write_projected_units(initial, projection, source)
    resources = controller.load_desired_resource_graph(initial)
    units = {
        resource.name: resource
        for (api_version, _kind, _name), resource in resources.items()
        if api_version == controller.UNIT_API_VERSION
    }
    order = controller.convergence_order(units, tuple(units), projection.dependencies)
    assert order == ("web--preview-db", "web--preview-app")

    inventory = Inventory(projection.dependencies)
    inventory.deploy(order)
    desired = store.publish("deploy/dev", initial, None, "publish initial Stack desired state")
    initial_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((initial / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert initial_stack.metadata.uid is not None

    (environment / "stacks/web.json").unlink()
    source_revision_without_stack = commit(source, "remove preview Stack")
    current = tmp_path / "current"
    store.materialize(desired.revision, current)
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    observed.mkdir()
    result = controller.build_desired_candidate(
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
    intent = controller.load_desired_stack_deletion_intents(candidate)["web"]
    assert intent.uid == initial_stack.metadata.uid
    assert {item.unit_name for item in intent.owned_unit_closure} == set(order)

    deleted = store.publish("deploy/dev", candidate, desired.revision, "retain Stack deletion intent")
    restarted = tmp_path / "restarted"
    GitStateStore(source).materialize(deleted.revision, restarted)
    assert controller.load_desired_stack_deletion_intents(restarted)["web"] == intent

    inventory.destroy(tuple(reversed(order)))
    assert inventory.active == set()
    assert [name for action, name in inventory.events if action == "destroy"] == list(reversed(order))

    finalized = tmp_path / "finalized"
    shutil.copytree(restarted, finalized)
    controller.write_stack_incarnation_tombstone(
        finalized,
        controller.StackIncarnationTombstone(stack_name="web", uid=initial_stack.metadata.uid),
    )
    for path in controller.document_candidates(finalized / "stacks", "web"):
        path.unlink()
    for path in controller.document_candidates(finalized / controller.DESIRED_STACK_DELETION_INTENTS_PATH, "web"):
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
    recreated_revision = commit(source, "recreate preview Stack")
    recreated = tmp_path / "recreated"
    recreated_projection = controller.project_stack_resources(
        source, "dev", recreated_revision, recreated, source, finalized
    )
    recreated_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((recreated / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert recreated_stack.metadata.uid is not None
    assert recreated_stack.metadata.uid != initial_stack.metadata.uid
    assert recreated_projection.dependencies == projection.dependencies
