"""Explicitly rooted Git-backed desired and observed state storage."""

from __future__ import annotations

import os
import re
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
class GatedCandidate:
    """A candidate proven to be one commit directly on the target head."""

    revision: str
    target_revision: str
    parent: str


@dataclass(frozen=True)
class ControllerPin:
    """A controller-owned Git ref retaining one exact source revision."""

    name: str
    ref: str
    revision: str


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

    def create_controller_pin(self, name: str, revision: str) -> ControllerPin:
        """Create or retain a named pin without changing an existing revision.

        ``revision`` may be any locally resolvable commit expression, but the
        returned pin always contains its canonical object ID.  The remote pin
        is created with an ordinary push, so a concurrent creator cannot
        replace a pin with a different revision.
        """

        pin_ref = self._controller_pin_ref(name)
        resolved_revision = self._resolve_commit(revision)
        existing_revision = self._remote_ref_revision(pin_ref)
        if existing_revision is not None:
            if existing_revision != resolved_revision:
                raise OperationError(
                    f"controller pin {name!r} already points to {existing_revision}, "
                    f"not requested revision {resolved_revision}"
                )
            return ControllerPin(name, pin_ref, resolved_revision)

        pushed = self.git("push", "origin", f"{resolved_revision}:{pin_ref}", check=False)
        if pushed.returncode != 0:
            # Another actor may have won the create race.  Re-inspect the
            # remote and accept only the exact requested revision.
            existing_revision = self._remote_ref_revision(pin_ref)
            if existing_revision == resolved_revision:
                return ControllerPin(name, pin_ref, resolved_revision)
            if existing_revision is not None:
                raise OperationError(f"controller pin {name!r} was created at unexpected revision {existing_revision}")
            raise OperationError(pushed.stderr.strip() or f"could not create controller pin {name!r}")

        existing_revision = self._remote_ref_revision(pin_ref)
        if existing_revision != resolved_revision:
            raise OperationError(f"controller pin {name!r} was not retained at the requested revision")
        return ControllerPin(name, pin_ref, resolved_revision)

    def release_controller_pin(self, name: str, expected_revision: str) -> bool:
        """Release a pin only when its current remote revision matches exactly.

        Missing pins are already released and return ``False``.  A mismatched
        pin is never modified.  The delete uses the normal Git push protocol
        and is verified afterward; no force-push option is used.
        """

        pin_ref = self._controller_pin_ref(name)
        existing_revision = self._remote_ref_revision(pin_ref)
        if existing_revision is None:
            return False
        if existing_revision != expected_revision:
            raise OperationError(
                f"controller pin {name!r} is fenced at {existing_revision}, not expected revision {expected_revision}"
            )

        released = self.git("push", "origin", f":{pin_ref}", check=False)
        remaining_revision = self._remote_ref_revision(pin_ref)
        if remaining_revision is None:
            return True
        if remaining_revision != expected_revision:
            raise OperationError(
                f"controller pin {name!r} changed during release to unexpected revision {remaining_revision}"
            )
        raise OperationError(released.stderr.strip() or f"could not release controller pin {name!r}")

    def list_controller_pins(self) -> tuple[ControllerPin, ...]:
        """List controller-owned pins from the remote without mutating refs."""

        prefix = "refs/heads/gitopsctr/pins/"
        result = self.git("ls-remote", "--refs", "origin", f"{prefix}*", check=False)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or "could not inspect controller pins")
        pins: list[ControllerPin] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise OperationError("controller pin inspection returned an invalid result")
            revision, ref = fields
            if not re.fullmatch(r"[0-9a-f]{40}", revision) or not ref.startswith(prefix):
                raise OperationError("controller pin inspection returned an invalid ref")
            name = ref.removeprefix(prefix)
            self._controller_pin_ref(name)
            pins.append(ControllerPin(name, ref, revision))
        return tuple(sorted(pins, key=lambda pin: pin.name))

    def _controller_pin_ref(self, name: str) -> str:
        ref = f"refs/heads/gitopsctr/pins/{name}"
        if self.git("check-ref-format", ref, check=False).returncode != 0:
            raise OperationError(f"invalid controller pin name: {name!r}")
        return ref

    def _resolve_commit(self, revision: str) -> str:
        resolved = self.git("rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
        if resolved.returncode != 0:
            raise OperationError(f"revision {revision!r} is not a valid commit")
        return resolved.stdout.strip()

    def _remote_ref_revision(self, ref: str) -> str | None:
        result = self.git("ls-remote", "--exit-code", "--refs", "origin", ref, check=False)
        if result.returncode == 2:
            return None
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or f"could not inspect {ref}")
        lines = result.stdout.splitlines()
        if len(lines) != 1 or len(lines[0].split()) != 2:
            raise OperationError(f"remote ref inspection returned an invalid result for {ref}")
        return lines[0].split()[0]

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

    def verify_gated_candidate(self, candidate_revision: str | None, target_revision: str | None) -> GatedCandidate:
        """Verify the fail-closed commit shape required by a change-gated candidate.

        A valid candidate is exactly one commit whose only parent is the current target
        head.  Checking both the parent list and the revision range rejects roots,
        stale candidates, rebases, multi-commit proposals, and merge commits.
        """

        if not candidate_revision:
            raise OperationError("gated candidate is missing its head revision")
        if not target_revision:
            raise OperationError("gated candidate is missing the current target head revision")

        candidate = self.git("rev-parse", "--verify", f"{candidate_revision}^{{commit}}", check=False)
        if candidate.returncode != 0:
            raise OperationError("gated candidate head revision is missing or invalid")
        target = self.git("rev-parse", "--verify", f"{target_revision}^{{commit}}", check=False)
        if target.returncode != 0:
            raise OperationError("gated candidate target head revision is missing or invalid")
        resolved_candidate = candidate.stdout.strip()
        resolved_target = target.stdout.strip()

        parents = self.git("rev-list", "--parents", "-n", "1", resolved_candidate, check=False)
        if parents.returncode != 0:
            raise OperationError("gated candidate head commit cannot be inspected")
        parent_revisions = parents.stdout.split()
        if len(parent_revisions) != 2:
            raise OperationError(
                "gated candidate must contain exactly one controller commit with one parent; "
                "roots and merge candidates are rejected"
            )
        parent = parent_revisions[1]
        if parent != resolved_target:
            raise OperationError("gated candidate is stale or rebased against a different target head")

        count = self.git("rev-list", "--count", f"{resolved_target}..{resolved_candidate}", check=False)
        if count.returncode != 0 or count.stdout.strip() != "1":
            raise OperationError("gated candidate must contain exactly one commit after the target head")

        return GatedCandidate(resolved_candidate, resolved_target, parent)

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
