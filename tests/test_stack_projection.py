from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli
from gitopsctr.contracts import (
    DesiredLifecycle,
    DesiredOwnerReference,
    DesiredStackSpec,
    LifecycleManagement,
    StackInstantiationProvenance,
)
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, StackResource, validate_desired_resource_graph
from gitopsctr.state import ControllerPin, ControllerPinClaim


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    root.joinpath("gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {},
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


def _write_stack_source(environment: Path) -> None:
    templates = environment / "stack-templates"
    stacks = environment / "stacks"
    templates.mkdir()
    stacks.mkdir()
    (templates / "preview.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [{"name": "source-path", "type": "string"}],
                    "resources": [
                        {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "name": "preview-app",
                            "spec": {"source": {"path": {"fromParameter": {"name": "source-path"}}}},
                        }
                    ],
                },
            }
        )
    )
    (stacks / "web.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "web"},
                "spec": {"template": "preview", "parameters": {"source-path": "."}},
            }
        )
    )


def test_stack_projection_is_deterministic_and_templates_are_inert(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)

    first = cli.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate-a", source)
    second = cli.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate-b", source)

    assert sorted(first.generated_units) == ["web--preview-app"]
    assert first.owners == second.owners
    assert first.generated_units["web--preview-app"].spec == second.generated_units["web--preview-app"].spec
    assert list((tmp_path / "candidate-a/stack-templates").glob("preview.*"))
    assert not list((tmp_path / "candidate-a/units").glob("*"))


def test_existing_stack_root_uids_are_preserved_across_source_revisions(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    initial = tmp_path / "initial"
    cli.project_stack_resources(source, "dev", "a" * 40, initial, source)
    current = tmp_path / "current"
    shutil.copytree(initial, current)

    next_candidate = tmp_path / "next"
    cli.project_stack_resources(source, "dev", "b" * 40, next_candidate, source, current)

    for kind, directory in (("StackTemplate", "stack-templates"), ("Stack", "stacks")):
        old_path = next(
            path for path in (current / directory).glob("preview.*" if kind == "StackTemplate" else "web.*")
        )
        new_path = next(
            path for path in (next_candidate / directory).glob("preview.*" if kind == "StackTemplate" else "web.*")
        )
        old = (
            cli.RESOURCE_CATALOG.parse_stack_template(cli.RESOURCE_CATALOG.load_document(old_path), profile="desired")
            if kind == "StackTemplate"
            else cli.RESOURCE_CATALOG.parse_stack(cli.RESOURCE_CATALOG.load_document(old_path), profile="desired")
        )
        new = (
            cli.RESOURCE_CATALOG.parse_stack_template(cli.RESOURCE_CATALOG.load_document(new_path), profile="desired")
            if kind == "StackTemplate"
            else cli.RESOURCE_CATALOG.parse_stack(cli.RESOURCE_CATALOG.load_document(new_path), profile="desired")
        )
        assert old.metadata.uid == new.metadata.uid


def test_recreated_source_stack_does_not_reuse_finalized_uid(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    initial = tmp_path / "initial"
    cli.project_stack_resources(source, "dev", "a" * 40, initial, source)
    old_path = next((initial / "stacks").glob("web.*"))
    old_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(old_path), profile="desired", expected_name="web"
    )
    assert old_stack.metadata.uid is not None

    current = tmp_path / "current"
    shutil.copytree(initial, current)
    for path in (current / "stacks").glob("web.*"):
        path.unlink()
    cli.write_stack_incarnation_tombstone(
        current,
        cli.StackIncarnationTombstone(stack_name="web", uid=old_stack.metadata.uid),
    )

    candidate = tmp_path / "candidate"
    cli.project_stack_resources(source, "dev", "a" * 40, candidate, source, current)
    new_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(next((candidate / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert new_stack.metadata.uid != old_stack.metadata.uid


def test_source_absent_stack_root_is_retained_for_owned_unit_cleanup(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    initial = tmp_path / "initial"
    cli.project_stack_resources(source, "dev", "a" * 40, initial, source)
    current = tmp_path / "current"
    shutil.copytree(initial, current)

    (environment / "stacks/web.json").unlink()
    next_candidate = tmp_path / "next"
    projection = cli.project_stack_resources(source, "dev", "b" * 40, next_candidate, source, current)

    assert not projection.generated_units
    assert list((next_candidate / "stacks").glob("web.*"))
    assert list((next_candidate / "stack-templates").glob("preview.*"))


def test_expanded_stack_dependencies_are_retained_and_validated(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["resources"].append(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "name": "preview-db",
            "spec": {"source": {"path": "."}},
        }
    )
    template["spec"]["resources"][0]["dependsOn"] = ["preview-db"]
    template_path.write_text(json.dumps(template))

    candidate = tmp_path / "candidate"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    assert projection.dependencies["web--preview-app"] == ("web--preview-db",)
    for name, unit in projection.generated_units.items():
        cli.write_desired_candidate_unit(
            candidate / "units" / f"{name}.json",
            unit.with_metadata(
                ResourceMetadata(
                    name=name,
                    uid=f"d1-generated-{name}",
                    lifecycle=DesiredLifecycle(owner=projection.owners[name]),
                )
            ),
            source,
        )
    cli.load_desired_resource_graph(candidate)
    current_desired = tmp_path / "empty-desired"
    current_desired.mkdir()
    specifications, dependencies = cli.load_convergence_specifications(
        source,
        "dev",
        current_desired,
        "a" * 40,
        tmp_path / "convergence-projection",
    )
    selection = cli.convergence_scope(specifications, ["web--preview-app"], additional_dependencies=dependencies)
    assert selection.scope == ("web--preview-app", "web--preview-db")
    assert cli.convergence_order(specifications, selection.scope, dependencies) == (
        "web--preview-db",
        "web--preview-app",
    )
    next(path for path in (candidate / "units").glob("web--preview-db.*")).unlink()
    with pytest.raises(OperationError, match="dependency 'web--preview-db' is absent"):
        cli.load_desired_resource_graph(candidate)


def test_desired_graph_loads_stack_roots_and_uid_fenced_generated_unit(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    candidate = tmp_path / "candidate"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    owner = projection.owners["web--preview-app"]
    unit = projection.generated_units["web--preview-app"].with_metadata(
        ResourceMetadata(
            name="web--preview-app",
            uid="d1-generated-preview-app",
            lifecycle=DesiredLifecycle(owner=owner),
        )
    )
    cli.write_desired_candidate_unit(candidate / "units/web--preview-app.json", unit, source)

    graph = cli.load_desired_resource_graph(candidate)
    assert cli.desired_unit_names(candidate) == ("web--preview-app",)
    assert ("gitopsctr.io/v1", "StackTemplate", "preview") in graph
    assert ("gitopsctr.io/v1", "Stack", "web") in graph
    assert graph[("unit.gitopsctr.io/v1", "Terraform", "web--preview-app")].metadata.lifecycle is not None

    bad_owner = DesiredOwnerReference(
        apiVersion=owner.apiVersion,
        kind=owner.kind,
        name=owner.name,
        uid="d1-wrong-owner",
    )
    with pytest.raises(ValueError, match="different UID"):
        validate_desired_resource_graph(
            {
                key: value.with_metadata(
                    ResourceMetadata(
                        name=value.name,
                        uid=value.metadata.uid,
                        lifecycle=DesiredLifecycle(owner=bad_owner),
                    )
                )
                if key[2] == "web--preview-app"
                else value
                for key, value in graph.items()
                if not isinstance(value, StackResource) or value.gvk.kind != "StackTemplate"
            }
        )


def test_convergence_discovers_desired_only_stack_units(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    authored_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(environment / "stacks/web.json"),
        profile="authored",
        expected_name="web",
    )
    desired = tmp_path / "desired"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, desired, source)
    (environment / "stacks/web.json").unlink()
    stack = StackResource(
        cli.GVK(cli.CORE_API_VERSION, "Stack"),
        ResourceMetadata(
            name="web",
            uid="d1-stack-web",
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="direct")),
        ),
        DesiredStackSpec(
            template="preview",
            parameters=authored_stack.spec.parameters,  # type: ignore[union-attr]
            provenance=StackInstantiationProvenance(
                templateRevision="a" * 40,
                templatePath="deployment/environments/dev/stack-templates/preview.json",
                templateDigest="b" * 64,
                requestIdentity="pull-123",
            ),
        ),
    )
    for path in cli.document_candidates(desired / "stacks", "web"):
        path.unlink()
    (desired / "stacks/web.json").write_text(
        json.dumps(cli.RESOURCE_CATALOG.serialize_stack_resource(stack, profile="desired"))
    )
    unit = projection.generated_units["web--preview-app"].with_metadata(
        ResourceMetadata(
            name="web--preview-app",
            uid="d1-unit-preview-app",
            lifecycle=DesiredLifecycle(
                owner=DesiredOwnerReference(
                    apiVersion=cli.CORE_API_VERSION,
                    kind="Stack",
                    name="web",
                    uid="d1-stack-web",
                )
            ),
        )
    )
    cli.write_desired_candidate_unit(desired / "units/web--preview-app.json", unit, source)

    specifications, dependencies = cli.load_convergence_specifications(
        source,
        "dev",
        desired,
        "a" * 40,
        tmp_path / "projection",
    )
    assert specifications["web--preview-app"].metadata.lifecycle is not None
    assert specifications["web--preview-app"].metadata.lifecycle.owner is not None
    assert dependencies == {"web--preview-app": ()}


def test_stack_rejects_missing_template_during_projection(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    stacks = environment / "stacks"
    stacks.mkdir()
    (stacks / "web.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "web"},
                "spec": {"template": "missing", "parameters": {}},
            }
        )
    )

    with pytest.raises(OperationError, match="missing StackTemplate"):
        cli.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate", source)


def test_instantiate_stack_publishes_direct_uid_fenced_owner_graph(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    (environment / "stacks/web.json").unlink()
    current = tmp_path / "current"
    current.mkdir()
    published: list[Path] = []
    events: list[str] = []
    source_revision = "a" * 40

    def materialize(revision: str, output: Path) -> None:
        shutil.copytree(source if revision == source_revision else current, output)

    def fake_build(_environment, source_root, revision, _current, _observed, _observed_revision, candidate, **_kwargs):
        projection = cli.project_stack_resources(source_root, "dev", revision, candidate, source_root)
        for name in projection.generated_units:
            unit_document = {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": {
                    "name": name,
                    "uid": "d1-generated-unit",
                    "lifecycle": {"management": {"mode": "sourceTracked"}},
                },
                "spec": {
                    "source": {
                        "path": ".",
                        "revision": source_revision,
                        "inputHash": "sha256:" + "0" * 64,
                        "driverVersion": cli.DRIVER_VERSIONS["terraform"],
                    },
                    "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
                },
            }
            unit = cli.RESOURCE_CATALOG.parse_unit(unit_document, profile="desired", expected_name=name)
            cli.write_desired_candidate_unit(candidate / "units" / f"{name}.json", unit, source_root)

    def publish(_environment, candidate, *_args, **kwargs):
        assert kwargs["request_change"] is False
        events.append("publish")
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "c" * 40, None

    monkeypatch.setattr(cli, "REPOSITORY_ROOT", source)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda ref: "b" * 40 if ref == "deploy/dev" else None)
    monkeypatch.setattr(cli, "materialize_revision", materialize)
    monkeypatch.setattr(cli, "observed_tree", lambda _ref, output: output.mkdir(parents=True) or None)
    monkeypatch.setattr(
        cli, "git", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=source_revision + "\n")
    )
    monkeypatch.setattr(cli, "build_desired_candidate", fake_build)
    monkeypatch.setattr(cli, "publish_desired_change", publish)
    monkeypatch.setattr(cli, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def create_claim(claim: ControllerPinClaim) -> ControllerPinClaim:
        events.append("claim")
        return replace(claim, revision="d" * 40)

    def create_pin(name: str, revision: str) -> ControllerPin:
        events.append("pin")
        return ControllerPin(name, f"refs/heads/gitopsctr/pins/{name}", revision)

    def update_claim(claim: ControllerPinClaim, expected_revision: str) -> ControllerPinClaim:
        assert expected_revision == "d" * 40
        assert claim.state == "active"
        events.append("activate")
        return replace(claim, revision="e" * 40)

    store = SimpleNamespace(
        create_controller_pin_claim=create_claim,
        create_controller_pin=create_pin,
        update_controller_pin_claim=update_claim,
    )
    monkeypatch.setattr(
        cli,
        "state_store",
        lambda: store,
    )

    args = cli.build_parser().parse_args(
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
            "pull-123",
        ]
    )
    assert cli.command_instantiate_stack(args) is True
    assert events == ["claim", "pin", "publish", "activate"]
    candidate = published[0]
    stack_path = next((candidate / "stacks").glob("web.*"))
    stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(stack_path), profile="desired", expected_name="web"
    )
    assert stack.metadata.lifecycle is not None
    assert stack.metadata.lifecycle.management is not None
    assert stack.metadata.lifecycle.management.mode == "direct"
    assert isinstance(stack.spec, cli.DesiredStackSpec)
    assert stack.spec.provenance is not None
    unit = cli.load_desired_unit(next((candidate / "units").glob("web--preview-app.*")), "web--preview-app")
    assert unit.metadata.lifecycle is not None
    assert unit.metadata.lifecycle.owner is not None
    assert unit.metadata.lifecycle.owner.uid == stack.metadata.uid
