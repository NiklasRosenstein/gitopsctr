"""Run the isolated Helm and kind acceptance demonstration."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / "repository"
STATE_ROOT = PROJECT_ROOT / ".kubernetes-demo-state"
WORKTREE = STATE_ROOT / "repository"
REMOTE = STATE_ROOT / "origin.git"
CLUSTER_NAME = "gitopsctr-kubernetes-demo"
KUBE_CONTEXT = f"kind-{CLUSTER_NAME}"
RESOURCE_NAME = "gitopsctr-kubernetes-demo"


def run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=capture)


def require_commands() -> None:
    missing = [name for name in ("git", "helm", "kind", "kubectl") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing required commands: {', '.join(missing)}; run 'mise install'")


def prepare_repository() -> None:
    if WORKTREE.is_dir():
        return
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, WORKTREE)
    run("git", "init", "--bare", str(REMOTE))
    run("git", "init", "--initial-branch=main", cwd=WORKTREE)
    run("git", "config", "user.name", "gitopsctr Kubernetes demo", cwd=WORKTREE)
    run("git", "config", "user.email", "demo@localhost", cwd=WORKTREE)
    run("git", "add", ".", cwd=WORKTREE)
    run("git", "commit", "-m", "Initialize Kubernetes demo", cwd=WORKTREE)
    run("git", "remote", "add", "origin", str(REMOTE), cwd=WORKTREE)
    run("git", "push", "--set-upstream", "origin", "main", cwd=WORKTREE)


def clean() -> None:
    if shutil.which("kind") is not None:
        run("kind", "delete", "cluster", "--name", CLUSTER_NAME, check=False)
    if STATE_ROOT.exists():
        shutil.rmtree(STATE_ROOT)
    print("Kubernetes demo cluster and state removed.")


def ensure_cluster() -> None:
    clusters = run("kind", "get", "clusters", capture=True).stdout.splitlines()
    if CLUSTER_NAME not in clusters:
        run("kind", "create", "cluster", "--name", CLUSTER_NAME, "--wait", "120s")


def controller(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        sys.executable,
        "-m",
        "gitopsctr",
        "--repository",
        str(WORKTREE),
        *args,
        cwd=WORKTREE,
        capture=capture,
    )


def deployment_heads() -> tuple[str, str]:
    def head(ref: str) -> str:
        return run(
            "git",
            "--git-dir",
            str(REMOTE),
            "rev-parse",
            f"refs/heads/{ref}",
            capture=True,
        ).stdout.strip()

    return head("deploy/dev"), head("observed/dev")


def verify_resource() -> None:
    message = run(
        "kubectl",
        "--context",
        KUBE_CONTEXT,
        "--namespace",
        "default",
        "get",
        "configmap",
        RESOURCE_NAME,
        "--output",
        "jsonpath={.data.message}",
        capture=True,
    ).stdout
    if message != "rendered and reconciled":
        raise RuntimeError(f"unexpected ConfigMap value: {message!r}")
    controller("verify", "--environment", "dev")


def converge(*, expect_clean: bool = False) -> None:
    result = controller(
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
    verify_resource()


def start() -> None:
    require_commands()
    prepare_repository()
    ensure_cluster()
    converge()
    print(f"Kubernetes demo is applied in context {KUBE_CONTEXT}.")
    print("Run 'mise run kubernetes-demo-clean' to remove all effects.")


def acceptance() -> None:
    clean()
    try:
        start()
        first_heads = deployment_heads()
        converge(expect_clean=True)
        second_heads = deployment_heads()
        if second_heads != first_heads:
            raise RuntimeError("clean Kubernetes convergence moved desired or observed refs")
        print(f"Acceptance passed: deploy/dev={second_heads[0][:12]} observed/dev={second_heads[1][:12]}")
    finally:
        clean()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "clean", "acceptance"), nargs="?", default="run")
    args = parser.parse_args()
    try:
        if args.operation == "clean":
            clean()
        elif args.operation == "acceptance":
            acceptance()
        else:
            start()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Kubernetes demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
