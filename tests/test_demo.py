import shutil
import subprocess
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
import yaml

from demo.docker import run as docker_demo
from demo.k8s import run as k8s_demo
from demo.utils import RefHeads
from gitopsctr import controller
from tests.stack_support import commit, git


def test_k8s_controller_preserves_terminal_color_when_capturing(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(k8s_demo, "color_enabled", lambda _stream: True)
    monkeypatch.setattr(k8s_demo, "repository", lambda *_args, **_kwargs: SimpleNamespace(worktree=tmp_path))

    def fake_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(k8s_demo, "run", fake_run)

    k8s_demo.run_controller("kind", "converge", capture=True)

    environment = captured["env"]
    assert isinstance(environment, Mapping)
    assert environment["FORCE_COLOR"] == "1"


def test_docker_demo_projects_only_stack_owned_units(tmp_path, monkeypatch):
    worktree = tmp_path / "repository"
    state = tmp_path / "terraform.tfstate"
    shutil.copytree(docker_demo.TEMPLATE, worktree)
    monkeypatch.setattr(docker_demo, "WORKTREE", worktree)
    monkeypatch.setattr(docker_demo, "TERRAFORM_STATE", state)
    monkeypatch.setattr(docker_demo, "docker_platform", lambda: "linux/arm64")

    docker_demo.configure_template("localhost:5001", 18081)
    assert controller.load_environment_specifications(worktree, "dev") == {}

    projection = controller.project_stack_resources(
        worktree,
        "dev",
        "a" * 40,
        tmp_path / "candidate",
        worktree,
    )

    assert set(projection.generated_units) == {"application--image", "application--deploy"}
    image = projection.generated_units["application--image"]
    deploy = projection.generated_units["application--deploy"]
    assert image.spec.build.platform == "linux/arm64"
    assert image.spec.publish.targets["application"].repository == "localhost:5001/gitopsctr-demo/app"
    deploy_spec = deploy.driver.unit_contract.dump(deploy.spec)
    assert deploy_spec["terraform"]["backend"]["path"] == str(state)
    assert deploy_spec["terraform"]["variables"]["container_name"] == "gitopsctr-demo-app"
    assert deploy_spec["terraform"]["variables"]["host_port"] == 18081
    assert deploy_spec["terraform"]["variables"]["image"]["fromArtifact"]["unit"] == "application--image"


def test_docker_converge_reapplies_the_authoritative_partition(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(docker_demo, "require_commands", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(docker_demo, "prepare_repository", lambda *_args: None)
    monkeypatch.setattr(docker_demo, "ensure_registry", lambda *_args: None)
    monkeypatch.setattr(docker_demo, "verify_application", lambda *_args: "ok")
    monkeypatch.setattr(
        docker_demo,
        "_run_controller",
        lambda *args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    docker_demo.converge(5001, 18081)

    assert calls == [
        (
            "converge",
            "--environment",
            "dev",
            "--partition",
            "application",
            "--file",
            "deployment/environments/dev/stacks",
            "--source-revision",
            "HEAD",
            "--yes",
        )
    ]


def test_docker_acceptance_proves_clean_convergence_then_finalizes(monkeypatch):
    events: list[object] = []
    heads = iter((RefHeads("desired", "observed"), RefHeads("desired", "observed")))
    monkeypatch.setattr(docker_demo, "clean", lambda registry: events.append(("clean", registry)))
    monkeypatch.setattr(
        docker_demo,
        "converge",
        lambda registry_port, app_port, **kwargs: events.append(("converge", registry_port, app_port, kwargs)),
    )
    monkeypatch.setattr(docker_demo, "deployment_heads", lambda: next(heads))
    monkeypatch.setattr(
        docker_demo,
        "remove_and_finalize_partitioned_stack",
        lambda: events.append(("finalize",)),
    )

    docker_demo.acceptance(5001, 18081)

    assert events == [
        ("clean", "localhost:5001"),
        ("converge", 5001, 18081, {}),
        ("converge", 5001, 18081, {"expect_clean": True}),
        ("finalize",),
        ("clean", "localhost:5001"),
    ]


def test_docker_acceptance_always_cleans_after_failed_invariant(monkeypatch):
    cleaned: list[str] = []
    heads = iter((RefHeads("desired-1", "observed"), RefHeads("desired-2", "observed")))
    monkeypatch.setattr(docker_demo, "clean", cleaned.append)
    monkeypatch.setattr(docker_demo, "converge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(docker_demo, "deployment_heads", lambda: next(heads))
    monkeypatch.setattr(docker_demo, "remove_and_finalize_partitioned_stack", lambda: None)

    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        docker_demo.acceptance(5001, 18081)

    assert cleaned == ["localhost:5001", "localhost:5001"]


@pytest.mark.parametrize("provider", ("kind", "minikube"))
def test_k8s_demo_projects_partitioned_stack_for_provider(tmp_path, monkeypatch, provider):
    worktree = tmp_path / provider / "repository"
    shutil.copytree(k8s_demo.TEMPLATE, worktree)
    monkeypatch.setattr(k8s_demo, "docker_platform", lambda: "linux/amd64")

    k8s_demo.configure_template(provider, worktree)
    assert controller.load_environment_specifications(worktree, "dev") == {}
    projection = controller.project_stack_resources(
        worktree,
        "dev",
        "a" * 40,
        tmp_path / provider / "candidate",
        worktree,
    )

    assert set(projection.generated_units) == {"application--image", "application--deploy"}
    image = projection.generated_units["application--image"]
    deploy = projection.generated_units["application--deploy"]
    target = image.spec.publish.targets["application"]
    assert target.type == provider
    assert getattr(target, "cluster" if provider == "kind" else "profile") == k8s_demo.cluster_name("direct")
    assert deploy.spec.materialize.values._serialize()["image"]["fromArtifact"]["unit"] == "application--image"
    assert deploy.driver.unit_contract.dump(deploy.spec)["delivery"] == {
        "mode": "direct",
        "kubeContext": k8s_demo.kube_context(provider),
        "prune": False,
        "wait": [
            {
                "resource": "deployment/gitopsctr-k8s-dev",
                "namespace": "default",
                "condition": "Available",
                "timeoutSeconds": 120,
            }
        ],
    }


def test_k8s_staging_stack_uses_promoted_template_pin_with_target_parameters(tmp_path, monkeypatch):
    worktree = tmp_path / "repository"
    shutil.copytree(k8s_demo.TEMPLATE, worktree)
    monkeypatch.setattr(k8s_demo, "docker_platform", lambda: "linux/amd64")
    k8s_demo.configure_template("kind", worktree)

    stack = yaml.safe_load((worktree / "deployment/environments/staging/stacks/application.yaml").read_text())

    assert stack["spec"]["template"] == {
        "name": "application",
        "source": {"fromPromotion": {"stack": "application"}},
    }
    assert stack["spec"]["units"] == ["deploy"]
    assert stack["spec"]["artifactImports"] == [
        {
            "unit": "image",
            "name": "containers",
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "fromPromotion": {"stack": "application"},
        }
    ]

    git(worktree, "init", "-b", "main")
    source_revision = commit(worktree, "configured demo")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", worktree)
    controller._state_store.cache_clear()
    dev_desired = tmp_path / "dev-desired"
    controller.project_stack_resources(worktree, "dev", source_revision, dev_desired, worktree)
    staging = controller.project_stack_resources(
        worktree,
        "staging",
        source_revision,
        tmp_path / "staging-desired",
        worktree,
        promotion=controller.PromotionContext(
            source_environment="dev",
            desired_ref="gitopsctr/desired/dev",
            desired_revision="b" * 40,
            observed_ref="gitopsctr/observed/dev",
            observed_revision="c" * 40,
            specification_revision=source_revision,
            desired_root=dev_desired,
        ),
    )

    assert set(staging.generated_units) == {"application--deploy"}
    deploy = staging.generated_units["application--deploy"]
    assert deploy.spec.materialize.releaseName == "gitopsctr-k8s-staging"
    assert deploy.spec.materialize.values._serialize()["message"] == "promoted from dev to staging"


def test_k8s_promotion_passes_explicit_target_input(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(k8s_demo, "converge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(k8s_demo, "verify_workload", lambda *_args, **_kwargs: "image@sha256:1")
    monkeypatch.setattr(k8s_demo, "deployment_heads", lambda *_args, **_kwargs: RefHeads("desired", "observed"))
    monkeypatch.setattr(
        k8s_demo,
        "run_controller",
        lambda _provider, *args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    k8s_demo.run_promotion_story("kind", "direct")

    assert calls == [
        (
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--file",
            "deployment/environments/staging/stacks/application.yaml",
            "--partition",
            "application",
        )
    ]


@pytest.mark.parametrize("provider", ("kind", "minikube"))
def test_argocd_delivery_uses_parameterized_stack_observer(tmp_path, monkeypatch, provider):
    worktree = tmp_path / provider / "repository"
    shutil.copytree(k8s_demo.TEMPLATE, worktree)
    monkeypatch.setattr(k8s_demo, "docker_platform", lambda: "linux/amd64")

    k8s_demo.configure_template(provider, worktree, "argocd")
    projection = controller.project_stack_resources(
        worktree,
        "dev",
        "a" * 40,
        tmp_path / provider / "candidate",
        worktree,
    )
    deploy = projection.generated_units["application--deploy"]

    assert deploy.driver.unit_contract.dump(deploy.spec)["delivery"] == {
        "mode": "external",
        "observer": {
            "type": "argocd",
            "access": "kubernetes",
            "application": "gitopsctr-k8s-dev",
            "applicationNamespace": k8s_demo.ARGO_NAMESPACE,
            "kubeContext": k8s_demo.kube_context(provider, "argocd"),
            "timeoutSeconds": 600,
        },
    }
    application = k8s_demo.argo_application_document("dev")
    assert application["spec"]["source"] == {
        "repoURL": f"git://{k8s_demo.ARGO_GIT_SERVICE}.{k8s_demo.ARGO_NAMESPACE}.svc.cluster.local:9418/origin.git",
        "targetRevision": "gitopsctr/desired/dev",
        "path": "materialized/application--deploy",
    }


def test_preview_application_targets_unpartitioned_stack_projection():
    application = k8s_demo.argo_application_document("preview", preview=True)
    assert application["spec"]["source"]["targetRevision"] == "gitopsctr/desired/preview"
    assert application["spec"]["source"]["path"] == "materialized/preview--deploy"


def test_existing_preview_at_current_source_only_converges(monkeypatch, tmp_path):
    events: list[object] = []
    monkeypatch.setattr(k8s_demo, "repository", lambda *_args, **_kwargs: SimpleNamespace(worktree=tmp_path))
    monkeypatch.setattr(k8s_demo, "source_revision", lambda _worktree: "a" * 40)
    monkeypatch.setattr(
        k8s_demo,
        "converge",
        lambda *args, **kwargs: events.append(("converge", args, kwargs)),
    )
    monkeypatch.setattr(k8s_demo, "verify_workload", lambda *_args, **_kwargs: "application--image:r1")
    monkeypatch.setattr(
        k8s_demo,
        "deployment_heads",
        lambda *_args, **_kwargs: RefHeads("desired", "observed"),
    )

    heads, image = k8s_demo.run_preview_story("kind", "direct")

    assert heads == RefHeads("desired", "observed")
    assert image == "application--image:r1"
    assert len(events) == 1
    assert events[0][0] == "converge"
    assert events[0][2]["files"] == ("deployment/environments/preview/stacks/preview.yaml",)


def test_provider_defaults_to_kind_and_accepts_minikube(monkeypatch):
    monkeypatch.delenv("GITOPSCTR_K8S_PROVIDER", raising=False)
    assert k8s_demo.provider_from_environment() == "kind"

    monkeypatch.setenv("GITOPSCTR_K8S_PROVIDER", "minikube")
    assert k8s_demo.provider_from_environment() == "minikube"

    monkeypatch.setenv("GITOPSCTR_K8S_PROVIDER", "docker-desktop")
    with pytest.raises(RuntimeError, match="must be 'kind' or 'minikube'"):
        k8s_demo.provider_from_environment()


def test_refresh_argo_application_requests_environment_application_refresh(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(k8s_demo, "run", fake_run)

    k8s_demo.refresh_argo_application("kind", "staging")

    assert calls[0][0][0:8] == (
        "kubectl",
        "--context",
        k8s_demo.kube_context("kind", "argocd"),
        "--namespace",
        k8s_demo.ARGO_NAMESPACE,
        "patch",
        "application.argoproj.io",
        "gitopsctr-k8s-staging",
    )
    assert calls[0][1] == {"check": False}
