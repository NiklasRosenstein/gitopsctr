import shutil
import subprocess
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
import yaml

from demo.docker import run as demo
from demo.kubernetes import run as kubernetes_demo
from demo.utils import RefHeads
from gitopsctr import cli


def test_kubernetes_controller_preserves_terminal_color_when_capturing(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(kubernetes_demo, "color_enabled", lambda _stream: True)
    monkeypatch.setattr(kubernetes_demo, "repository", lambda _provider: SimpleNamespace(worktree=tmp_path))

    def fake_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(kubernetes_demo, "run", fake_run)

    kubernetes_demo.controller("kind", "converge", capture=True)

    environment = captured["env"]
    assert isinstance(environment, Mapping)
    assert environment["FORCE_COLOR"] == "1"


def test_demo_repository_exercises_observation_driven_convergence():
    specifications = cli.load_environment_specifications(demo.TEMPLATE, "dev")

    selection = cli.convergence_scope(specifications, ["demo-service"])
    targets, scope = selection.targets, selection.scope

    assert targets == ("demo-service",)
    assert scope == ("demo-image", "demo-service")
    assert cli.convergence_order(specifications, scope) == ("demo-image", "demo-service")


def test_demo_runner_materializes_local_runtime_configuration(tmp_path, monkeypatch):
    worktree = tmp_path / "repository"
    state = tmp_path / "terraform.tfstate"
    shutil.copytree(demo.TEMPLATE, worktree)
    monkeypatch.setattr(demo, "WORKTREE", worktree)
    monkeypatch.setattr(demo, "TERRAFORM_STATE", state)
    monkeypatch.setattr(demo, "docker_platform", lambda: "linux/arm64")

    demo.configure_template("localhost:5001", 18081)

    image = yaml.safe_load((worktree / "deployment/environments/dev/units/demo-image.yaml").read_text())
    service = yaml.safe_load((worktree / "deployment/environments/dev/units/demo-service.yaml").read_text())
    assert image["spec"]["build"]["platform"] == "linux/arm64"
    assert image["spec"]["publish"]["targets"]["application"] == {
        "type": "registry",
        "repository": "localhost:5001/gitopsctr-demo/app",
    }
    assert service["spec"]["terraform"]["backend"]["path"] == str(state)
    assert service["spec"]["terraform"]["variables"]["host_port"] == 18081


def test_demo_stack_source_projects_parameterized_terraform_unit(tmp_path, monkeypatch):
    worktree = tmp_path / "repository"
    stack_state = tmp_path / "stack-terraform.tfstate"
    shutil.copytree(demo.TEMPLATE, worktree)
    monkeypatch.setattr(demo, "WORKTREE", worktree)
    monkeypatch.setattr(demo, "STACK_TERRAFORM_STATE", stack_state)
    monkeypatch.setattr(demo, "_commit_source", lambda _message: "a" * 40)
    demo.add_stack_source(18082)

    projection = cli.project_stack_resources(
        worktree,
        "dev",
        "a" * 40,
        tmp_path / "candidate",
        worktree,
    )

    generated_name = "preview--demo-service"
    assert tuple(projection.generated_units) == (generated_name,)
    assert projection.dependencies == {generated_name: ()}
    generated = projection.generated_units[generated_name]
    assert generated.driver_name == "terraform"
    specification = generated.driver.unit_contract.dump(generated.spec)
    assert specification["terraform"]["backend"]["path"] == str(stack_state)
    assert specification["terraform"]["variables"]["container_name"] == "gitopsctr-demo-stack-app"
    assert specification["terraform"]["variables"]["host_port"] == 18082
    assert specification["terraform"]["variables"]["image"]["fromArtifact"]["unit"] == "demo-image"


def _planned_stack_teardown_commands(stack_name, stack_uid, owned_units):
    """Describe the smallest CLI sequence for UID-fenced Stack cleanup."""

    commands = [("advance-desired", "--environment", "dev", "--source-revision", "HEAD")]
    for unit_name, unit_uid in reversed(owned_units):
        commands.append(
            (
                "finalize",
                "--environment",
                "dev",
                "--unit",
                unit_name,
                "--uid",
                unit_uid,
                "--deletion-generation",
                "1",
            )
        )
    commands.append(
        (
            "finalize-stack",
            "--environment",
            "dev",
            "--stack",
            stack_name,
            "--uid",
            stack_uid,
            "--deletion-generation",
            "1",
        )
    )
    return commands


def test_demo_stack_cleanup_commands_match_current_cli_contracts():
    commands = _planned_stack_teardown_commands(
        "demo-preview",
        "stack-uid",
        (("demo-preview--database", "database-uid"), ("demo-preview--service", "service-uid")),
    )
    parser = cli.build_parser()
    parsed = [parser.parse_args(("--repository", "/tmp/demo-repository", *command)) for command in commands]

    assert [args.command for args in parsed] == [
        "advance-desired",
        "finalize",
        "finalize",
        "finalize-stack",
    ]
    assert [args.unit for args in parsed[1:3]] == ["demo-preview--service", "demo-preview--database"]
    assert all(args.deletion_generation == 1 for args in parsed[1:])
    assert parsed[-1].stack == "demo-preview"
    assert parsed[-1].uid == "stack-uid"


def test_demo_acceptance_delegates_stack_cleanup_after_clean_direct_convergence(monkeypatch):
    events: list[object] = []
    heads = iter(
        (
            RefHeads("desired", "observed"),
            RefHeads("desired", "observed"),
            RefHeads("desired", "observed"),
        )
    )
    monkeypatch.setattr(demo, "clean", lambda registry: events.append(("clean", registry)))
    monkeypatch.setattr(
        demo,
        "converge",
        lambda registry_port, app_port, **kwargs: events.append(("converge", registry_port, app_port, kwargs)),
    )
    monkeypatch.setattr(demo, "deployment_heads", lambda: next(heads))
    monkeypatch.setattr(
        demo,
        "stack_acceptance",
        lambda registry_port, app_port: events.append(("stack_acceptance", registry_port, app_port)),
    )

    demo.acceptance(5001, 18081)

    assert events == [
        ("clean", "localhost:5001"),
        ("converge", 5001, 18081, {}),
        ("converge", 5001, 18081, {"expect_clean": True}),
        ("stack_acceptance", 5001, 18081),
        ("clean", "localhost:5001"),
    ]


def test_demo_acceptance_requires_stable_refs_and_always_cleans(monkeypatch):
    events: list[object] = []
    heads = iter(
        (
            RefHeads("desired", "observed"),
            RefHeads("desired", "observed"),
            RefHeads("desired", "observed"),
        )
    )
    monkeypatch.setattr(demo, "clean", lambda registry: events.append(("clean", registry)))
    monkeypatch.setattr(
        demo,
        "converge",
        lambda registry_port, app_port, **kwargs: events.append(("converge", registry_port, app_port, kwargs)),
    )
    monkeypatch.setattr(demo, "deployment_heads", lambda: next(heads))
    monkeypatch.setattr(demo, "stack_acceptance", lambda *_args: None)

    demo.acceptance(5001, 18081)

    assert events == [
        ("clean", "localhost:5001"),
        ("converge", 5001, 18081, {}),
        ("converge", 5001, 18081, {"expect_clean": True}),
        ("clean", "localhost:5001"),
    ]


def test_demo_acceptance_cleans_after_a_failed_invariant(monkeypatch):
    cleaned: list[str] = []
    heads = iter((RefHeads("desired-1", "observed"), RefHeads("desired-2", "observed")))
    monkeypatch.setattr(demo, "clean", cleaned.append)
    monkeypatch.setattr(demo, "converge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(demo, "deployment_heads", lambda: next(heads))
    monkeypatch.setattr(demo, "stack_acceptance", lambda *_args: None)

    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        demo.acceptance(5001, 18081)

    assert cleaned == ["localhost:5001", "localhost:5001"]


@pytest.mark.parametrize("provider", ("kind", "minikube"))
def test_kubernetes_demo_is_a_real_image_and_helm_delivery(tmp_path, monkeypatch, provider):
    worktree = tmp_path / provider / "repository"
    shutil.copytree(kubernetes_demo.TEMPLATE, worktree)
    monkeypatch.setattr(kubernetes_demo, "docker_platform", lambda: "linux/amd64")
    kubernetes_demo.configure_template(provider, worktree)
    specifications = cli.load_environment_specifications(worktree, "dev")
    specification = specifications["web"]

    assert cli.convergence_order(specifications, ["demo-image", "web"]) == ("demo-image", "web")
    assert specification.spec.source.inputs == ["**/*"]
    assert specification.spec.materialize.type == "helm"
    assert specification.spec.materialize.values._serialize()["image"]["fromArtifact"] == {
        "unit": "demo-image",
        "name": "containers",
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "pointer": "/images/application/uri",
    }
    assert specification.driver.unit_contract.dump(specification.spec)["delivery"] == {
        "mode": "direct",
        "kubeContext": kubernetes_demo.kube_context(provider),
        "prune": False,
        "wait": [
            {
                "resource": "deployment/gitopsctr-kubernetes-demo",
                "namespace": "default",
                "condition": "Available",
                "timeoutSeconds": 120,
            }
        ],
    }
    image_target = specifications["demo-image"].spec.publish.targets["application"]
    assert image_target.type == provider
    assert getattr(image_target, "cluster" if provider == "kind" else "profile") == kubernetes_demo.CLUSTER_NAME


def test_kubernetes_acceptance_requires_stable_refs_and_always_cleans(monkeypatch):
    events: list[object] = []
    heads = iter((RefHeads("desired", "observed"), RefHeads("desired", "observed")))
    monkeypatch.setattr(kubernetes_demo, "clean", lambda provider: events.append(("clean", provider)))
    monkeypatch.setattr(kubernetes_demo, "start", lambda provider: events.append(("start", provider)))
    monkeypatch.setattr(
        kubernetes_demo,
        "converge",
        lambda provider, **kwargs: events.append(("converge", provider, kwargs)),
    )
    monkeypatch.setattr(kubernetes_demo, "deployment_heads", lambda _provider: next(heads))

    kubernetes_demo.acceptance("kind")

    assert events == [
        ("clean", "kind"),
        ("start", "kind"),
        ("converge", "kind", {"expect_clean": True}),
        ("clean", "kind"),
    ]


def test_kubernetes_acceptance_cleans_after_a_failed_invariant(monkeypatch):
    events: list[object] = []
    heads = iter((RefHeads("desired-1", "observed"), RefHeads("desired-2", "observed")))
    monkeypatch.setattr(kubernetes_demo, "clean", lambda provider: events.append(("clean", provider)))
    monkeypatch.setattr(kubernetes_demo, "start", lambda _provider: None)
    monkeypatch.setattr(kubernetes_demo, "converge", lambda _provider, **_kwargs: None)
    monkeypatch.setattr(kubernetes_demo, "deployment_heads", lambda _provider: next(heads))

    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        kubernetes_demo.acceptance("minikube")

    assert events == [("clean", "minikube"), ("clean", "minikube")]


@pytest.mark.parametrize("provider", ("kind", "minikube"))
def test_argocd_demo_uses_the_external_observer_and_materialized_payload(tmp_path, monkeypatch, provider):
    worktree = tmp_path / provider / "repository"
    shutil.copytree(kubernetes_demo.TEMPLATE, worktree)
    monkeypatch.setattr(kubernetes_demo, "docker_platform", lambda: "linux/amd64")

    kubernetes_demo.configure_template(provider, worktree, "argocd")

    specification = cli.load_environment_specifications(worktree, "dev")["web"]
    assert specification.driver.unit_contract.dump(specification.spec)["delivery"] == {
        "mode": "external",
        "observer": {
            "type": "argocd",
            "access": "kubernetes",
            "application": kubernetes_demo.ARGO_APPLICATION,
            "applicationNamespace": kubernetes_demo.ARGO_NAMESPACE,
            "kubeContext": kubernetes_demo.kube_context(provider, "argocd"),
            "timeoutSeconds": 600,
        },
    }
    application = kubernetes_demo.argo_application_document()
    assert application["spec"]["source"] == {
        "repoURL": f"git://{kubernetes_demo.ARGO_GIT_SERVICE}.{kubernetes_demo.ARGO_NAMESPACE}.svc.cluster.local:9418/origin.git",
        "targetRevision": "gitopsctr/desired/dev",
        "path": "materialized/web",
    }
    assert application["spec"]["syncPolicy"] == {"automated": {}}


def test_refresh_argo_application_requests_application_refresh(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(kubernetes_demo, "run", fake_run)

    kubernetes_demo.refresh_argo_application("kind")

    assert calls == [
        (
            (
                "kubectl",
                "--context",
                kubernetes_demo.kube_context("kind", "argocd"),
                "--namespace",
                kubernetes_demo.ARGO_NAMESPACE,
                "patch",
                "application.argoproj.io",
                kubernetes_demo.ARGO_APPLICATION,
                "--type",
                "merge",
                "--patch",
                '{"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}}',
            ),
            {},
        )
    ]
