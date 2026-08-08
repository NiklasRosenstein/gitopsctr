"""Explicitly rooted Git-backed desired and observed state storage."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gitopsctr.errors import OperationError


@dataclass(frozen=True)
class GitRefSnapshot:
    ref: str
    revision: str | None


@dataclass(frozen=True)
class PublishedTree:
    ref: str
    revision: str
    parent: str | None


@dataclass(frozen=True)
class GitStateStore:
    root: Path
    author_name: str = "gitopsctr"
    author_email: str = "gitopsctr@users.noreply.github.com"

    def git(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *args),
            check=check,
            text=True,
            input=input_text,
            env=env,
            cwd=self.root,
            capture_output=True,
        )

    def fetch(self, ref: str) -> GitRefSnapshot:
        remote_ref = f"refs/heads/{ref}"
        result = self.git("ls-remote", "--exit-code", "--heads", "origin", remote_ref, check=False)
        if result.returncode == 2:
            return GitRefSnapshot(ref, None)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or f"could not inspect {ref}")
        revision = result.stdout.split()[0]
        self.git("fetch", "origin", f"{remote_ref}:refs/remotes/origin/{ref}")
        return GitRefSnapshot(ref, revision)

    def resolve(self, ref: str, revision: str | None = None) -> GitRefSnapshot:
        snapshot = self.fetch(ref)
        if snapshot.revision is None:
            raise OperationError(f"ref {ref!r} does not exist")
        resolved = (
            snapshot.revision if revision is None else self.git("rev-parse", f"{revision}^{{commit}}").stdout.strip()
        )
        if (
            revision is not None
            and self.git("merge-base", "--is-ancestor", resolved, snapshot.revision, check=False).returncode
        ):
            raise OperationError(f"requested revision is not part of {ref} history")
        return GitRefSnapshot(ref, resolved)

    def materialize(self, revision: str, output: Path) -> None:
        if output.exists() and any(output.iterdir()):
            raise OperationError(f"output directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ("git", "archive", "--format=tar", revision),
            check=True,
            cwd=self.root,
            stdout=subprocess.PIPE,
        ).stdout
        with tempfile.TemporaryFile() as stream:
            stream.write(archive)
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as tar:
                tar.extractall(output, filter="data")

    def publish(self, ref: str, directory: Path, parent: str | None, message: str) -> PublishedTree:
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if not files:
            raise OperationError(f"tree is empty: {directory}")
        with tempfile.TemporaryDirectory() as temporary_directory:
            identity = os.environ | {"GIT_INDEX_FILE": str(Path(temporary_directory) / "index")}
            self.git("read-tree", "--empty", env=identity)
            for path in files:
                if path.is_symlink():
                    raise OperationError(f"tree contains a symbolic link: {path}")
                relative = path.relative_to(directory).as_posix()
                blob = self.git("hash-object", "-w", str(path)).stdout.strip()
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", env=identity)
            tree = self.git("write-tree", env=identity).stdout.strip()
        commit_args = ["commit-tree", tree]
        if parent:
            commit_args.extend(("-p", parent))
        identity = os.environ | {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
        }
        revision = self.git(*commit_args, input_text=f"{message}\n", env=identity).stdout.strip()
        self.git("push", "origin", f"{revision}:refs/heads/{ref}")
        return PublishedTree(ref, revision, parent)
