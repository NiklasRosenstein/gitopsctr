"""Run the Stack-based Kubernetes demo against kind or minikube."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

import yaml

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run
from gitopsctr import controller as controller_module
from gitopsctr.controller import color_enabled
from gitopsctr.state import GitStateStore

Provider = Literal["kind", "minikube"]
Delivery = Literal["direct", "argocd"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".k8s-demo-state"
ARGO_NAMESPACE = "argocd"
ARGO_GIT_SERVICE = "gitopsctr-demo-git"
ARGOCD_VERSION = "v3.4.2"
GIT_DAEMON_IMAGE = "alpine:3.20.3"
SOURCE_STACK = "application"
PREVIEW_STACK = "preview"
EXPECTED_RESPONSE = "Hello from a gitopsctr-managed Kubernetes container!"
WORKLOADS = {
    "dev": "gitopsctr-k8s-dev",
    "staging": "gitopsctr-k8s-staging",
    "preview": "gitopsctr-k8s-preview",
}
MESSAGES = {
    "dev": "rendered and reconciled in dev",
    "staging": "promoted from dev to staging",
    "preview": "unpartitioned preview",
}


def provider_from_environment() -> Provider:
    value = os.environ.get("GITOPSCTR_K8S_PROVIDER", "kind").strip().lower()
    if value not in {"kind", "minikube"}:
        raise RuntimeError("GITOPSCTR_K8S_PROVIDER must be 'kind' or 'minikube'")
    return value


def cluster_name(delivery: Delivery) -> str:
    return f"gitopsctr-k8s-{delivery}"


def kube_context(provider: Provider, delivery: Delivery = "direct") -> str:
    name = cluster_name(delivery)
    return f"kind-{name}" if provider == "kind" else name


def repository(
    provider: Provider,
    delivery: Delivery = "direct",
    *,
    preview: bool = False,
    remote: str | None = None,
) -> DemoRepository:
    mode = "preview" if preview else "promotion"
    state_root = STATE_ROOT / delivery / provider / mode
    return DemoRepository(
        template=TEMPLATE,
        state_root=state_root,
        worktree=state_root / "repository",
        remote=remote or state_root / "origin.git",
        identity=f"gitopsctr Kubernetes {delivery} {provider} {mode} demo",
    )


def configure_template(provider: Provider, worktree: Path, delivery: Delivery = "direct") -> None:
    replacements = {
        "__CLUSTER_NAME__": cluster_name(delivery),
        "__DOCKER_PLATFORM__": docker_platform(),
        "__KUBE_CONTEXT__": kube_context(provider, delivery),
    }
    for path in worktree.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        content = path.read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)

    template_path = worktree / "deployment/stack-templates/application.yaml"
    template = yaml.safe_load(template_path.read_text())
    image = template["spec"]["unitTemplates"]["image"]["spec"]
    target = image["publish"]["targets"]["application"]
    if provider == "minikube":
        target["type"] = "minikube"
        target["profile"] = target.pop("cluster")
    if delivery == "argocd":
        deploy = template["spec"]["unitTemplates"]["deploy"]["spec"]
        deploy["delivery"] = {
            "mode": "external",
            "observer": {
                "type": "argocd",
                "access": "kubernetes",
                "application": {"fromParameter": {"name": "argocd-application"}},
                "applicationNamespace": ARGO_NAMESPACE,
                "kubeContext": {"fromParameter": {"name": "kube-context"}},
                "timeoutSeconds": 600,
            },
        }
    template_path.write_text(yaml.safe_dump(template, sort_keys=False))


def prepare_repository(
    provider: Provider,
    delivery: Delivery = "direct",
    *,
    preview: bool = False,
    remote: str | None = None,
) -> DemoRepository:
    demo_repository = repository(provider, delivery, preview=preview, remote=remote)
    demo_repository.prepare(lambda: configure_template(provider, demo_repository.worktree, delivery))
    return demo_repository


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


def clean(provider: Provider, delivery: Delivery = "direct") -> None:
    name = cluster_name(delivery)
    if provider == "kind" and shutil.which("kind") is not None:
        run("kind", "delete", "cluster", "--name", name, check=False)
    elif provider == "minikube" and shutil.which("minikube") is not None:
        run("minikube", "delete", "--profile", name, check=False)
    if shutil.which("docker") is not None:
        remove_docker_images("application--image:*", "preview--image:*")
    state_root = STATE_ROOT / delivery / provider
    if state_root.exists():
        shutil.rmtree(state_root)
    print(f"Kubernetes {delivery} demo state removed for {provider}.")


def run_controller(
    provider: Provider,
    *args: str,
    delivery: Delivery = "direct",
    preview: bool = False,
    remote: str | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    worktree = repository(provider, delivery, preview=preview, remote=remote).worktree
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
        check=check,
        env=environment,
    )


def deployment_heads(
    provider: Provider,
    environment: str,
    delivery: Delivery = "direct",
    *,
    preview: bool = False,
    remote: str | None = None,
) -> RefHeads:
    return repository(provider, delivery, preview=preview, remote=remote).heads(environment)


def workload_image(provider: Provider, environment: str, delivery: Delivery) -> str:
    return run(
        "kubectl",
        "--context",
        kube_context(provider, delivery),
        "--namespace",
        "default",
        "get",
        "deployment",
        WORKLOADS[environment],
        "--output",
        "jsonpath={.spec.template.spec.containers[0].image}",
        capture=True,
    ).stdout


def verify_workload(
    provider: Provider,
    environment: str,
    delivery: Delivery,
    *,
    preview: bool = False,
    remote: str | None = None,
) -> str:
    context = kube_context(provider, delivery)
    name = WORKLOADS[environment]
    run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "rollout",
        "status",
        f"deployment/{name}",
        "--timeout=120s",
    )
    message = run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "get",
        "configmap",
        name,
        "--output",
        "jsonpath={.data.message}",
        capture=True,
    ).stdout
    if message != MESSAGES[environment]:
        raise RuntimeError(f"{environment} ConfigMap contains {message!r}, expected {MESSAGES[environment]!r}")
    response = run(
        "kubectl",
        "--context",
        context,
        "--namespace",
        "default",
        "exec",
        f"deployment/{name}",
        "--",
        "wget",
        "--quiet",
        "--output-document=-",
        "http://127.0.0.1:8080/",
        capture=True,
    ).stdout.strip()
    if response != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected {environment} application response: {response!r}")
    stack_name = PREVIEW_STACK if preview else SOURCE_STACK
    run_controller(
        provider,
        "verify",
        "--environment",
        environment,
        "--unit",
        f"{stack_name}--deploy",
        delivery=delivery,
        preview=preview,
        remote=remote,
    )
    print(f"{environment} application check passed: {response}", flush=True)
    return workload_image(provider, environment, delivery)


def converge(
    provider: Provider,
    environment: str,
    delivery: Delivery,
    *,
    preview: bool = False,
    remote: str | None = None,
    source_revision: str | None = None,
    expect_clean: bool = False,
    allow_stall: bool = False,
    files: tuple[str, ...] = (),
    partition: str | None = None,
) -> None:
    arguments = ["converge", "--environment", environment, "--yes"]
    if source_revision is not None:
        arguments.extend(("--source-revision", source_revision))
    if partition is not None:
        arguments.extend(("--partition", partition))
    for path in files:
        arguments.extend(("--file", path))
    for attempt in range(4):
        result = run_controller(
            provider,
            *arguments,
            delivery=delivery,
            preview=preview,
            remote=remote,
            capture=True,
            check=False,
        )
        output = result.stdout + result.stderr
        print(output, end="" if not output or output.endswith("\n") else "\n")
        if result.returncode == 0:
            if expect_clean and "no drivers ran" not in output:
                raise RuntimeError(f"clean {environment} convergence ran a driver or moved a ref")
            return
        if "convergence stalled with no ready unit" not in output:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        if allow_stall:
            return
        if delivery == "argocd":
            refresh_argo_application(provider, environment)
        time.sleep(min(attempt + 1, 2))
    raise RuntimeError(f"{environment} did not converge after four passes")


def apply_document(provider: Provider, document: object) -> None:
    payload = (
        yaml.safe_dump_all(document, sort_keys=False)
        if isinstance(document, list)
        else yaml.safe_dump(document, sort_keys=False)
    )
    run(
        "kubectl",
        "--context",
        kube_context(provider, "argocd"),
        "apply",
        "--filename",
        "-",
        input_text=payload,
    )


def argo_project_document() -> dict[str, object]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": "default", "namespace": ARGO_NAMESPACE},
        "spec": {"sourceRepos": ["*"], "destinations": [{"namespace": "*", "server": "*"}]},
    }


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
    apply_document(provider, argo_project_document())


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
                                    "apk add --no-cache git-daemon >/dev/null; exec git daemon --reuseaddr "
                                    "--export-all --enable=receive-pack --base-path=/repositories --port=9418 "
                                    "/repositories",
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


def argo_application_document(environment: str, preview: bool = False) -> dict[str, object]:
    stack_name = PREVIEW_STACK if preview else SOURCE_STACK
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": WORKLOADS[environment], "namespace": ARGO_NAMESPACE},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": f"git://{ARGO_GIT_SERVICE}.{ARGO_NAMESPACE}.svc.cluster.local:9418/origin.git",
                "targetRevision": f"gitopsctr/desired/{environment}",
                "path": f"materialized/{stack_name}--deploy",
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            "syncPolicy": {"automated": {}},
        },
    }


def refresh_argo_application(provider: Provider, environment: str) -> None:
    run(
        "kubectl",
        "--context",
        kube_context(provider, "argocd"),
        "--namespace",
        ARGO_NAMESPACE,
        "patch",
        "application.argoproj.io",
        WORKLOADS[environment],
        "--type",
        "merge",
        "--patch",
        json.dumps({"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}}),
        check=False,
    )


def setup_argocd_applications(provider: Provider, preview: bool) -> None:
    environments = ("preview",) if preview else ("dev", "staging")
    for environment in environments:
        apply_document(provider, argo_application_document(environment, preview))


def source_revision(worktree: Path) -> str:
    return run("git", "rev-parse", "HEAD", cwd=worktree, capture=True).stdout.strip()


def run_promotion_story(
    provider: Provider,
    delivery: Delivery,
    *,
    remote: str | None = None,
    expect_clean: bool = False,
) -> tuple[RefHeads, RefHeads]:
    converge(
        provider,
        "dev",
        delivery,
        remote=remote,
        source_revision="HEAD",
        files=("deployment/environments/dev/stacks/application.yaml",),
        partition="application",
        expect_clean=expect_clean,
    )
    dev_image = verify_workload(provider, "dev", delivery, remote=remote)
    if not expect_clean:
        run_controller(
            provider,
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--file",
            "deployment/environments/staging/stacks/application.yaml",
            "--partition",
            "application",
            delivery=delivery,
            remote=remote,
        )
    converge(provider, "staging", delivery, remote=remote, expect_clean=expect_clean)
    staging_image = verify_workload(provider, "staging", delivery, remote=remote)
    if staging_image != dev_image:
        raise RuntimeError("staging did not deploy the exact image artifact promoted from dev")
    return (
        deployment_heads(provider, "dev", delivery, remote=remote),
        deployment_heads(provider, "staging", delivery, remote=remote),
    )


def desired_stack(worktree: Path) -> tuple[str, str, str]:
    store = GitStateStore(worktree)
    revision = store.fetch("gitopsctr/desired/preview").revision
    if revision is None:
        raise RuntimeError("preview has no desired state")
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "desired"
        store.materialize(revision, root)
        paths = controller_module.document_candidates(root / "stacks", PREVIEW_STACK)
        if len(paths) != 1:
            raise RuntimeError("unpartitioned preview Stack is missing")
        stack = controller_module.RESOURCE_CATALOG.parse_stack(
            controller_module.RESOURCE_CATALOG.load_document(paths[0]),
            profile="desired",
            expected_name=PREVIEW_STACK,
        )
        if stack.metadata.uid is None or stack.metadata.partition is not None:
            raise RuntimeError("preview Stack is not an unpartitioned root")
        if not isinstance(stack.spec, controller_module.DesiredStackSpec) or stack.spec.resolvedSource is None:
            raise RuntimeError("preview Stack has no resolved template source")
        return revision, stack.metadata.uid, stack.spec.resolvedSource.fromGit.commit


def run_preview_story(
    provider: Provider,
    delivery: Delivery,
    *,
    remote: str | None = None,
    expect_clean: bool = False,
) -> tuple[RefHeads, str]:
    worktree = repository(provider, delivery, preview=True, remote=remote).worktree
    revision = source_revision(worktree)
    converge(
        provider,
        "preview",
        delivery,
        preview=True,
        remote=remote,
        source_revision=revision,
        files=("deployment/environments/preview/stacks/preview.yaml",),
        expect_clean=expect_clean,
    )
    image = verify_workload(provider, "preview", delivery, preview=True, remote=remote)
    return deployment_heads(provider, "preview", delivery, preview=True, remote=remote), image


def publish_preview_source(worktree: Path) -> str:
    path = worktree / "app.py"
    content = path.read_text()
    marker = "# acceptance revision R2\n"
    if marker not in content:
        path.write_text(content + "\n" + marker)
        run("git", "add", "app.py", cwd=worktree)
        run("git", "commit", "-m", "Publish preview application R2", cwd=worktree)
        run("git", "push", "origin", "main", cwd=worktree)
    return source_revision(worktree)


def request_preview_deletion(
    provider: Provider,
    delivery: Delivery,
    *,
    remote: str | None = None,
) -> None:
    _desired_revision, uid, _template_revision = desired_stack(
        repository(provider, delivery, preview=True, remote=remote).worktree
    )
    run_controller(
        provider,
        "delete",
        "stack",
        "--environment",
        "preview",
        "--name",
        PREVIEW_STACK,
        "--uid",
        uid,
        delivery=delivery,
        preview=True,
        remote=remote,
    )


def execute_story(
    provider: Provider,
    delivery: Delivery,
    *,
    preview: bool,
    acceptance: bool,
    remote: str | None,
) -> None:
    prepare_repository(provider, delivery, preview=preview, remote=remote)
    if delivery == "argocd":
        setup_argocd_applications(provider, preview)
    if preview:
        first_heads, first_image = run_preview_story(provider, delivery, remote=remote)
        if acceptance:
            clean_heads, _image = run_preview_story(provider, delivery, remote=remote, expect_clean=True)
            if clean_heads != first_heads:
                raise RuntimeError("clean preview convergence moved desired or observed refs")
            worktree = repository(provider, delivery, preview=True, remote=remote).worktree
            revision = publish_preview_source(worktree)
            converge(
                provider,
                "preview",
                delivery,
                preview=True,
                remote=remote,
                source_revision=revision,
                files=("deployment/environments/preview/stacks/preview.yaml",),
                allow_stall=True,
            )
            converge(
                provider,
                "preview",
                delivery,
                preview=True,
                remote=remote,
                source_revision=revision,
                files=("deployment/environments/preview/stacks/preview.yaml",),
            )
            second_image = verify_workload(provider, "preview", delivery, preview=True, remote=remote)
            if second_image == first_image:
                raise RuntimeError("preview application did not publish a new image")
            request_preview_deletion(provider, delivery, remote=remote)
            print("Acceptance passed: unpartitioned preview applied, converged, updated, and entered deletion.")
        return

    first_heads = run_promotion_story(provider, delivery, remote=remote)
    if acceptance:
        clean_heads = run_promotion_story(provider, delivery, remote=remote, expect_clean=True)
        if clean_heads != first_heads:
            raise RuntimeError("clean promoted-environment convergence moved desired or observed refs")
        print("Acceptance passed: dev Stack promoted its exact image artifact to staging and converged cleanly.")


def run_demo(provider: Provider, delivery: Delivery, *, preview: bool, acceptance: bool) -> None:
    print(f"Using Kubernetes provider {provider}; delivery={delivery}; mode={'preview' if preview else 'promotion'}.")
    require_commands("docker", "git", "helm", provider, "kubectl")
    ensure_cluster(provider, delivery)
    if delivery == "direct":
        execute_story(provider, delivery, preview=preview, acceptance=acceptance, remote=None)
        return
    install_argocd(provider)
    install_git_server(provider)
    with git_port_forward(provider) as remote:
        execute_story(provider, delivery, preview=preview, acceptance=acceptance, remote=remote)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "clean", "acceptance"), nargs="?", default="run")
    parser.add_argument("--preview", action="store_true", help="apply an unpartitioned preview Stack")
    parser.add_argument("--delivery", choices=("direct", "argocd"), default="direct")
    args = parser.parse_args()
    try:
        provider = provider_from_environment()
        delivery = cast(Delivery, args.delivery)
        if args.operation == "clean":
            clean(provider, delivery)
        elif args.operation == "acceptance":
            clean(provider, delivery)
            try:
                run_demo(provider, delivery, preview=args.preview, acceptance=True)
            finally:
                clean(provider, delivery)
        else:
            run_demo(provider, delivery, preview=args.preview, acceptance=False)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Kubernetes demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
