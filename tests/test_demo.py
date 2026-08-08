import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from gitopsctr import cli

DEMO_ROOT = Path(__file__).parents[1] / "demo"
SPEC = importlib.util.spec_from_file_location("gitopsctr_demo_runner", DEMO_ROOT / "run.py")
assert SPEC is not None and SPEC.loader is not None
demo = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = demo
SPEC.loader.exec_module(demo)

KUBERNETES_SPEC = importlib.util.spec_from_file_location(
    "gitopsctr_kubernetes_demo_runner", DEMO_ROOT / "kubernetes/run.py"
)
assert KUBERNETES_SPEC is not None and KUBERNETES_SPEC.loader is not None
kubernetes_demo = importlib.util.module_from_spec(KUBERNETES_SPEC)
sys.modules[KUBERNETES_SPEC.name] = kubernetes_demo
KUBERNETES_SPEC.loader.exec_module(kubernetes_demo)


def test_demo_repository_exercises_observation_driven_convergence():
    specifications = cli.load_environment_specifications(demo.TEMPLATE, "dev")

    targets, scope = cli.convergence_scope(specifications, ["demo-service"])

    assert targets == ["demo-service"]
    assert scope == ["demo-image", "demo-service"]
    assert cli.convergence_order(specifications, scope) == ["demo-image", "demo-service"]


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
    assert image["spec"]["publish"]["repositories"]["application"] == "localhost:5001/gitopsctr-demo/app"
    assert service["spec"]["terraform"]["backend"]["path"] == str(state)
    assert service["spec"]["terraform"]["variables"]["host_port"] == 18081


def test_demo_acceptance_requires_stable_refs_and_always_cleans(monkeypatch):
    events: list[object] = []
    heads = iter((("desired", "observed"), ("desired", "observed")))
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
    heads = iter((("desired-1", "observed"), ("desired-2", "observed")))
    monkeypatch.setattr(demo, "clean", cleaned.append)
    monkeypatch.setattr(demo, "converge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(demo, "deployment_heads", lambda: next(heads))

    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        demo.acceptance(5001, 18081)

    assert cleaned == ["localhost:5001", "localhost:5001"]


def test_kubernetes_demo_is_a_real_helm_direct_delivery_unit():
    specifications = cli.load_environment_specifications(kubernetes_demo.TEMPLATE, "dev")
    specification = specifications["web"]

    assert specification["source"]["inputs"] == ["**/*"]
    assert specification["materialize"]["type"] == "helm"
    assert specification["delivery"] == {
        "mode": "direct",
        "kubeContext": kubernetes_demo.KUBE_CONTEXT,
        "prune": False,
        "wait": [],
    }


def test_kubernetes_acceptance_requires_stable_refs_and_always_cleans(monkeypatch):
    events: list[object] = []
    heads = iter((("desired", "observed"), ("desired", "observed")))
    monkeypatch.setattr(kubernetes_demo, "clean", lambda: events.append("clean"))
    monkeypatch.setattr(kubernetes_demo, "start", lambda: events.append("start"))
    monkeypatch.setattr(
        kubernetes_demo,
        "converge",
        lambda **kwargs: events.append(("converge", kwargs)),
    )
    monkeypatch.setattr(kubernetes_demo, "deployment_heads", lambda: next(heads))

    kubernetes_demo.acceptance()

    assert events == ["clean", "start", ("converge", {"expect_clean": True}), "clean"]


def test_kubernetes_acceptance_cleans_after_a_failed_invariant(monkeypatch):
    events: list[object] = []
    heads = iter((("desired-1", "observed"), ("desired-2", "observed")))
    monkeypatch.setattr(kubernetes_demo, "clean", lambda: events.append("clean"))
    monkeypatch.setattr(kubernetes_demo, "start", lambda: None)
    monkeypatch.setattr(kubernetes_demo, "converge", lambda **_kwargs: None)
    monkeypatch.setattr(kubernetes_demo, "deployment_heads", lambda: next(heads))

    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        kubernetes_demo.acceptance()

    assert events == ["clean", "clean"]
