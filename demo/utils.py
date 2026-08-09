"""Shared support for isolated demo repositories and commands."""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


def run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=capture, env=env, input=input_text)


def require_commands(*names: str, installation_hint: str = "run 'mise install'") -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing required commands: {', '.join(missing)}; {installation_hint}")


def docker_platform() -> str:
    architecture = run("docker", "info", "--format", "{{.Architecture}}", capture=True).stdout.strip()
    platforms = {
        "amd64": "linux/amd64",
        "x86_64": "linux/amd64",
        "arm64": "linux/arm64",
        "aarch64": "linux/arm64",
    }
    try:
        return platforms[architecture]
    except KeyError as exc:
        raise RuntimeError(f"unsupported Docker architecture: {architecture or platform.machine()}") from exc


def remove_docker_images(*references: str) -> None:
    image_ids: set[str] = set()
    for reference in references:
        result = run(
            "docker",
            "image",
            "ls",
            "--quiet",
            "--filter",
            f"reference={reference}",
            check=False,
            capture=True,
        )
        image_ids.update(result.stdout.split())
    if image_ids:
        run("docker", "image", "rm", "--force", *sorted(image_ids))


@dataclass(frozen=True)
class RefHeads:
    desired: str
    observed: str


@dataclass(frozen=True)
class DemoRepository:
    template: Path
    state_root: Path
    worktree: Path
    remote: Path | str
    identity: str

    def prepare(self, configure: Callable[[], None] | None = None) -> None:
        if self.worktree.is_dir():
            return
        self.state_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.template, self.worktree)
        if configure is not None:
            configure()
        if isinstance(self.remote, Path):
            run("git", "init", "--bare", str(self.remote))
        run("git", "init", "--initial-branch=main", cwd=self.worktree)
        run("git", "config", "user.name", self.identity, cwd=self.worktree)
        run("git", "config", "user.email", "demo@localhost", cwd=self.worktree)
        # Demos must not inherit a developer's mandatory commit-signing policy:
        # their temporary identity has no signing key or agent.
        run("git", "config", "commit.gpgsign", "false", cwd=self.worktree)
        run("git", "add", ".", cwd=self.worktree)
        run("git", "commit", "-m", f"Initialize {self.identity}", cwd=self.worktree)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.worktree)
        run("git", "push", "--set-upstream", "origin", "main", cwd=self.worktree)

    def heads(self) -> RefHeads:
        def head(ref: str) -> str:
            if not isinstance(self.remote, Path):
                output = run("git", "ls-remote", self.remote, f"refs/heads/{ref}", capture=True).stdout.split()
                if len(output) != 2:
                    raise RuntimeError(f"remote {self.remote!r} has no {ref} head")
                return output[0]
            return run(
                "git",
                "--git-dir",
                str(self.remote),
                "rev-parse",
                f"refs/heads/{ref}",
                capture=True,
            ).stdout.strip()

        return RefHeads(
            desired=head("gitopsctr/desired/dev"),
            observed=head("gitopsctr/observed/dev"),
        )

    def clean(self) -> None:
        if self.state_root.exists():
            shutil.rmtree(self.state_root)
