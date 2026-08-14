"""Run the partitioned Stack demo with Docker and Terraform."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from demo.utils import DemoRepository, RefHeads, docker_platform, remove_docker_images, require_commands, run
from gitopsctr import controller
from gitopsctr.state import GitStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".docker-demo-state"
WORKTREE = STATE_ROOT / "repository"
REMOTE = STATE_ROOT / "origin.git"
TERRAFORM_STATE = STATE_ROOT / "terraform.tfstate"
REGISTRY_NAME = "gitopsctr-demo-registry"
APP_NAME = "gitopsctr-demo-app"
STACK_NAME = "application"
REPOSITORY = DemoRepository(TEMPLATE, STATE_ROOT, WORKTREE, REMOTE, "gitopsctr Docker Stack demo")


def configure_template(registry: str, app_port: int) -> None:
    replacements = {
        '"__APP_PORT__"': str(app_port),
        "__APP_PORT__": str(app_port),
        "__DOCKER_PLATFORM__": docker_platform(),
        "__REGISTRY__": registry,
        "__TERRAFORM_STATE__": TERRAFORM_STATE.as_posix(),
    }
    for path in WORKTREE.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
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


def _run_controller(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        *args,
        cwd=WORKTREE,
        capture=capture,
    )
    if capture:
        output = result.stdout + result.stderr
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
    return result


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
    result = _run_controller(
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
        capture=expect_clean,
    )
    if expect_clean and "no drivers ran" not in result.stdout + result.stderr:
        raise RuntimeError("second Docker convergence was not clean")
    message = verify_application(app_port)
    print(f"\nDocker Stack demo is running at http://127.0.0.1:{app_port}/")
    print(f"Response: {message}")


def _desired_tree(label: str) -> Path:
    revision = GitStateStore(WORKTREE).fetch("gitopsctr/desired/dev").revision
    if revision is None:
        raise RuntimeError("Docker demo has no desired revision")
    output = STATE_ROOT / f"desired-{label}"
    if output.exists():
        shutil.rmtree(output)
    GitStateStore(WORKTREE).materialize(revision, output)
    return output


def _desired_metadata(desired: Path, directory: str, name: str) -> Mapping[str, object]:
    paths = controller.document_candidates(desired / directory, name)
    if len(paths) != 1:
        raise RuntimeError(f"expected one desired {directory[:-1]} named {name}")
    document = controller.RESOURCE_CATALOG.load_document(paths[0])
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"desired {directory[:-1]} {name} has no metadata")
    return metadata


def _deletion_generation(metadata: Mapping[str, object], name: str) -> int:
    deletion = metadata.get("deletion")
    if not isinstance(deletion, dict) or not isinstance(deletion.get("generation"), int):
        raise RuntimeError(f"desired resource {name} has no deletion metadata")
    return deletion["generation"]


def _owned_units(desired: Path, stack_uid: str) -> list[tuple[str, str, int]]:
    records: list[tuple[str, str, int]] = []
    for path in sorted((desired / "units").iterdir()):
        if not path.is_file():
            continue
        document = controller.RESOURCE_CATALOG.load_document(path)
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            continue
        owners = metadata.get("ownerReferences")
        if not isinstance(owners, list) or not any(
            isinstance(owner, dict)
            and owner.get("kind") == "Stack"
            and owner.get("name") == STACK_NAME
            and owner.get("uid") == stack_uid
            for owner in owners
        ):
            continue
        name, uid = metadata.get("name"), metadata.get("uid")
        if not isinstance(name, str) or not isinstance(uid, str):
            raise RuntimeError(f"owned Unit in {path} has no name or UID")
        records.append((name, uid, _deletion_generation(metadata, name)))
    return sorted(records, key=lambda item: (not item[0].endswith("--deploy"), item[0]))


def remove_and_finalize_partitioned_stack() -> None:
    stack_path = WORKTREE / "deployment/environments/dev/stacks/application.yaml"
    stack_path.unlink()
    run("git", "add", str(stack_path.relative_to(WORKTREE)), cwd=WORKTREE)
    run("git", "commit", "-m", "Remove Docker application Stack", cwd=WORKTREE)
    run("git", "push", "origin", "main", cwd=WORKTREE)
    _run_controller(
        "apply",
        "--environment",
        "dev",
        "--partition",
        "application",
        "--file",
        "deployment/environments/dev/stacks",
        "--source-revision",
        "HEAD",
    )

    desired = _desired_tree("deleting")
    stack_metadata = _desired_metadata(desired, "stacks", STACK_NAME)
    stack_uid = stack_metadata.get("uid")
    if not isinstance(stack_uid, str):
        raise RuntimeError("deleting Stack has no UID")
    stack_generation = _deletion_generation(stack_metadata, STACK_NAME)
    for unit_name, unit_uid, generation in _owned_units(desired, stack_uid):
        _run_controller(
            "finalize",
            "unit",
            "--environment",
            "dev",
            "--name",
            unit_name,
            "--uid",
            unit_uid,
            "--deletion-generation",
            str(generation),
        )
    _run_controller(
        "finalize",
        "stack",
        "--environment",
        "dev",
        "--name",
        STACK_NAME,
        "--uid",
        stack_uid,
        "--deletion-generation",
        str(stack_generation),
    )
    if run("docker", "container", "inspect", APP_NAME, check=False, capture=True).returncode == 0:
        raise RuntimeError("Stack finalization left the Docker application running")


def clean(registry: str) -> None:
    if shutil.which("docker") is not None:
        for name in (APP_NAME, REGISTRY_NAME):
            if run("docker", "container", "inspect", name, check=False, capture=True).returncode == 0:
                run("docker", "container", "rm", "--force", name)
        remove_docker_images(f"{registry}/gitopsctr-demo/app*")
    REPOSITORY.clean()
    print("Docker Stack demo resources and state removed.")


def acceptance(registry_port: int, app_port: int) -> None:
    registry = f"localhost:{registry_port}"
    clean(registry)
    try:
        converge(registry_port, app_port)
        first_heads = deployment_heads()
        converge(registry_port, app_port, expect_clean=True)
        if deployment_heads() != first_heads:
            raise RuntimeError("clean Docker convergence moved desired or observed refs")
        remove_and_finalize_partitioned_stack()
        print("Acceptance passed: partitioned Docker Stack converged cleanly and finalized child-first.")
    finally:
        clean(registry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "clean", "acceptance"), nargs="?", default="run")
    args = parser.parse_args()
    registry_port = int(os.environ.get("GITOPSCTR_DEMO_REGISTRY_PORT", "5000"))
    app_port = int(os.environ.get("GITOPSCTR_DEMO_APP_PORT", "18080"))
    registry = f"localhost:{registry_port}"
    try:
        if args.operation == "clean":
            clean(registry)
        elif args.operation == "acceptance":
            acceptance(registry_port, app_port)
        else:
            converge(registry_port, app_port)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Docker demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
