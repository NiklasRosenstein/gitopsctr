from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import (
    DesiredOwnerReference,
    DesiredStackSpec,
    GitSourceRequest,
    StackSpec,
    StackTemplateFromGit,
    StackTemplateFromPromotion,
    StackTemplatePromotionReference,
    StackTemplateReference,
    StackTemplateSource,
    StackTemplateSpec,
    StackTemplateUnitTemplate,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, StackResource, validate_desired_resource_graph
from gitopsctr.templates import TemplateObject
from tests.stack_support import project_repository, write_projected_units, write_stack_source


def test_stack_projection_is_deterministic_and_templates_are_inert(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)

    first = controller.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate-a", source)
    second = controller.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate-b", source)

    assert sorted(first.generated_units) == ["web--preview-app"]
    assert first.owners == second.owners
    assert first.generated_units["web--preview-app"].spec == second.generated_units["web--preview-app"].spec
    assert list((tmp_path / "candidate-a/stack-templates").glob("preview.*"))
    assert not list((tmp_path / "candidate-a/units").glob("*"))


def test_existing_stack_root_uids_are_preserved_across_source_revisions(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    initial = tmp_path / "initial"
    controller.project_stack_resources(source, "dev", "a" * 40, initial, source)
    current = tmp_path / "current"
    shutil.copytree(initial, current)

    next_candidate = tmp_path / "next"
    controller.project_stack_resources(source, "dev", "b" * 40, next_candidate, source, current)

    for kind, directory in (("StackTemplate", "stack-templates"), ("Stack", "stacks")):
        old_path = next(
            path for path in (current / directory).glob("preview.*" if kind == "StackTemplate" else "web.*")
        )
        new_path = next(
            path for path in (next_candidate / directory).glob("preview.*" if kind == "StackTemplate" else "web.*")
        )
        old = (
            controller.RESOURCE_CATALOG.parse_stack_template(
                controller.RESOURCE_CATALOG.load_document(old_path), profile="desired"
            )
            if kind == "StackTemplate"
            else controller.RESOURCE_CATALOG.parse_stack(
                controller.RESOURCE_CATALOG.load_document(old_path), profile="desired"
            )
        )
        new = (
            controller.RESOURCE_CATALOG.parse_stack_template(
                controller.RESOURCE_CATALOG.load_document(new_path), profile="desired"
            )
            if kind == "StackTemplate"
            else controller.RESOURCE_CATALOG.parse_stack(
                controller.RESOURCE_CATALOG.load_document(new_path), profile="desired"
            )
        )
        assert old.metadata.uid == new.metadata.uid


def test_recreated_source_stack_does_not_reuse_finalized_uid(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    initial = tmp_path / "initial"
    controller.project_stack_resources(source, "dev", "a" * 40, initial, source)
    old_path = next((initial / "stacks").glob("web.*"))
    old_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(old_path), profile="desired", expected_name="web"
    )
    assert old_stack.metadata.uid is not None

    current = tmp_path / "current"
    shutil.copytree(initial, current)
    for path in (current / "stacks").glob("web.*"):
        path.unlink()
    controller.write_resource_incarnation_tombstone(
        current,
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="Stack",
            name="web",
            uid=old_stack.metadata.uid,
            deletion_generation=1,
        ),
    )

    candidate = tmp_path / "candidate"
    controller.project_stack_resources(source, "dev", "a" * 40, candidate, source, current)
    new_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((candidate / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert new_stack.metadata.uid != old_stack.metadata.uid


def test_resource_incarnation_lookup_uses_full_gvk(tmp_path: Path):
    root = tmp_path / "desired"
    terraform = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="Terraform",
        name="application",
        uid="d1-terraform",
        deletion_generation=1,
    )
    stack = controller.ResourceIncarnationTombstone(
        api_version=controller.CORE_API_VERSION,
        kind="Stack",
        name="application",
        uid="d1-stack",
        deletion_generation=1,
    )
    controller.write_resource_incarnation_tombstone(root, terraform)
    controller.write_resource_incarnation_tombstone(root, stack)
    tombstones = controller.load_resource_incarnation_tombstones(root)

    assert (
        controller.finalized_incarnation_for_resource(
            tombstones,
            controller.UNIT_API_VERSION,
            "Terraform",
            "application",
        )
        == terraform
    )
    assert (
        controller.finalized_incarnation_for_resource(
            tombstones,
            controller.CORE_API_VERSION,
            "Stack",
            "application",
        )
        == stack
    )


def test_expanded_stack_dependencies_are_retained_and_validated(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    template_path = environment.parents[1] / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["unitTemplates"]["preview-db"] = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "spec": {"source": {"path": "."}},
    }
    template["spec"]["unitTemplates"]["preview-app"]["dependsOn"] = ["preview-db"]
    template_path.write_text(json.dumps(template))

    candidate = tmp_path / "candidate"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    assert projection.dependencies["web--preview-app"] == ("web--preview-db",)
    write_projected_units(candidate, projection, source, uid_prefix="d1-generated-")
    controller.load_desired_resource_graph(candidate)
    current_desired = tmp_path / "empty-desired"
    current_desired.mkdir()
    specifications, dependencies = controller.load_convergence_specifications(
        source,
        "dev",
        current_desired,
        "a" * 40,
        tmp_path / "convergence-projection",
    )
    selection = controller.convergence_scope(specifications, ["web--preview-app"], additional_dependencies=dependencies)
    assert selection.scope == ("web--preview-app", "web--preview-db")
    assert controller.convergence_order(specifications, selection.scope, dependencies) == (
        "web--preview-db",
        "web--preview-app",
    )
    next(path for path in (candidate / "units").glob("web--preview-db.*")).unlink()
    with pytest.raises(OperationError, match="dependency 'web--preview-db' is absent"):
        controller.load_desired_resource_graph(candidate)


NON_RESOURCE_TEMPLATE_SOURCES = (
    StackTemplateFromGit(fromGit=GitSourceRequest(path=".")),
    StackTemplateFromPromotion(fromPromotion=StackTemplatePromotionReference(stack="web")),
)


def _non_resource_stack_graph(tmp_path: Path, source: StackTemplateSource):
    repository = tmp_path / "source"
    environment = project_repository(repository)
    write_stack_source(
        environment,
        unit_templates={
            "preview-db": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            },
            "preview-app": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
                "dependsOn": ["preview-db"],
            },
        },
    )
    candidate = tmp_path / "candidate"
    projection = controller.project_stack_resources(repository, "dev", "a" * 40, candidate, repository)
    write_projected_units(candidate, projection, repository)
    graph = controller.load_desired_resource_graph(candidate)
    stack_key = (controller.CORE_API_VERSION, "Stack", "web")
    stack = graph[stack_key]
    assert isinstance(stack, StackResource)
    assert isinstance(stack.spec, DesiredStackSpec)
    template_reference = StackTemplateReference(name="preview", source=source)
    graph[stack_key] = replace(
        stack,
        spec=replace(stack.spec, template=template_reference, requestedSource=source),
    )
    return graph, stack_key


@pytest.mark.parametrize("source", NON_RESOURCE_TEMPLATE_SOURCES, ids=("from-git", "from-promotion"))
def test_non_resource_stack_sources_validate_resolved_projection_and_ignore_sibling_template(
    tmp_path: Path, source: StackTemplateSource
) -> None:
    graph, _stack_key = _non_resource_stack_graph(tmp_path, source)
    template_key = (controller.CORE_API_VERSION, "StackTemplate", "preview")
    template = graph[template_key]
    assert isinstance(template, StackResource)
    graph[template_key] = replace(
        template,
        spec=StackTemplateSpec(
            unitTemplates={
                "unrelated": StackTemplateUnitTemplate(
                    apiVersion=controller.UNIT_API_VERSION,
                    kind="Terraform",
                    spec=TemplateObject({"source": {"path": "elsewhere"}}),
                )
            }
        ),
    )

    validate_desired_resource_graph(graph)


@pytest.mark.parametrize("source", NON_RESOURCE_TEMPLATE_SOURCES, ids=("from-git", "from-promotion"))
def test_non_resource_stack_sources_reject_missing_and_stale_projected_units(
    tmp_path: Path, source: StackTemplateSource
) -> None:
    graph, stack_key = _non_resource_stack_graph(tmp_path, source)
    missing_key = (controller.UNIT_API_VERSION, "Terraform", "web--preview-db")
    missing = graph.pop(missing_key)
    with pytest.raises(ValueError, match="missing generated Unit 'web--preview-db'"):
        validate_desired_resource_graph(graph)

    graph[missing_key] = missing
    stack = graph[stack_key]
    assert isinstance(stack, StackResource)
    assert isinstance(stack.spec, DesiredStackSpec)
    assert stack.spec.resolvedProjection is not None
    units = dict(stack.spec.resolvedProjection["units"])  # type: ignore[arg-type]
    app = dict(units["preview-app"])  # type: ignore[arg-type]
    app["dependsOn"] = ["absent"]
    units["preview-app"] = app
    graph[stack_key] = replace(
        stack,
        spec=replace(stack.spec, resolvedProjection=JsonObjectValue({"units": units})),
    )
    with pytest.raises(ValueError, match="depends on missing generated Unit 'web--absent'"):
        validate_desired_resource_graph(graph)


@pytest.mark.parametrize("source", NON_RESOURCE_TEMPLATE_SOURCES, ids=("from-git", "from-promotion"))
def test_non_resource_stack_sources_reject_wrong_gvk_and_unexpected_owned_units(
    tmp_path: Path, source: StackTemplateSource
) -> None:
    graph, _stack_key = _non_resource_stack_graph(tmp_path, source)
    app_key = (controller.UNIT_API_VERSION, "Terraform", "web--preview-app")
    app = graph.pop(app_key)
    wrong_key = (controller.UNIT_API_VERSION, "KubernetesManifests", "web--preview-app")
    graph[wrong_key] = replace(app, gvk=controller.GVK(*wrong_key[:2]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="has GVK .*KubernetesManifests, expected .*Terraform"):
        validate_desired_resource_graph(graph)

    graph.pop(wrong_key)
    graph[app_key] = app
    extra = replace(app, metadata=replace(app.metadata, name="web--stale"))  # type: ignore[arg-type]
    graph[(extra.gvk.api_version, extra.gvk.kind, extra.name)] = extra
    with pytest.raises(ValueError, match="unexpected generated Units: web--stale"):
        validate_desired_resource_graph(graph)


def test_desired_graph_loads_stack_roots_and_uid_fenced_generated_unit(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    candidate = tmp_path / "candidate"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    owner = projection.owners["web--preview-app"]
    unit = projection.generated_units["web--preview-app"].with_metadata(
        ResourceMetadata(
            name="web--preview-app",
            uid="d1-generated-preview-app",
            ownerReferences=[owner],
        )
    )
    controller.write_desired_candidate_unit(candidate / "units/web--preview-app.json", unit, source)

    graph = controller.load_desired_resource_graph(candidate)
    assert controller.desired_unit_names(candidate) == ("web--preview-app",)
    assert ("gitopsctr.io/v1", "StackTemplate", "preview") in graph
    assert ("gitopsctr.io/v1", "Stack", "web") in graph
    assert graph[("unit.gitopsctr.io/v1", "Terraform", "web--preview-app")].metadata.ownerReferences is not None

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
                        ownerReferences=[bad_owner],
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
    environment = project_repository(source)
    write_stack_source(environment)
    authored_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(environment / "stacks/web.json"),
        profile="authored",
        expected_name="web",
    )
    assert isinstance(authored_stack.spec, StackSpec)
    desired = tmp_path / "desired"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, desired, source)
    projected_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(desired / "stacks/web.json"),
        profile="desired",
        expected_name="web",
    )
    assert isinstance(projected_stack.spec, DesiredStackSpec)
    (environment / "stacks/web.json").unlink()
    stack = StackResource(
        controller.GVK(controller.CORE_API_VERSION, "Stack"),
        ResourceMetadata(name="web", uid="d1-stack-web").with_partition("application"),
        DesiredStackSpec(
            template="preview",
            parameters=authored_stack.spec.parameters,
            resolvedSource=projected_stack.spec.resolvedSource,
            resolvedProjection=projected_stack.spec.resolvedProjection,
        ),
    )
    for path in controller.document_candidates(desired / "stacks", "web"):
        path.unlink()
    (desired / "stacks/web.json").write_text(
        json.dumps(controller.RESOURCE_CATALOG.serialize_stack_resource(stack, profile="desired"))
    )
    unit = projection.generated_units["web--preview-app"].with_metadata(
        ResourceMetadata(
            name="web--preview-app",
            uid="d1-unit-preview-app",
            ownerReferences=[
                DesiredOwnerReference(
                    apiVersion=controller.CORE_API_VERSION,
                    kind="Stack",
                    name="web",
                    uid="d1-stack-web",
                )
            ],
        )
    )
    controller.write_desired_candidate_unit(desired / "units/web--preview-app.json", unit, source)

    specifications, dependencies = controller.load_convergence_specifications(
        source,
        "dev",
        desired,
        "a" * 40,
        tmp_path / "projection",
    )
    assert specifications["web--preview-app"].metadata.ownerReferences is not None
    assert specifications["web--preview-app"].metadata.labels is None
    assert dependencies == {"web--preview-app": ()}


def test_stack_rejects_missing_template_during_projection(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
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
        controller.project_stack_resources(source, "dev", "a" * 40, tmp_path / "candidate", source)
