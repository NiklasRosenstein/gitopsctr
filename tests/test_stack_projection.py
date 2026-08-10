from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.contracts import DesiredLifecycle, DesiredOwnerReference
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, StackResource, validate_desired_resource_graph


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

    assert sorted(first.generated_units) == ["preview-app"]
    assert first.owners == second.owners
    assert first.generated_units["preview-app"].spec == second.generated_units["preview-app"].spec
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
    assert projection.dependencies["preview-app"] == ("preview-db",)
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
    next(path for path in (candidate / "units").glob("preview-db.*")).unlink()
    with pytest.raises(OperationError, match="dependency 'preview-db' is absent"):
        cli.load_desired_resource_graph(candidate)


def test_desired_graph_loads_stack_roots_and_uid_fenced_generated_unit(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    candidate = tmp_path / "candidate"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    owner = projection.owners["preview-app"]
    unit = projection.generated_units["preview-app"].with_metadata(
        ResourceMetadata(
            name="preview-app",
            uid="d1-generated-preview-app",
            lifecycle=DesiredLifecycle(owner=owner),
        )
    )
    cli.write_desired_candidate_unit(candidate / "units/preview-app.json", unit, source)

    graph = cli.load_desired_resource_graph(candidate)
    assert ("gitopsctr.io/v1", "StackTemplate", "preview") in graph
    assert ("gitopsctr.io/v1", "Stack", "web") in graph
    assert graph[("unit.gitopsctr.io/v1", "Terraform", "preview-app")].metadata.lifecycle is not None

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
                if key[2] == "preview-app"
                else value
                for key, value in graph.items()
                if not isinstance(value, StackResource) or value.gvk.kind != "StackTemplate"
            }
        )


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
