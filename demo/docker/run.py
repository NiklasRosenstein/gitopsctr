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

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".docker-demo-state"
WORKTREE = STATE_ROOT / "repository"
REMOTE = STATE_ROOT / "origin.git"
TERRAFORM_STATE = STATE_ROOT / "terraform.tfstate"
REGISTRY_NAME = "gitopsctr-demo-registry"
APP_NAME = "gitopsctr-demo-app"
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
        for name in (APP_NAME, REGISTRY_NAME):
            existing = run("docker", "container", "inspect", name, check=False, capture=True)
            if existing.returncode == 0:
                run("docker", "container", "rm", "--force", name)
        remove_demo_images(registry)
    REPOSITORY.clean()
    print("Demo resources and state removed.")


def deployment_heads() -> RefHeads:
    return REPOSITORY.heads()


def converge(registry_port: int, app_port: int, *, expect_clean: bool = False) -> None:
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
    print("Run 'mise run demo' again to observe a clean convergence.")
    print("Run 'mise run demo-clean' to remove all demo effects.")


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
        print(f"Acceptance passed: deploy/dev={second_heads.desired[:12]} observed/dev={second_heads.observed[:12]}")
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
