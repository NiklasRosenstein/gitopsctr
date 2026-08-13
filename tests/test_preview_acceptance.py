"""Acceptance-level coverage for Stack lifecycle recovery and ordering."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import ControllerPin, ControllerPinClaim
from tests.stack_deletion_support import deletion_args as _args
from tests.stack_deletion_support import fake_git as _fake_git
from tests.stack_deletion_support import stack_tree as _stack_tree
from tests.stack_support import commit, git, project_repository, write_projected_units, write_stack_source


def _write_project_template(root: Path, environment_name: str, stack: dict[str, object]) -> Path:
    environment = root / "deployment/environments" / environment_name
    environment.mkdir(parents=True, exist_ok=True)
    (environment / "environment.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": environment_name},
                "spec": {},
            }
        )
    )
    (environment / "stacks").mkdir(exist_ok=True)
    stack_name = cast(dict[str, object], stack["metadata"])["name"]
    (environment / f"stacks/{stack_name}.json").write_text(json.dumps(stack))
    templates = root / "deployment/stack-templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "application.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "application"},
                "spec": {
                    "unitTemplates": {
                        "image": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {"source": {"path": "."}},
                        },
                        "deploy": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {
                                    "variables": {
                                        "image": "promoted",
                                    }
                                },
                            },
                        },
                    }
                },
            }
        )
    )
    return environment


def test_stack_source_variants_pin_and_reconcile_from_desired_projection(tmp_path: Path):
    source = tmp_path / "source"
    project_repository(source)
    _write_project_template(
        source,
        "dev",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {
                "template": {"name": "application", "source": {"fromResource": {}}},
                "units": ["image", "deploy"],
            },
        },
    )
    desired = tmp_path / "desired"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, desired, source)
    assert sorted(projection.generated_units) == ["application--deploy", "application--image"]
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((desired / "stacks").glob("application.*"))),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedSource is not None
    assert stack.spec.resolvedSource.fromGit.commit == "a" * 40
    assert stack.spec.resolvedSource.fromGit.resourcePath == "deployment/stack-templates/application.json"
    assert (
        stack.spec.resolvedSource.fromGit.digest
        == hashlib.sha256((source / "deployment/stack-templates/application.json").read_bytes()).hexdigest()
    )
    assert stack.spec.resolvedProjection is not None
    write_projected_units(desired, projection, source)
    (source / "deployment/stack-templates/application.json").unlink()
    specifications, dependencies = controller.load_convergence_specifications(
        source, "dev", desired, "b" * 40, tmp_path / "reconcile-projection"
    )
    assert "application--deploy" in specifications
    assert dependencies["application--deploy"] == ()


def test_from_git_stack_source_does_not_create_catalog_or_read_source_during_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    project_repository(source)
    external = source / "external"
    external.mkdir()
    (external / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "external"},
                "spec": {"effectLease": None},
            }
        )
    )
    _write_project_template(
        external,
        "unused",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "unused"},
            "spec": {"template": "application"},
        },
    )
    dev_stacks = source / "deployment/environments/dev/stacks"
    dev_stacks.mkdir(parents=True, exist_ok=True)
    (dev_stacks / "application.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "application"},
                "spec": {
                    "template": {
                        "name": "application",
                        "source": {"fromGit": {"path": "external", "ref": "main"}},
                    }
                },
            }
        )
    )
    git(source, "init", "-b", "main")
    source_revision = commit(source, "external StackTemplate")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    desired = tmp_path / "desired"
    projection = controller.project_stack_resources(source, "dev", source_revision, desired, source)
    assert not list((desired / "stack-templates").glob("*"))
    assert projection.generated_units
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((desired / "stacks").glob("application.*"))),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedSource is not None
    assert stack.spec.resolvedSource.fromGit.ref == "refs/heads/main"
    assert stack.spec.resolvedSource.fromGit.resourcePath == "deployment/stack-templates/application.json"
    write_projected_units(desired, projection, source)
    shutil.rmtree(external)
    specifications, _ = controller.load_convergence_specifications(
        source, "dev", desired, "d" * 40, tmp_path / "reconcile-projection"
    )
    assert "application--image" in specifications


def test_from_promotion_loads_exact_source_and_projects_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    project_repository(source)
    _write_project_template(
        source,
        "dev",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {"template": "application"},
        },
    )
    git(source, "init", "-b", "main")
    source_revision = commit(source, "template A and dev Stack")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    dev_desired = tmp_path / "dev-desired"
    dev_projection = controller.project_stack_resources(source, "dev", source_revision, dev_desired, source)
    write_projected_units(dev_desired, dev_projection, source)
    _write_project_template(
        source,
        "staging",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "staging"},
            "spec": {
                "template": {
                    "name": "application",
                    "source": {"fromPromotion": {"stack": "application"}},
                },
                "units": ["deploy"],
            },
        },
    )
    staging = tmp_path / "staging"
    promotion = controller.PromotionContext(
        source_environment="dev",
        desired_ref="deploy/dev",
        desired_revision="d" * 40,
        observed_ref="observed/dev",
        observed_revision=None,
        specification_revision=source_revision,
        desired_root=dev_desired,
    )
    projection = controller.project_stack_resources(
        source, "staging", source_revision, staging, source, promotion=promotion
    )
    assert sorted(projection.generated_units) == ["staging--deploy"]
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((staging / "stacks").glob("staging.*"))),
        profile="desired",
        expected_name="staging",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedSource is not None
    assert stack.spec.resolvedSource.fromGit.commit == source_revision
    dev_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((dev_desired / "stacks").glob("application.*"))),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(dev_stack.spec, controller.DesiredStackSpec)
    assert dev_stack.spec.resolvedSource == stack.spec.resolvedSource


def test_source_tracked_stack_cleanup_is_durable_across_restart(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    initial = tmp_path / "initial"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, initial, source)
    initial_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((initial / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )

    write_projected_units(initial, projection, source)
    controller.load_desired_resource_graph(initial)

    (environment / "stacks/web.json").unlink()
    observed = tmp_path / "observed"
    observed.mkdir()
    candidate = tmp_path / "candidate"
    first = controller.build_desired_candidate(
        "dev",
        source,
        "b" * 40,
        initial,
        observed,
        None,
        candidate,
        verbose=False,
    )

    assert first.blocked == {}
    assert initial_stack.metadata.uid is not None
    deleting_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((candidate / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    deleting_unit = controller.load_desired_unit(candidate / "units/web--preview-app.json", "web--preview-app")
    assert deleting_stack.metadata.deletion is not None
    assert deleting_stack.metadata.deletion.generation == 1
    assert deleting_stack.metadata.uid == initial_stack.metadata.uid
    assert deleting_unit.metadata.deletion is not None
    assert deleting_unit.metadata.ownerReferences is not None
    assert deleting_unit.metadata.ownerReferences[0].uid == initial_stack.metadata.uid

    restarted = tmp_path / "restarted"
    second = controller.build_desired_candidate(
        "dev",
        source,
        "c" * 40,
        candidate,
        observed,
        None,
        restarted,
        verbose=False,
    )

    assert second.blocked == {}
    second_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(next((restarted / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )
    assert second_stack.metadata.deletion == deleting_stack.metadata.deletion
    retained_unit = controller.load_desired_unit(restarted / "units/web--preview-app.json", "web--preview-app")
    assert retained_unit.metadata.uid == "d1-web--preview-app"
    assert retained_unit.metadata.ownerReferences is not None
    assert retained_unit.metadata.ownerReferences[0].uid == initial_stack.metadata.uid
    assert controller.load_desired_resource_graph(restarted)


def test_two_stacks_from_one_template_have_independent_generated_units(tmp_path: Path):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    (environment / "stacks/api.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "api"},
                "spec": {"template": "preview", "parameters": {"source-path": "."}},
            }
        )
    )

    candidate = tmp_path / "candidate"
    projection = controller.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    assert sorted(projection.generated_units) == ["api--preview-app", "web--preview-app"]
    assert projection.owners["api--preview-app"].name == "api"
    assert projection.owners["web--preview-app"].name == "web"
    write_projected_units(candidate, projection, source)

    graph = controller.load_desired_resource_graph(candidate)
    assert ("unit.gitopsctr.io/v1", "Terraform", "api--preview-app") in graph
    assert ("unit.gitopsctr.io/v1", "Terraform", "web--preview-app") in graph


def test_direct_stack_finalization_retries_after_injected_publication_failure(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    stack_uid, unit_name = _stack_tree(current)
    published: list[Path] = []
    released: list[tuple[str, str]] = []
    deleted_claims: list[tuple[str, str]] = []
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    claim = ControllerPinClaim(
        environment="dev",
        stack_name="preview",
        uid=stack_uid,
        pin_name=pin.name,
        pin_revision=pin.revision,
        target_ref="deploy/dev",
        target_revision="c" * 40,
        candidate_ref="candidate/dev",
        candidate_revision=None,
        state="active",
        revision="e" * 40,
    )
    store = SimpleNamespace(
        create_controller_pin=lambda _name, _revision: pin,
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
        read_controller_pin_claim=lambda _name: claim,
        delete_controller_pin_claim=lambda name, revision: deleted_claims.append((name, revision)) or True,
    )

    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(controller, "state_store", lambda: store)
    monkeypatch.setattr(controller, "git", _fake_git)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment: str, candidate: Path, *_args: object, **_kwargs: object):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_delete_resource(_args()) is None

    requested = published[0]
    retryable = tmp_path / "retryable"
    shutil.copytree(requested, retryable)
    (retryable / "units" / f"{unit_name}.json").unlink()

    monkeypatch.setattr(
        controller, "materialize_revision", lambda _revision, output: shutil.copytree(retryable, output)
    )
    monkeypatch.setattr(
        controller,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("injected publication failure")),
    )
    with pytest.raises(OperationError, match="injected publication failure"):
        controller.command_finalize(_args())

    assert (
        controller.RESOURCE_CATALOG.parse_stack(
            controller.RESOURCE_CATALOG.load_document(retryable / "stacks/preview.json"),
            profile="desired",
            expected_name="preview",
        ).metadata.uid
        == stack_uid
    )
    assert released == []

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    assert controller.command_finalize(_args()) is True
    assert not list((published[-1] / "stacks").glob("preview.*"))
    assert released == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]
    assert deleted_claims == [("stacks/dev/preview/d1-stack-direct", "e" * 40)]


def test_dependencies_cli_preserves_explicit_stack_order_after_restart(tmp_path: Path, monkeypatch, capsys):
    source = tmp_path / "source"
    environment = project_repository(source)
    write_stack_source(environment)
    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["unitTemplates"]["preview-db"] = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "spec": {"source": {"path": "."}},
    }
    template["spec"]["unitTemplates"]["preview-app"]["dependsOn"] = ["preview-db"]
    template_path.write_text(json.dumps(template))

    source_revision = "a" * 40
    monkeypatch.setattr(
        controller,
        "git",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=source_revision + "\n"),
    )
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(source, output))
    args = controller.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--unit",
            "web--preview-app",
            "--json",
        ]
    )

    args.handler(args)
    first = json.loads(capsys.readouterr().out)
    args.handler(args)
    restarted = json.loads(capsys.readouterr().out)

    assert first == restarted
    assert [unit["name"] for unit in first["units"]] == ["web--preview-db", "web--preview-app"]
    assert first["units"][-1]["dependencies"] == ["web--preview-db"]
