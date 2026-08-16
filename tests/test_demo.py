import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from demo.docker import run as docker_demo
from demo.k8s import run as k8s_demo
from demo.utils import RefHeads
from gitopsctr import controller
from tests.stack_support import commit, write_stack_source
from tests.test_apply import _repository


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
            "deployment/stack-templates/application.yaml",
            "--file",
            "deployment/environments/dev/stacks",
            "--source-revision",
            "HEAD",
            "--yes",
        ),
        ("get", "all", "--environment", "dev"),
    ]


def test_k8s_converge_prints_aggregate_inventory_after_success(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run_controller(_provider: str, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "converged\n", "")

    monkeypatch.setattr(k8s_demo, "run_controller", fake_run_controller)

    k8s_demo.converge("kind", "dev", "argocd", remote="git://demo")

    assert calls == [
        (
            ("converge", "--environment", "dev", "--yes"),
            {
                "delivery": "argocd",
                "preview": False,
                "remote": "git://demo",
                "capture": True,
                "check": False,
            },
        ),
        (
            ("get", "all", "--environment", "dev"),
            {
                "delivery": "argocd",
                "preview": False,
                "remote": "git://demo",
            },
        ),
    ]


def test_fresh_stack_demo_command_selects_template_with_stack(tmp_path, monkeypatch):
    source, _store, _initial_revision = _repository(tmp_path, monkeypatch)
    environment = source / "deployment/environments/dev"
    write_stack_source(environment, stack_name="application")
    revision = commit(source, "add demo StackTemplate and Stack")
    stack_path = environment / "stacks/application.json"
    template_path = source / "deployment/stack-templates/preview.json"

    def parse(*files: Path):
        arguments = [
            "apply",
            "--environment",
            "dev",
            "--source-revision",
            revision,
            "--desired-ref",
            "deploy/dev",
            "--observed-ref",
            "observed/dev",
        ]
        for path in files:
            arguments.extend(("--file", str(path)))
        return controller.build_parser().parse_args(arguments)

    with pytest.raises(controller.OperationError, match="missing desired StackTemplate"):
        controller.command_apply(parse(stack_path))
    published = controller.command_apply(parse(template_path, stack_path))
    assert published is not None


def test_docker_acceptance_uses_automatic_deletion_convergence(monkeypatch):
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
        "remove_and_converge_partitioned_stack",
        lambda: events.append(("deletion-converge",)),
    )

    docker_demo.acceptance(5001, 18081)

    assert events == [
        ("clean", "localhost:5001"),
        ("converge", 5001, 18081, {}),
        ("converge", 5001, 18081, {"expect_clean": True}),
        ("deletion-converge",),
        ("clean", "localhost:5001"),
    ]


def test_docker_acceptance_always_cleans_after_failed_invariant(monkeypatch):
    cleaned: list[str] = []
    heads = iter((RefHeads("desired-1", "observed"), RefHeads("desired-2", "observed")))
    monkeypatch.setattr(docker_demo, "clean", cleaned.append)
    monkeypatch.setattr(docker_demo, "converge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(docker_demo, "deployment_heads", lambda: next(heads))
    with pytest.raises(RuntimeError, match="moved desired or observed refs"):
        docker_demo.acceptance(5001, 18081)

    assert cleaned == ["localhost:5001", "localhost:5001"]


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
            "--file",
            "deployment/stack-templates/application.yaml",
            "--partition",
            "application",
        )
    ]


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
    assert events[0][2]["files"] == (
        "deployment/stack-templates/application.yaml",
        "deployment/environments/preview/stacks/preview.yaml",
    )


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


@pytest.mark.parametrize(
    ("demo_root", "replacements", "expected_kind"),
    [
        (
            docker_demo.TEMPLATE,
            {
                "__APP_PORT__": "18081",
                "__DOCKER_PLATFORM__": "linux/arm64",
                "__REGISTRY__": "localhost:5001",
                "__TERRAFORM_STATE__": "/tmp/gitopsctr-demo.tfstate",
            },
            "Terraform",
        ),
        (
            k8s_demo.TEMPLATE,
            {"__CLUSTER_NAME__": "gitopsctr-k8s-dev", "__KUBE_CONTEXT__": "kind-gitopsctr-k8s-dev"},
            "KubernetesManifests",
        ),
    ],
)
def test_demo_stack_templates_project_real_driver_contracts(
    tmp_path: Path,
    demo_root: Path,
    replacements: dict[str, str],
    expected_kind: str,
):
    source = tmp_path / demo_root.name
    shutil.copytree(demo_root, source)
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        content = path.read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)

    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    current.mkdir()
    observed.mkdir()
    result = controller.build_desired_candidate(
        "dev",
        source,
        "a" * 40,
        current,
        observed,
        None,
        candidate,
        verbose=False,
    )
    resources = controller.load_desired_resource_graph(candidate)
    stack = resources[("gitopsctr.io/v1", "Stack", "application")]
    assert stack.spec.structuralProjection.units["deploy"].kind == expected_kind  # type: ignore[union-attr]
    assert stack.spec.structuralProjection.units["image"].kind == "OciImages"  # type: ignore[union-attr]
    assert result.blocked == {"application--deploy": "receipt does not exist: application--image"}
    assert stack.spec.activeProjection is not None  # type: ignore[union-attr]
    assert set(stack.spec.activeProjection.units) == {"image"}  # type: ignore[union-attr]
