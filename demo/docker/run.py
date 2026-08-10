"""Run the isolated local Docker and Terraform demonstration."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run
from gitopsctr import cli
from gitopsctr.state import GitStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".docker-demo-state"
WORKTREE = STATE_ROOT / "repository"
REMOTE = STATE_ROOT / "origin.git"
TERRAFORM_STATE = STATE_ROOT / "terraform.tfstate"
STACK_TERRAFORM_STATE = STATE_ROOT / "stack-terraform.tfstate"
REGISTRY_NAME = "gitopsctr-demo-registry"
APP_NAME = "gitopsctr-demo-app"
STACK_APP_NAME = "gitopsctr-demo-stack-app"
REPOSITORY = DemoRepository(TEMPLATE, STATE_ROOT, WORKTREE, REMOTE, "gitopsctr Docker demo")


def configure_template(registry: str, app_port: int) -> None:
    replacements = {
        '"__APP_PORT__"': str(app_port),
        "__APP_PORT__": str(app_port),
        "__DOCKER_PLATFORM__": docker_platform(),
        "__REGISTRY__": registry,
        "__TERRAFORM_STATE__": TERRAFORM_STATE.as_posix(),
    }
    for path in WORKTREE.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content)


def prepare_repository(registry: str, app_port: int) -> None:
    REPOSITORY.prepare(lambda: configure_template(registry, app_port))


def wait_for_registry(port: int) -> None:
    url = f"http://127.0.0.1:{port}/v2/"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"local registry did not become ready at {url}")


def ensure_registry(port: int) -> None:
    existing = run("docker", "container", "inspect", REGISTRY_NAME, check=False, capture=True)
    if existing.returncode == 0:
        run("docker", "start", REGISTRY_NAME)
    else:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            REGISTRY_NAME,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        )
    wait_for_registry(port)


def verify_application(port: int) -> str:
    url = f"http://127.0.0.1:{port}/"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.read().decode().strip()
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"demo application did not become ready at {url}")


def remove_demo_images(registry: str) -> None:
    remove_docker_images("demo-image:*", f"{registry}/gitopsctr-demo/app*")


def clean(registry: str) -> None:
    if shutil.which("docker") is not None:
        for name in (APP_NAME, STACK_APP_NAME, REGISTRY_NAME):
            existing = run("docker", "container", "inspect", name, check=False, capture=True)
            if existing.returncode == 0:
                run("docker", "container", "rm", "--force", name)
        remove_demo_images(registry)
    REPOSITORY.clean()
    print("Demo resources and state removed.")


def deployment_heads() -> RefHeads:
    return REPOSITORY.heads()


def converge(
    registry_port: int,
    app_port: int,
    *,
    expect_clean: bool = False,
    verify_ports: tuple[int, ...] = (),
) -> None:
    registry = f"localhost:{registry_port}"
    require_commands(
        "docker",
        "git",
        "terraform",
        "curl",
        installation_hint="run 'mise install' and ensure Docker is installed",
    )
    prepare_repository(registry, app_port)
    ensure_registry(registry_port)
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        "converge",
        "--environment",
        "dev",
        "--source-revision",
        "HEAD",
        "--yes",
        cwd=WORKTREE,
        capture=expect_clean,
    )
    if expect_clean:
        output = result.stdout + result.stderr
        print(output, end="" if output.endswith("\n") else "\n")
        if "no drivers ran; 0 ref movements" not in output:
            raise RuntimeError("second convergence was not clean")
    message = verify_application(app_port)
    print(f"\nDemo is running at http://127.0.0.1:{app_port}/")
    print(f"Response: {message}")
    for port in verify_ports:
        stack_message = verify_application(port)
        print(f"Stack demo is running at http://127.0.0.1:{port}/")
        print(f"Stack response: {stack_message}")
    print("Run 'mise run demo' again to observe a clean convergence.")
    print("Run 'mise run demo-clean' to remove all demo effects.")


def _commit_source(message: str) -> str:
    run("git", "add", "deployment/environments/dev/stack-templates", "deployment/environments/dev/stacks", cwd=WORKTREE)
    run("git", "commit", "-m", message, cwd=WORKTREE)
    run("git", "push", "origin", "main", cwd=WORKTREE)
    return run("git", "rev-parse", "HEAD", cwd=WORKTREE, capture=True).stdout.strip()


def add_stack_source(stack_port: int) -> None:
    environment = WORKTREE / "deployment/environments/dev"
    (environment / "stack-templates").mkdir(parents=True, exist_ok=True)
    (environment / "stacks").mkdir(parents=True, exist_ok=True)
    template = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": {
            "parameters": [
                {"name": "container-name", "type": "string"},
                {"name": "host-port", "type": "integer"},
                {"name": "terraform-state", "type": "string"},
                {"name": "image", "type": "object"},
            ],
            "resources": [
                {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "demo-service",
                    "spec": {
                        "source": {"path": "infrastructure", "inputs": ["*.tf"]},
                        "terraform": {
                            "backend": {"path": {"fromParameter": {"name": "terraform-state"}}},
                            "variables": {
                                "container_name": {"fromParameter": {"name": "container-name"}},
                                "host_port": {"fromParameter": {"name": "host-port"}},
                                "image": {"fromParameter": {"name": "image"}},
                            },
                            "observeOutputs": ["container_id", "url"],
                            "checks": [{"type": "http", "urlOutput": "url", "path": "/"}],
                        },
                    },
                }
            ],
        },
    }
    stack = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "preview"},
        "spec": {
            "template": "preview",
            "parameters": {
                "container-name": STACK_APP_NAME,
                "host-port": stack_port,
                "terraform-state": STACK_TERRAFORM_STATE.as_posix(),
                "image": {
                    "fromArtifact": {
                        "unit": "demo-image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "pointer": "/images/application/uri",
                    }
                },
            },
        },
    }
    (environment / "stack-templates/preview.yaml").write_text(yaml.safe_dump(template, sort_keys=False))
    (environment / "stacks/preview.yaml").write_text(yaml.safe_dump(stack, sort_keys=False))
    _commit_source("Add Docker Stack preview")


def remove_stack_source() -> str:
    environment = WORKTREE / "deployment/environments/dev"
    for path in (environment / "stacks").glob("preview.*"):
        path.unlink()
    return _commit_source("Remove Docker Stack preview")


def _run_controller(*args: str) -> None:
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        *args,
        cwd=WORKTREE,
        capture=True,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="" if output.endswith("\n") else "\n")


def _desired_tree() -> Path:
    store = GitStateStore(WORKTREE)
    revision = store.fetch("gitopsctr/desired/dev").revision
    if revision is None:
        raise RuntimeError("desired state was not published")
    output = STATE_ROOT / "desired-inspection"
    if output.exists():
        shutil.rmtree(output)
    store.materialize(revision, output)
    return output


def stack_acceptance(registry_port: int, app_port: int) -> None:
    stack_port = app_port + 1
    add_stack_source(stack_port)
    try:
        converge(registry_port, app_port, verify_ports=(stack_port,))
        first_heads = deployment_heads()
        converge(registry_port, app_port, expect_clean=True, verify_ports=(stack_port,))
        if deployment_heads() != first_heads:
            raise RuntimeError("clean Stack convergence moved desired or observed refs")

        remove_stack_source()
        _run_controller("advance-desired", "--environment", "dev", "--source-revision", "HEAD")
        desired = _desired_tree()
        stack_intent = cli.load_desired_stack_deletion_intents(desired).get("preview")
        if stack_intent is None:
            raise RuntimeError("Stack removal did not create a deletion intent")
        for identity in reversed(stack_intent.owned_unit_closure):
            unit_intent = cli.load_desired_deletion_intents(desired).get(identity.unit_name)
            if unit_intent is None:
                raise RuntimeError(f"Stack removal did not create a Unit deletion intent for {identity.unit_name}")
            _run_controller(
                "finalize",
                "--environment",
                "dev",
                "--unit",
                identity.unit_name,
                "--uid",
                unit_intent.uid,
                "--deletion-generation",
                str(unit_intent.deletion_generation),
            )
            desired = _desired_tree()
        _run_controller(
            "finalize-stack",
            "--environment",
            "dev",
            "--stack",
            "preview",
            "--uid",
            stack_intent.uid,
            "--deletion-generation",
            str(stack_intent.deletion_generation),
        )
        absent = run("docker", "container", "inspect", STACK_APP_NAME, check=False, capture=True)
        if absent.returncode == 0:
            raise RuntimeError("Stack finalization left its Docker container running")
        if verify_application(app_port) == "":
            raise RuntimeError("direct demo application became unavailable during Stack cleanup")
        print("Acceptance passed: Stack-driven Docker/Terraform cleanup removed the Stack application.")
    finally:
        clean(f"localhost:{registry_port}")


def acceptance(registry_port: int, app_port: int) -> None:
    registry = f"localhost:{registry_port}"
    clean(registry)
    try:
        converge(registry_port, app_port)
        first_heads = deployment_heads()
        converge(registry_port, app_port, expect_clean=True)
        second_heads = deployment_heads()
        if second_heads != first_heads:
            raise RuntimeError("clean convergence moved desired or observed refs")
        stack_acceptance(registry_port, app_port)
        final_heads = deployment_heads()
        print(
            "Acceptance passed: "
            f"gitopsctr/desired/dev={final_heads.desired[:12]} "
            f"gitopsctr/observed/dev={final_heads.observed[:12]}"
        )
    finally:
        clean(registry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "reset", "clean", "acceptance"), nargs="?", default="run")
    args = parser.parse_args()
    registry_port = int(os.environ.get("GITOPSCTR_DEMO_REGISTRY_PORT", "5000"))
    app_port = int(os.environ.get("GITOPSCTR_DEMO_APP_PORT", "18080"))
    registry = f"localhost:{registry_port}"
    try:
        if args.operation == "clean":
            clean(registry)
        elif args.operation == "acceptance":
            acceptance(registry_port, app_port)
        elif args.operation == "reset":
            clean(registry)
            converge(registry_port, app_port)
        else:
            converge(registry_port, app_port)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
