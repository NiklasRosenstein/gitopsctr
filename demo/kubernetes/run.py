"""Run the local Kubernetes demos against kind or minikube."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run
from gitopsctr.cli import color_enabled

Provider = Literal["kind", "minikube"]
Delivery = Literal["direct", "argocd"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".kubernetes-demo-state"
DIRECT_CLUSTER_NAME = "gitopsctr-kubernetes-demo"
ARGO_CLUSTER_NAME = "gitopsctr-argocd-demo"
CLUSTER_NAME = DIRECT_CLUSTER_NAME
RESOURCE_NAME = "gitopsctr-kubernetes-demo"
EXPECTED_RESPONSE = "Hello from a gitopsctr-managed Kubernetes container!"
ARGO_NAMESPACE = "argocd"
ARGO_APPLICATION = "gitopsctr-kubernetes-demo"
ARGO_GIT_SERVICE = "gitopsctr-acceptance-git"
ARGOCD_VERSION = "v3.4.2"
# The alpine/git image intentionally omits the git-daemon executable.  Alpine's
# git-daemon package supplies both the client and server binaries we need.
GIT_DAEMON_IMAGE = "alpine:3.20.3"


@dataclass(frozen=True)
class MaterializedWorkload:
    image: str
    message: str


def cluster_name(delivery: Delivery) -> str:
    return DIRECT_CLUSTER_NAME if delivery == "direct" else ARGO_CLUSTER_NAME


def kube_context(provider: Provider, delivery: Delivery = "direct") -> str:
    name = cluster_name(delivery)
    return f"kind-{name}" if provider == "kind" else name


def repository(provider: Provider, delivery: Delivery = "direct", remote: str | None = None) -> DemoRepository:
    state_root = STATE_ROOT / provider if delivery == "direct" else STATE_ROOT / delivery / provider
    return DemoRepository(
        template=TEMPLATE,
        state_root=state_root,
        worktree=state_root / "repository",
        remote=remote or state_root / "origin.git",
        identity=f"gitopsctr Kubernetes {delivery} {provider} demo",
    )


def configure_template(provider: Provider, worktree: Path, delivery: Delivery = "direct") -> None:
    replacements = {
        "__CLUSTER_NAME__": cluster_name(delivery),
        "__DOCKER_PLATFORM__": docker_platform(),
        "__KUBE_CONTEXT__": kube_context(provider, delivery),
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
    if delivery == "argocd":
        web_path = worktree / "deployment/environments/dev/units/web.yaml"
        document = yaml.safe_load(web_path.read_text())
        document["spec"]["delivery"] = {
            "mode": "external",
            "observer": {
                "type": "argocd",
                "access": "kubernetes",
                "application": ARGO_APPLICATION,
                "applicationNamespace": ARGO_NAMESPACE,
                "kubeContext": kube_context(provider, delivery),
                "timeoutSeconds": 600,
            },
        }
        web_path.write_text(yaml.safe_dump(document, sort_keys=False))


def prepare_repository(provider: Provider, delivery: Delivery = "direct", remote: str | None = None) -> DemoRepository:
    demo_repository = repository(provider, delivery, remote)
    demo_repository.prepare(lambda: configure_template(provider, demo_repository.worktree, delivery))
    return demo_repository


def clean(provider: Provider, delivery: Delivery = "direct") -> None:
    name = cluster_name(delivery)
    if provider == "kind" and shutil.which("kind") is not None:
        run("kind", "delete", "cluster", "--name", name, check=False)
    elif provider == "minikube" and shutil.which("minikube") is not None:
        run("minikube", "delete", "--profile", name, check=False)
    if shutil.which("docker") is not None:
        remove_docker_images("demo-image:*")
    repository(provider, delivery).clean()
    print(f"Kubernetes {delivery} {provider} demo cluster and state removed.")


def ensure_cluster(provider: Provider, delivery: Delivery = "direct") -> None:
    name = cluster_name(delivery)
    if provider == "kind":
        clusters = run("kind", "get", "clusters", capture=True).stdout.splitlines()
        if name not in clusters:
            run("kind", "create", "cluster", "--name", name, "--wait", "120s")
        return
    status = run("minikube", "status", "--profile", name, check=False, capture=True)
    if status.returncode != 0:
        run("minikube", "start", "--profile", name, "--driver", "docker", "--wait", "all")


def controller(
    provider: Provider, *args: str, delivery: Delivery = "direct", capture: bool = False
) -> subprocess.CompletedProcess[str]:
    worktree = repository(provider).worktree if delivery == "direct" else repository(provider, delivery).worktree
    environment = None
    if capture and color_enabled(sys.stderr) and not os.environ.get("NO_COLOR"):
        environment = os.environ.copy()
        environment["FORCE_COLOR"] = "1"
    return run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(worktree),
        *args,
        cwd=worktree,
        capture=capture,
        env=environment,
    )


def deployment_heads(provider: Provider, delivery: Delivery = "direct", remote: str | None = None) -> RefHeads:
    return repository(provider, delivery, remote).heads()


def verify_resource(
    provider: Provider,
    delivery: Delivery = "direct",
    expected: MaterializedWorkload | None = None,
) -> None:
    context = kube_context(provider, delivery)
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
    if expected is not None and image != expected.image:
        raise RuntimeError(f"deployed image does not match materialized payload: {image!r}")
    if not image.startswith("demo-image:"):
        raise RuntimeError(f"unexpected deployed image: {image!r}")
    message = run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "get",
        "configmap",
        RESOURCE_NAME,
        "--output",
        "jsonpath={.data.message}",
        capture=True,
    ).stdout
    if expected is not None and message != expected.message:
        raise RuntimeError(f"rendered ConfigMap does not match materialized payload: {message!r}")
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
    controller(provider, "verify", "--environment", "dev", "--unit", "web", delivery=delivery)


def converge(provider: Provider, delivery: Delivery = "direct", *, expect_clean: bool = False) -> None:
    result = controller(
        provider,
        "converge",
        "--environment",
        "dev",
        "--source-revision",
        "HEAD",
        "--yes",
        delivery=delivery,
        capture=expect_clean,
    )
    if expect_clean:
        output = result.stdout + result.stderr
        print(output, end="" if output.endswith("\n") else "\n")
        if "no drivers ran; 0 ref movements" not in output:
            raise RuntimeError("second Kubernetes convergence was not clean")
    print()


def apply_document(provider: Provider, document: object, delivery: Delivery = "argocd") -> None:
    payload = (
        yaml.safe_dump_all(document, sort_keys=False)
        if isinstance(document, list)
        else yaml.safe_dump(document, sort_keys=False)
    )
    run(
        "kubectl",
        "--context",
        kube_context(provider, delivery),
        "apply",
        "--filename",
        "-",
        input_text=payload,
    )


def install_argocd(provider: Provider) -> None:
    context = kube_context(provider, "argocd")
    run("kubectl", "--context", context, "create", "namespace", ARGO_NAMESPACE, check=False)
    run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        ARGO_NAMESPACE,
        "apply",
        "--server-side",
        "--force-conflicts",
        "--filename",
        f"https://raw.githubusercontent.com/argoproj/argo-cd/{ARGOCD_VERSION}/manifests/core-install.yaml",
    )
    run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        ARGO_NAMESPACE,
        "wait",
        "--for=condition=Ready",
        "pod",
        "--all",
        "--timeout=300s",
    )
    run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        ARGO_NAMESPACE,
        "patch",
        "configmap",
        "argocd-cm",
        "--type",
        "merge",
        "--patch",
        json.dumps({"data": {"timeout.reconciliation": "5s", "timeout.reconciliation.jitter": "0s"}}),
    )
    apply_document(provider, argo_project_document())


def argo_project_document() -> dict[str, object]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": "default", "namespace": ARGO_NAMESPACE},
        "spec": {
            "sourceRepos": ["*"],
            "destinations": [{"namespace": "*", "server": "*"}],
        },
    }


def git_server_document() -> list[dict[str, object]]:
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": ARGO_GIT_SERVICE, "namespace": ARGO_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": ARGO_GIT_SERVICE}},
                "template": {
                    "metadata": {"labels": {"app": ARGO_GIT_SERVICE}},
                    "spec": {
                        "volumes": [{"name": "repositories", "emptyDir": {}}],
                        "initContainers": [
                            {
                                "name": "initialize",
                                "image": GIT_DAEMON_IMAGE,
                                "command": [
                                    "sh",
                                    "-ec",
                                    "apk add --no-cache git-daemon >/dev/null; git init --bare /repositories/origin.git",
                                ],
                                "volumeMounts": [{"name": "repositories", "mountPath": "/repositories"}],
                            }
                        ],
                        "containers": [
                            {
                                "name": "git",
                                "image": GIT_DAEMON_IMAGE,
                                "command": [
                                    "sh",
                                    "-ec",
                                    "apk add --no-cache git-daemon >/dev/null; exec git daemon --reuseaddr --export-all "
                                    "--enable=receive-pack --base-path=/repositories --port=9418 /repositories",
                                ],
                                "ports": [{"containerPort": 9418}],
                                "readinessProbe": {
                                    "tcpSocket": {"port": 9418},
                                    "periodSeconds": 1,
                                    "failureThreshold": 30,
                                },
                                "volumeMounts": [{"name": "repositories", "mountPath": "/repositories"}],
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": ARGO_GIT_SERVICE, "namespace": ARGO_NAMESPACE},
            "spec": {
                "selector": {"app": ARGO_GIT_SERVICE},
                "ports": [{"name": "git", "port": 9418, "targetPort": 9418}],
            },
        },
    ]


def install_git_server(provider: Provider) -> None:
    apply_document(provider, git_server_document())
    run(
        "kubectl",
        "--context",
        kube_context(provider, "argocd"),
        "--namespace",
        ARGO_NAMESPACE,
        "rollout",
        "status",
        f"deployment/{ARGO_GIT_SERVICE}",
        "--timeout=120s",
    )


def argo_application_document() -> dict[str, object]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": ARGO_APPLICATION, "namespace": ARGO_NAMESPACE},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": f"git://{ARGO_GIT_SERVICE}.{ARGO_NAMESPACE}.svc.cluster.local:9418/origin.git",
                "targetRevision": "gitopsctr/desired/dev",
                "path": "materialized/web",
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            "syncPolicy": {"automated": {}},
        },
    }


@contextmanager
def git_port_forward(provider: Provider) -> Iterator[str]:
    context = kube_context(provider, "argocd")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            "kubectl",
            "--context",
            context,
            "--namespace",
            ARGO_NAMESPACE,
            "port-forward",
            f"service/{ARGO_GIT_SERVICE}",
            f"{port}:9418",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    remote = f"git://127.0.0.1:{port}/origin.git"
    try:
        for _ in range(30):
            if process.poll() is not None:
                detail = process.stderr.read() if process.stderr is not None else ""
                raise RuntimeError(f"Git port-forward exited early: {detail.strip()}")
            if run("git", "ls-remote", remote, check=False).returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("timed out waiting for Git port-forward")
        yield remote
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def application_port_forward(provider: Provider) -> Iterator[int]:
    context = kube_context(provider, "argocd")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            "kubectl",
            "--context",
            context,
            "--namespace",
            "default",
            "port-forward",
            f"deployment/{RESOURCE_NAME}",
            f"{port}:8080",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(30):
            if process.poll() is not None:
                detail = process.stderr.read() if process.stderr is not None else ""
                raise RuntimeError(f"Application port-forward exited early: {detail.strip()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("timed out waiting for application port-forward")
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def verify_forwarded_application(provider: Provider) -> None:
    with application_port_forward(provider) as port:
        url = f"http://127.0.0.1:{port}/"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode().strip()
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"request to forwarded application failed at {url}") from exc
    if body != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected forwarded application response: {body!r}")
    print(f"Port-forwarded application check passed: {body}", flush=True)


def application_status(provider: Provider) -> dict[str, object]:
    output = run(
        "kubectl",
        "--context",
        kube_context(provider, "argocd"),
        "--namespace",
        ARGO_NAMESPACE,
        "get",
        "application.argoproj.io",
        ARGO_APPLICATION,
        "--output",
        "json",
        capture=True,
    ).stdout
    return cast(dict[str, object], json.loads(output))


def refresh_argo_application(provider: Provider) -> None:
    run(
        "kubectl",
        "--context",
        kube_context(provider, "argocd"),
        "--namespace",
        ARGO_NAMESPACE,
        "patch",
        "application.argoproj.io",
        ARGO_APPLICATION,
        "--type",
        "merge",
        "--patch",
        json.dumps({"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}}),
    )


def verify_argo_revision(provider: Provider, expected_revision: str) -> None:
    status = cast(dict[str, object], application_status(provider).get("status", {}))
    sync = cast(dict[str, object], status.get("sync", {}))
    health = cast(dict[str, object], status.get("health", {}))
    if sync.get("revision") != expected_revision or sync.get("status") != "Synced" or health.get("status") != "Healthy":
        raise RuntimeError(f"Argo CD did not reach the expected revision: {status!r}")


def materialized_workload(provider: Provider, remote: str, revision: str) -> MaterializedWorkload:
    worktree = repository(provider, "argocd", remote).worktree
    run("git", "fetch", "origin", "gitopsctr/desired/dev", cwd=worktree)
    fetched_revision = run("git", "rev-parse", "FETCH_HEAD", cwd=worktree, capture=True).stdout.strip()
    if fetched_revision != revision:
        raise RuntimeError(f"materialized desired ref changed from {revision} to {fetched_revision}")
    documents = list(
        yaml.safe_load_all(
            run("git", "show", "FETCH_HEAD:materialized/web/manifest.yaml", cwd=worktree, capture=True).stdout
        )
    )
    config_map = next(
        (document for document in documents if isinstance(document, dict) and document.get("kind") == "ConfigMap"),
        None,
    )
    deployment = next(
        (document for document in documents if isinstance(document, dict) and document.get("kind") == "Deployment"),
        None,
    )
    if not isinstance(config_map, dict) or not isinstance(deployment, dict):
        raise RuntimeError("materialized Helm payload is missing its ConfigMap or Deployment")
    message = config_map.get("data", {}).get("message")
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    image = containers[0].get("image") if isinstance(containers, list) and containers else None
    if not isinstance(message, str) or not isinstance(image, str):
        raise RuntimeError("materialized Helm payload has an invalid workload")
    return MaterializedWorkload(image=image, message=message)


def argocd_diagnostics(provider: Provider) -> None:
    context = kube_context(provider, "argocd")
    for args in (
        ("get", "pods", "--all-namespaces"),
        ("get", "application.argoproj.io", ARGO_APPLICATION, "--output", "yaml"),
        ("get", "events", "--namespace", ARGO_NAMESPACE),
    ):
        result = run("kubectl", "--context", context, *args, check=False, capture=True)
        print(result.stdout + result.stderr, file=sys.stderr)


def start(provider: Provider, delivery: Delivery = "direct") -> None:
    require_commands("docker", "git", "helm", provider, "kubectl")
    ensure_cluster(provider, delivery)
    if delivery == "direct":
        prepare_repository(provider)
        converge(provider)
        verify_resource(provider)
        print(f"Kubernetes demo is running in context {kube_context(provider)}.")
        return
    install_argocd(provider)
    install_git_server(provider)
    with git_port_forward(provider) as remote:
        prepare_repository(provider, delivery, remote)
        apply_document(provider, argo_application_document())
        controller(
            provider,
            "advance-desired",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            delivery=delivery,
        )
        controller(provider, "reconcile", "--environment", "dev", "--unit", "demo-image", delivery=delivery)
        controller(
            provider,
            "reconcile",
            "--environment",
            "dev",
            "--unit",
            "web",
            "--advance",
            "--source-revision",
            "HEAD",
            delivery=delivery,
        )
        heads = deployment_heads(provider, delivery, remote)
        verify_resource(provider, delivery, materialized_workload(provider, remote, heads.desired))
    print(f"Argo CD Kubernetes demo completed in context {kube_context(provider, delivery)}.")


def acceptance(provider: Provider, delivery: Delivery = "direct") -> None:
    if delivery == "direct":
        clean(provider)
    else:
        clean(provider, delivery)
    try:
        if delivery == "direct":
            start(provider)
            first_heads = deployment_heads(provider)
            converge(provider, expect_clean=True)
            second_heads = deployment_heads(provider)
        else:
            require_commands("docker", "git", "helm", provider, "kubectl")
            ensure_cluster(provider, delivery)
            install_argocd(provider)
            install_git_server(provider)
            with git_port_forward(provider) as remote:
                prepare_repository(provider, delivery, remote)
                apply_document(provider, argo_application_document())
                controller(
                    provider,
                    "advance-desired",
                    "--environment",
                    "dev",
                    "--source-revision",
                    "HEAD",
                    delivery=delivery,
                )
                controller(provider, "reconcile", "--environment", "dev", "--unit", "demo-image", delivery=delivery)
                controller(
                    provider,
                    "advance-desired",
                    "--environment",
                    "dev",
                    "--source-revision",
                    "HEAD",
                    delivery=delivery,
                )
                refresh_argo_application(provider)
                controller(
                    provider,
                    "reconcile",
                    "--environment",
                    "dev",
                    "--unit",
                    "web",
                    delivery=delivery,
                )
                first_heads = deployment_heads(provider, delivery, remote)
                verify_argo_revision(provider, first_heads.desired)
                verify_resource(provider, delivery, materialized_workload(provider, remote, first_heads.desired))
                verify_forwarded_application(provider)
                converge(provider, delivery, expect_clean=True)
                second_heads = deployment_heads(provider, delivery, remote)
        if second_heads != first_heads:
            raise RuntimeError("clean Kubernetes convergence moved desired or observed refs")
        print(
            "Acceptance passed: "
            f"gitopsctr/desired/dev={second_heads.desired[:12]} "
            f"gitopsctr/observed/dev={second_heads.observed[:12]}"
        )
    except Exception:
        if delivery == "argocd":
            argocd_diagnostics(provider)
        raise
    finally:
        if delivery == "direct":
            clean(provider)
        else:
            clean(provider, delivery)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "clean", "acceptance"))
    parser.add_argument("provider", choices=("kind", "minikube"), help="local cluster provider (required)")
    parser.add_argument("--delivery", choices=("direct", "argocd"), default="direct")
    args = parser.parse_args()
    provider = cast(Provider, args.provider)
    delivery = cast(Delivery, args.delivery)
    try:
        if args.operation == "clean":
            clean(provider, delivery)
        elif args.operation == "acceptance":
            acceptance(provider, delivery)
        else:
            start(provider, delivery)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Kubernetes demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
