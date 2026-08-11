"""Git-backed desired and observed state storage."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


PinClaimState = Literal["preparing", "active", "reaping"]


@dataclass(frozen=True)
class ControllerPinClaim:
    """A CAS-fenced claim for one controller-owned Stack pin."""

    environment: str
    stack_name: str
    uid: str
    pin_name: str
    pin_revision: str
    target_ref: str
    target_revision: str
    candidate_ref: str
    candidate_revision: str | None
    state: PinClaimState
    revision: str | None = None

    @property
    def ref(self) -> str:
        return f"gitopsctr/pin-claims/stacks/{self.environment}/{self.stack_name}/{self.uid}"

    def document(self) -> dict[str, object]:
        return {
            "schema": 1,
            "kind": "ControllerPinClaim",
            "environment": self.environment,
            "stackName": self.stack_name,
            "uid": self.uid,
            "pinName": self.pin_name,
            "pinRevision": self.pin_revision,
            "targetRef": self.target_ref,
            "targetRevision": self.target_revision,
            "candidateRef": self.candidate_ref,
            "candidateRevision": self.candidate_revision,
            "state": self.state,
        }

    @classmethod
    def from_document(cls, document: object, *, revision: str | None = None) -> ControllerPinClaim:
        if not isinstance(document, dict):
            raise OperationError("controller pin claim is not an object")
        expected = {
            "schema",
            "kind",
            "environment",
            "stackName",
            "uid",
            "pinName",
            "pinRevision",
            "targetRef",
            "targetRevision",
            "candidateRef",
            "candidateRevision",
            "state",
        }
        if set(document) != expected or document.get("schema") != 1 or document.get("kind") != "ControllerPinClaim":
            raise OperationError("controller pin claim has an invalid shape")
        values = {key: document[key] for key in expected - {"schema", "kind"}}
        if not all(
            isinstance(values[key], str) and values[key]
            for key in (
                "environment",
                "stackName",
                "uid",
                "pinName",
                "pinRevision",
                "targetRef",
                "targetRevision",
                "candidateRef",
                "state",
            )
        ):
            raise OperationError("controller pin claim has invalid string fields")
        candidate_revision = values["candidateRevision"]
        if candidate_revision is not None and not isinstance(candidate_revision, str):
            raise OperationError("controller pin claim has an invalid candidate revision")
        state = values["state"]
        if not isinstance(state, str) or state not in {"preparing", "active", "reaping"}:
            raise OperationError("controller pin claim has an invalid state")
        claim = cls(
            environment=values["environment"],
            stack_name=values["stackName"],
            uid=values["uid"],
            pin_name=values["pinName"],
            pin_revision=values["pinRevision"],
            target_ref=values["targetRef"],
            target_revision=values["targetRevision"],
            candidate_ref=values["candidateRef"],
            candidate_revision=candidate_revision,
            state=state,
            revision=revision,
        )
        if claim.pin_name != f"stacks/{claim.environment}/{claim.stack_name}/{claim.uid}":
            raise OperationError("controller pin claim does not match its Stack identity")
        if not re.fullmatch(r"[0-9a-f]{40}", claim.pin_revision) or not re.fullmatch(
            r"[0-9a-f]{40}", claim.target_revision
        ):
            raise OperationError("controller pin claim has an invalid revision")
        if claim.candidate_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", claim.candidate_revision):
            raise OperationError("controller pin claim has an invalid candidate revision")
        if claim.revision is not None and not re.fullmatch(r"[0-9a-f]{40}", claim.revision):
            raise OperationError("controller pin claim has an invalid claim revision")
        return claim


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
        """Create a named pin or return the existing pin.

        Resolve ``revision`` locally. The returned pin contains its object ID.
        A concurrent creator cannot replace the pin with another revision.
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
        """Release a pin only when its remote revision matches.

        A missing pin is an idempotent no-op. A mismatched pin is not modified.
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

    def list_remote_refs(self) -> tuple[GitRefSnapshot, ...]:
        """List remote branch heads without changing local or remote state."""

        prefix = "refs/heads/"
        result = self.git("ls-remote", "--refs", "origin", f"{prefix}*", check=False)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or "could not inspect remote refs")
        refs: list[GitRefSnapshot] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise OperationError("remote ref inspection returned an invalid result")
            revision, ref = fields
            if not re.fullmatch(r"[0-9a-f]{40}", revision) or not ref.startswith(prefix):
                raise OperationError("remote ref inspection returned an invalid result")
            refs.append(GitRefSnapshot(ref.removeprefix(prefix), revision))
        return tuple(sorted(refs, key=lambda snapshot: snapshot.ref))

    def read_controller_pin_claim(self, name: str) -> ControllerPinClaim | None:
        """Read one claim, retaining its remote commit as the CAS revision."""

        ref = self._controller_pin_claim_ref(name)
        snapshot = self._remote_ref_snapshot(ref)
        if snapshot.revision is None:
            return None
        remote_ref = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
        fetched = self.git("fetch", "--no-tags", "origin", remote_ref, check=False)
        if fetched.returncode != 0:
            raise OperationError(fetched.stderr.strip() or f"could not fetch controller pin claim {name!r}")
        result = self.git("show", f"{snapshot.revision}:claim.json", check=False)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or f"controller pin claim {name!r} is unreadable")
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OperationError(f"controller pin claim {name!r} is not valid JSON") from exc
        claim = ControllerPinClaim.from_document(document, revision=snapshot.revision)
        if claim.ref != ref.removeprefix("refs/heads/"):
            raise OperationError(f"controller pin claim {name!r} has an invalid identity")
        return claim

    def list_controller_pin_claims(self) -> tuple[ControllerPinClaim, ...]:
        """List and validate all Stack pin claims."""

        prefix = "gitopsctr/pin-claims/stacks/"
        claims: list[ControllerPinClaim] = []
        for snapshot in self.list_remote_refs():
            if not snapshot.ref.startswith(prefix):
                continue
            claim = self.read_controller_pin_claim(snapshot.ref.removeprefix("gitopsctr/pin-claims/"))
            if claim is None:
                raise OperationError(f"controller pin claim {snapshot.ref!r} disappeared during inspection")
            claims.append(claim)
        return tuple(sorted(claims, key=lambda claim: claim.ref))

    def create_controller_pin_claim(self, claim: ControllerPinClaim) -> ControllerPinClaim:
        """Create an exact claim, or return the identical existing claim."""

        self._validate_controller_pin_claim(claim)
        existing = self.read_controller_pin_claim(claim.ref.removeprefix("gitopsctr/pin-claims/"))
        if existing is not None:
            if existing.document() != claim.document():
                raise OperationError(f"controller pin claim {claim.ref!r} already exists with different contents")
            return existing
        try:
            return self._publish_controller_pin_claim(claim, None)
        except subprocess.CalledProcessError as exc:
            existing = self.read_controller_pin_claim(claim.ref.removeprefix("gitopsctr/pin-claims/"))
            if existing is not None and existing.document() == claim.document():
                return existing
            raise OperationError(f"controller pin claim {claim.ref!r} changed during creation") from exc

    def update_controller_pin_claim(self, claim: ControllerPinClaim, expected_revision: str) -> ControllerPinClaim:
        """Advance a claim only from the exact expected commit."""

        self._validate_controller_pin_claim(claim)
        current = self.read_controller_pin_claim(claim.ref.removeprefix("gitopsctr/pin-claims/"))
        if current is None or current.revision != expected_revision:
            raise OperationError(f"controller pin claim {claim.ref!r} changed before update")
        try:
            return self._publish_controller_pin_claim(claim, expected_revision)
        except subprocess.CalledProcessError as exc:
            raise OperationError(f"controller pin claim {claim.ref!r} changed during update") from exc

    def delete_controller_pin_claim(self, name: str, expected_revision: str) -> bool:
        """Delete a claim only while its remote commit matches the fence."""

        ref = self._controller_pin_claim_ref(name)
        current = self._remote_ref_snapshot(ref)
        if current.revision is None:
            return False
        if current.revision != expected_revision:
            raise OperationError(f"controller pin claim {name!r} changed before deletion")
        result = self.git(
            "push",
            f"--force-with-lease={ref if ref.startswith('refs/heads/') else f'refs/heads/{ref}'}:{expected_revision}",
            "origin",
            f":{ref if ref.startswith('refs/heads/') else f'refs/heads/{ref}'}",
            check=False,
        )
        remaining = self._remote_ref_snapshot(ref).revision
        if remaining is None:
            return True
        if remaining != expected_revision:
            raise OperationError(f"controller pin claim {name!r} changed during deletion")
        raise OperationError(result.stderr.strip() or f"could not delete controller pin claim {name!r}")

    def _publish_controller_pin_claim(
        self,
        claim: ControllerPinClaim,
        parent: str | None,
    ) -> ControllerPinClaim:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claim.json"
            path.write_text(json.dumps(claim.document(), sort_keys=True, separators=(",", ":")) + "\n")
            published = self.publish(claim.ref, Path(directory), parent, f"Update controller pin claim {claim.ref}")
        return ControllerPinClaim.from_document(claim.document(), revision=published.revision)

    def _validate_controller_pin_claim(self, claim: ControllerPinClaim) -> None:
        ControllerPinClaim.from_document(claim.document())
        self._controller_pin_claim_ref(claim.ref.removeprefix("gitopsctr/pin-claims/"))

    def _controller_pin_claim_ref(self, name: str) -> str:
        ref = f"refs/heads/gitopsctr/pin-claims/{name}"
        if self.git("check-ref-format", ref, check=False).returncode != 0:
            raise OperationError(f"invalid controller pin claim name: {name!r}")
        return ref

    def _remote_ref_snapshot(self, ref: str) -> GitRefSnapshot:
        remote_ref = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
        result = self.git("ls-remote", "--exit-code", "--refs", "origin", remote_ref, check=False)
        if result.returncode == 2:
            return GitRefSnapshot(ref, None)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or f"could not inspect {ref}")
        lines = result.stdout.splitlines()
        if len(lines) != 1 or len(lines[0].split()) != 2:
            raise OperationError(f"remote ref inspection returned an invalid result for {ref}")
        revision, remote_ref = lines[0].split()
        if remote_ref != (ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}") or not re.fullmatch(
            r"[0-9a-f]{40}", revision
        ):
            raise OperationError(f"remote ref inspection returned an invalid result for {ref}")
        return GitRefSnapshot(ref, revision)

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
        """Verify the commit shape required by a gated candidate.

        A valid candidate is one commit whose only parent is the current target
        head. The parent and revision checks reject roots, stale candidates,
        rebases, multi-commit proposals, and merge commits.
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
