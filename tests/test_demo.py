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


def test_demo_acceptance_requires_stable_refs_and_always_cleans(monkeypatch):
    events: list[object] = []
    heads = iter((RefHeads("desired", "observed"), RefHeads("desired", "observed")))
    monkeypatch.setattr(demo, "clean", lambda registry: events.append(("clean", registry)))
    monkeypatch.setattr(
        demo,
        "converge",
        lambda registry_port, app_port, **kwargs: events.append(("converge", registry_port, app_port, kwargs)),
    )
    monkeypatch.setattr(demo, "deployment_heads", lambda: next(heads))

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
