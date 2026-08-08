"""Run the isolated Helm demo against an explicitly selected local Kubernetes provider."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, cast

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run

Provider = Literal["kind", "minikube"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".kubernetes-demo-state"
CLUSTER_NAME = "gitopsctr-kubernetes-demo"
RESOURCE_NAME = "gitopsctr-kubernetes-demo"
EXPECTED_RESPONSE = "Hello from a gitopsctr-managed Kubernetes container!"


def kube_context(provider: Provider) -> str:
    return f"kind-{CLUSTER_NAME}" if provider == "kind" else CLUSTER_NAME


def repository(provider: Provider) -> DemoRepository:
    provider_state = STATE_ROOT / provider
    return DemoRepository(
        template=TEMPLATE,
        state_root=provider_state,
        worktree=provider_state / "repository",
        remote=provider_state / "origin.git",
        identity=f"gitopsctr Kubernetes {provider} demo",
    )


def configure_template(provider: Provider, worktree: Path) -> None:
    replacements = {
        "__CLUSTER_NAME__": CLUSTER_NAME,
        "__DOCKER_PLATFORM__": docker_platform(),
        "__KUBE_CONTEXT__": kube_context(provider),
    }
    for path in worktree.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text()
        if provider == "minikube":
            content = content.replace("type: kind\n        cluster:", "type: minikube\n        profile:")
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)


def prepare_repository(provider: Provider) -> None:
    demo_repository = repository(provider)
    demo_repository.prepare(lambda: configure_template(provider, demo_repository.worktree))


def clean(provider: Provider) -> None:
    if provider == "kind" and shutil.which("kind") is not None:
        run("kind", "delete", "cluster", "--name", CLUSTER_NAME, check=False)
    elif provider == "minikube" and shutil.which("minikube") is not None:
        run("minikube", "delete", "--profile", CLUSTER_NAME, check=False)
    if shutil.which("docker") is not None:
        remove_docker_images("demo-image:*")
    repository(provider).clean()
    print(f"Kubernetes {provider} demo cluster and state removed.")


def ensure_cluster(provider: Provider) -> None:
    if provider == "kind":
        clusters = run("kind", "get", "clusters", capture=True).stdout.splitlines()
        if CLUSTER_NAME not in clusters:
            run("kind", "create", "cluster", "--name", CLUSTER_NAME, "--wait", "120s")
        return
    status = run("minikube", "status", "--profile", CLUSTER_NAME, check=False, capture=True)
    if status.returncode != 0:
        run("minikube", "start", "--profile", CLUSTER_NAME, "--driver", "docker", "--wait", "all")


def controller(provider: Provider, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    worktree = repository(provider).worktree
    return run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(worktree),
        *args,
        cwd=worktree,
        capture=capture,
    )


def deployment_heads(provider: Provider) -> RefHeads:
    return repository(provider).heads()


def verify_resource(provider: Provider) -> None:
    context = kube_context(provider)
    run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "rollout",
        "status",
        f"deployment/{RESOURCE_NAME}",
        "--timeout=120s",
    )
    image = run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "get",
        "deployment",
        RESOURCE_NAME,
        "--output",
        "jsonpath={.spec.template.spec.containers[0].image}",
        capture=True,
    ).stdout
    if not image.startswith("demo-image:"):
        raise RuntimeError(f"unexpected deployed image: {image!r}")
    response = run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "exec",
        f"deployment/{RESOURCE_NAME}",
        "--",
        "wget",
        "--quiet",
        "--output-document=-",
        "http://127.0.0.1:8080/",
        capture=True,
    ).stdout.strip()
    if response != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected application response: {response!r}")
    print(f"Application check passed: {response}", flush=True)
    controller(provider, "verify", "--environment", "dev", "--unit", "web")


def converge(provider: Provider, *, expect_clean: bool = False) -> None:
    result = controller(
        provider,
        "converge",
        "--environment",
        "dev",
        "--source-revision",
        "HEAD",
        "--yes",
        capture=expect_clean,
    )
    if expect_clean:
        output = result.stdout + result.stderr
        print(output, end="" if output.endswith("\n") else "\n")
        if "no drivers ran; 0 ref movements" not in output:
            raise RuntimeError("second Kubernetes convergence was not clean")
    verify_resource(provider)


def start(provider: Provider) -> None:
    require_commands("docker", "git", "helm", provider, "kubectl")
    ensure_cluster(provider)
    prepare_repository(provider)
    converge(provider)
    print(f"Kubernetes demo is running in context {kube_context(provider)}.")
    print(f"Run 'mise run kubernetes-demo-clean -- {provider}' to remove all effects.")


def acceptance(provider: Provider) -> None:
    clean(provider)
    try:
        start(provider)
        first_heads = deployment_heads(provider)
        converge(provider, expect_clean=True)
        second_heads = deployment_heads(provider)
        if second_heads != first_heads:
            raise RuntimeError("clean Kubernetes convergence moved desired or observed refs")
        print(f"Acceptance passed: deploy/dev={second_heads.desired[:12]} observed/dev={second_heads.observed[:12]}")
    finally:
        clean(provider)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "clean", "acceptance"))
    parser.add_argument("provider", choices=("kind", "minikube"), help="local cluster provider (required)")
    args = parser.parse_args()
    provider = cast(Provider, args.provider)
    try:
        if args.operation == "clean":
            clean(provider)
        elif args.operation == "acceptance":
            acceptance(provider)
        else:
            start(provider)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Kubernetes demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
