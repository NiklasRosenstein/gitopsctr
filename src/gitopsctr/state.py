"""Git-backed desired and observed state storage."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from gitopsctr.errors import OperationError

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CREDENTIAL_URL_RE = re.compile(r"((?:[A-Za-z][A-Za-z0-9+.-]*):)//[^/@\s]+@")
_REPOSITORY_SCHEMES = frozenset(("file", "http", "https", "ssh", "git"))
_RESERVED_PUBLICATION_PREFIXES = frozenset(
    (
        "gitopsctr/pins",
        "gitopsctr/owners",
        "gitopsctr/locks",
    )
)


def _canonical_branch_ref(ref: str) -> str:
    """Validate a public branch ref and return its short spelling."""

    if not isinstance(ref, str) or not ref:
        raise OperationError("invalid publication ref")
    if ref.startswith("refs/heads/"):
        branch = ref.removeprefix("refs/heads/")
    elif ref.startswith("refs/"):
        raise OperationError(f"invalid publication ref: {ref!r}")
    else:
        branch = ref
    if not branch:
        raise OperationError(f"invalid publication ref: {ref!r}")
    if any(branch == prefix or branch.startswith(f"{prefix}/") for prefix in _RESERVED_PUBLICATION_PREFIXES):
        raise OperationError(f"publication ref is reserved for controller state: {ref!r}")
    if (
        subprocess.run(("git", "check-ref-format", f"refs/heads/{branch}"), check=False, capture_output=True).returncode
        != 0
    ):
        raise OperationError(f"invalid publication ref: {ref!r}")
    return branch


def canonical_publication_ref(ref: str) -> str:
    """Validate a public publication/candidate ref and return short spelling."""

    return _canonical_branch_ref(ref)


def _redact_credentials(value: str) -> str:
    """Remove URL userinfo from text emitted by Git or an operation error."""

    return _CREDENTIAL_URL_RE.sub(r"\1//", value)


def canonical_repository_identity(repository: str | Path, *, root: Path | None = None) -> str:
    """Return a stable repository identity that never contains credentials.

    The literal ``.`` is intentionally retained as the local-repository
    sentinel. Other paths are made absolute file identities, while URLs have
    userinfo removed and are normalized enough for equivalent credentials to
    share one retention namespace.
    """

    value = os.fspath(repository)
    if value == ".":
        return value
    if not value or any(character in value for character in "\r\n\x00"):
        raise OperationError("repository identity is empty or contains control characters")
    if value.startswith("-") or "::" in value or any(character in value for character in "?#"):
        raise OperationError("repository identity is not a safe local path or Git URL")

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme not in _REPOSITORY_SCHEMES or (scheme != "file" and not parsed.netloc):
            raise OperationError("repository uses an unsupported or remote-helper URL scheme")
        if parsed.query or parsed.fragment:
            raise OperationError("repository URL must not contain a query or fragment")
        if scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                raise OperationError("repository file URL must not contain a remote host")
            return _canonical_local_repository(unquote(parsed.path), root=root).as_uri()
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise OperationError("repository URL has an invalid host") from error
        if not hostname:
            raise OperationError("repository URL has no host")
        host = hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None:
            host = f"{host}:{port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((scheme, host, path, "", ""))

    scp_match = re.fullmatch(r"[^/@\s]+@([^/:\s]+):(.+)", value)
    if scp_match:
        return f"ssh://{scp_match.group(1).lower()}/{scp_match.group(2).lstrip('/')}"

    return _canonical_local_repository(value, root=root).as_uri()


def _canonical_local_repository(value: str, *, root: Path | None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (root if root is not None else Path.cwd()) / path
    return path.resolve(strict=False)


def _repository_transport(repository: str | Path, *, root: Path) -> str:
    """Return the process-only Git transport for a repository input."""

    value = os.fspath(repository)
    parsed = urlsplit(value)
    if parsed.scheme.lower() in _REPOSITORY_SCHEMES:
        return value
    if re.fullmatch(r"[^/@\s]+@([^/:\s]+):(.+)", value):
        return value
    return os.fspath(_canonical_local_repository(value, root=root))


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
class AcceptedDesiredTarget:
    """The exact desired ref and head accepted by the controller."""

    ref: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", _canonical_branch_ref(self.ref))
        if not isinstance(self.revision, str) or _COMMIT_RE.fullmatch(self.revision) is None:
            raise OperationError("accepted desired target revision is invalid")


@dataclass(frozen=True)
class ControllerPin:
    """A controller-owned Git ref retaining one exact source revision."""

    name: str
    ref: str
    revision: str


@dataclass(frozen=True)
class PublicationOwner:
    """One source-retaining owner for one exact publication."""

    name: str
    ref: str
    publication_ref: str
    publication_revision: str
    source_pin_name: str
    revision: str
    publication_marker: str | None = None


@dataclass(frozen=True)
class _PublicationOwnerCleanupObservation:
    """The exact remote heads used to decide and fence owner cleanup."""

    owner_revision: str | None
    publication_revision: str | None
    lock_revision: str | None
    canonical_revision: str | None
    accepted_target_revision: str | None


@dataclass(frozen=True)
class GitSourceRevision:
    """One source repository revision resolved to an immutable commit.

    ``repository`` is a credential-free identity.  The private transport is
    retained only for the current process so an authenticated source can be
    imported without ever becoming part of a document or a ref name.
    """

    repository: str
    ref: str
    revision: str
    local: bool = False
    _transport: str = field(default="", repr=False, compare=False)

    @property
    def identity(self) -> str:
        return self.repository


@dataclass(frozen=True)
class _SourceRef:
    name: str
    full_ref: str
    is_commit: bool = False


@dataclass(frozen=True)
class GitStateStore:
    root: Path
    author_name: str = "gitopsctr"
    author_email: str = "gitopsctr@users.noreply.github.com"
    clock: Callable[[], float] = time.time
    claim_expiry_seconds: float = 3600.0
    claim_grace_seconds: float = 900.0

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

    def _git_at(
        self,
        root: Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *args),
            check=check,
            text=True,
            env=env,
            cwd=root,
            capture_output=True,
        )

    @staticmethod
    def _source_ref(ref: str) -> _SourceRef:
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in "\r\n\x00?#"):
            raise OperationError("source ref is invalid")
        if "::" in ref:
            raise OperationError("source ref is invalid")
        if _COMMIT_RE.fullmatch(ref):
            return _SourceRef(ref, ref, is_commit=True)
        if ref == "HEAD":
            return _SourceRef(ref, ref)
        if ref.startswith("refs/"):
            name = ref
        else:
            name = f"refs/heads/{ref}"
        result = subprocess.run(
            ("git", "check-ref-format", name),
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise OperationError("source ref is invalid")
        return _SourceRef(ref, name)

    def _local_source_ref(self, ref: str) -> _SourceRef:
        """Resolve an unqualified source name to exactly one local ref."""

        source_ref = self._source_ref(ref)
        if source_ref.is_commit or source_ref.full_ref == "HEAD" or ref.startswith("refs/"):
            return source_ref
        matches = [
            candidate
            for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}")
            if self._git_at(self.root, "show-ref", "--verify", "--quiet", candidate, check=False).returncode == 0
        ]
        if len(matches) > 1:
            raise OperationError(f"source ref {ref!r} is ambiguous; qualify it as a branch or tag")
        if not matches:
            raise OperationError("source ref does not exist")
        return _SourceRef(ref, matches[0])

    def _remote_source_ref(self, root: Path, transport: str, ref: str) -> _SourceRef:
        """Resolve an unqualified source name against branch and tag refs."""

        source_ref = self._source_ref(ref)
        if source_ref.is_commit or source_ref.full_ref == "HEAD" or ref.startswith("refs/"):
            return source_ref
        matches: list[str] = []
        for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}"):
            result = self._git_at(root, "ls-remote", "--exit-code", "--refs", transport, candidate, check=False)
            if result.returncode == 0:
                fields = result.stdout.split()
                if len(fields) != 2 or fields[1] != candidate or not _COMMIT_RE.fullmatch(fields[0]):
                    raise OperationError("source ref inspection returned an invalid result")
                matches.append(candidate)
            elif result.returncode != 2:
                detail = _redact_credentials(result.stderr.strip())
                raise OperationError(detail or "could not inspect source ref")
        if len(matches) > 1:
            raise OperationError(f"source ref {ref!r} is ambiguous; qualify it as a branch or tag")
        if not matches:
            raise OperationError("source ref does not exist")
        return _SourceRef(ref, matches[0])

    @staticmethod
    def _commit_from(result: subprocess.CompletedProcess[str], message: str) -> str:
        revision = result.stdout.strip()
        if result.returncode != 0 or not _COMMIT_RE.fullmatch(revision):
            raise OperationError(message)
        return revision

    def _resolve_commit_at(self, root: Path, revision: str, message: str = "source revision is invalid") -> str:
        if not revision or revision.startswith("-"):
            raise OperationError(message)
        result = self._git_at(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
            check=False,
        )
        return self._commit_from(result, message)

    def _source_head_at(self, root: Path, ref: str) -> str:
        return self._resolve_commit_at(root, ref, "source ref does not exist")

    def _is_ancestor_at(self, root: Path, ancestor: str, descendant: str) -> bool:
        return self._git_at(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

    def resolve_source(self, repository: str | Path, ref: str, revision: str | None = None) -> GitSourceRevision:
        """Resolve a source ref once and return its exact commit.

        ``repository='.'`` reads the controller's local repository directly;
        every other repository is fetched into a disposable bare repository.
        The disposable repository is also what makes an external historical
        revision check independent of the controller's current object graph.
        """

        identity = canonical_repository_identity(repository, root=self.root)
        source_ref = self._local_source_ref(ref) if identity == "." else self._source_ref(ref)
        if identity == ".":
            head_ref = "HEAD" if source_ref.full_ref == "HEAD" else source_ref.full_ref
            head = self._source_head_at(self.root, head_ref)
            resolved = head if revision is None else self._resolve_commit_at(self.root, revision)
            if revision is not None and not self._is_ancestor_at(self.root, resolved, head):
                raise OperationError("requested source revision is not part of the source ref history")
            return GitSourceRevision(identity, source_ref.name, resolved, local=True, _transport=".")

        transport = _repository_transport(repository, root=self.root)
        with tempfile.TemporaryDirectory(prefix="gitopsctr-source-") as directory:
            temporary_root = Path(directory)
            initialized = self._git_at(temporary_root, "init", "--bare", check=False)
            if initialized.returncode != 0:
                raise OperationError("could not initialize temporary source repository")
            source_ref = self._remote_source_ref(temporary_root, transport, ref)
            remote_ref = source_ref.full_ref
            if source_ref.full_ref == "HEAD":
                head_result = self._git_at(
                    temporary_root, "ls-remote", "--symref", "--exit-code", transport, "HEAD", check=False
                )
                if head_result.returncode != 0:
                    detail = _redact_credentials(head_result.stderr.strip())
                    raise OperationError(detail or "source ref does not exist")
                symbolic = next(
                    (
                        fields[1]
                        for line in head_result.stdout.splitlines()
                        if (fields := line.split())[:1] == ["ref:"] and len(fields) == 3 and fields[2] == "HEAD"
                    ),
                    None,
                )
                remote_ref = symbolic or "HEAD"
            destination = "refs/remotes/source/resolved"
            fetch_spec = f"+{source_ref.full_ref if source_ref.is_commit else remote_ref}:{destination}"
            fetched = self._git_at(
                temporary_root,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                transport,
                fetch_spec,
                check=False,
            )
            if fetched.returncode != 0:
                if "couldn't find remote ref" in fetched.stderr.lower():
                    raise OperationError("source ref does not exist")
                detail = _redact_credentials(fetched.stderr.strip())
                raise OperationError(detail or "could not fetch source repository")
            head = self._source_head_at(temporary_root, destination)
            resolved = head if revision is None else self._resolve_commit_at(temporary_root, revision)
            if revision is not None and not self._is_ancestor_at(temporary_root, resolved, head):
                raise OperationError("requested source revision is not part of the source ref history")
            return GitSourceRevision(identity, source_ref.name, resolved, _transport=transport)

    def _local_ref_revision(self, ref: str) -> str | None:
        result = self.git("rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if result.returncode != 0:
            return None
        revision = result.stdout.strip()
        if not _COMMIT_RE.fullmatch(revision):
            raise OperationError("local ref is invalid")
        return revision

    def _has_origin(self) -> bool:
        return self.git("remote", "get-url", "origin", check=False).returncode == 0

    def fetch(self, ref: str) -> GitRefSnapshot:
        source_ref = self._source_ref(ref)
        if source_ref.full_ref == "HEAD" or source_ref.is_commit:
            raise OperationError("remote fetch requires a named branch or tag ref")
        remote_ref = source_ref.full_ref
        result = self.git("ls-remote", "--exit-code", "--refs", "origin", remote_ref, check=False)
        if result.returncode == 2:
            return GitRefSnapshot(ref, None)
        if result.returncode != 0:
            detail = _redact_credentials(result.stderr.strip())
            raise OperationError(detail or f"could not inspect {ref}")
        fields = result.stdout.split()
        if not fields or not _COMMIT_RE.fullmatch(fields[0]):
            raise OperationError(f"could not inspect {ref}")
        revision = fields[0]
        destination_name = source_ref.name.removeprefix("refs/heads/")
        if destination_name == source_ref.name:
            destination_name = f"gitopsctr/fetch/{hashlib.sha256(remote_ref.encode()).hexdigest()}"
        fetched = self.git("fetch", "origin", f"+{remote_ref}:refs/remotes/origin/{destination_name}", check=False)
        if fetched.returncode != 0:
            detail = _redact_credentials(fetched.stderr.strip())
            raise OperationError(detail or f"could not fetch {ref}")
        return GitRefSnapshot(ref, revision)

    def resolve(self, ref: str, revision: str | None = None) -> GitRefSnapshot:
        snapshot = self.fetch(ref)
        if snapshot.revision is None:
            raise OperationError(f"ref {ref!r} does not exist")
        if revision is None:
            resolved = snapshot.revision
        else:
            resolved_result = self.git(
                "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", check=False
            )
            resolved = self._commit_from(resolved_result, "requested revision is not a valid commit")
        if (
            revision is not None
            and self.git("merge-base", "--is-ancestor", resolved, snapshot.revision, check=False).returncode
        ):
            raise OperationError(f"requested revision is not part of {ref} history")
        return GitRefSnapshot(ref, resolved)

    @staticmethod
    def _publication_branch(ref: str) -> str:
        return _canonical_branch_ref(ref)

    @classmethod
    def publication_owner_name(cls, publication_ref: str, publication_revision: str, source_pin_name: str) -> str:
        """Encode the complete publication/source binding in a safe ref name."""

        branch = cls._publication_branch(publication_ref)
        if not _COMMIT_RE.fullmatch(publication_revision) or not cls._valid_source_pin_name(
            source_pin_name, source_pin_name.rsplit("/", 1)[-1]
        ):
            raise OperationError("invalid publication owner identity")
        encoded_ref = quote(branch, safe="")
        name = f"{encoded_ref}/{publication_revision}/{source_pin_name}"
        if (
            subprocess.run(
                ("git", "check-ref-format", f"refs/heads/gitopsctr/owners/{name}"),
                check=False,
                capture_output=True,
            ).returncode
            != 0
        ):
            raise OperationError("invalid publication owner ref")
        return name

    @staticmethod
    def _valid_source_pin_name(name: str, revision: str) -> bool:
        return (
            name.startswith("stack-templates/")
            and name.rsplit("/", 1)[-1] == revision
            and _COMMIT_RE.fullmatch(revision) is not None
            and "//" not in name
            and ".." not in name
        )

    @classmethod
    def _parse_publication_owner(cls, name: str, revision: str, ref: str) -> PublicationOwner:
        parts = name.split("/", 2)
        if len(parts) != 3 or not _COMMIT_RE.fullmatch(parts[1]):
            raise OperationError("publication owner inspection returned an invalid ref")
        try:
            publication_ref = unquote(parts[0])
        except ValueError as error:
            raise OperationError("publication owner inspection returned an invalid publication ref") from error
        if quote(publication_ref, safe="") != parts[0]:
            raise OperationError("publication owner inspection returned an invalid publication ref")
        source_pin_name = parts[2]
        if not cls._valid_source_pin_name(source_pin_name, revision):
            raise OperationError("publication owner inspection returned an invalid source identity")
        expected = cls.publication_owner_name(publication_ref, parts[1], source_pin_name)
        if expected != name:
            raise OperationError("publication owner inspection returned an invalid ref")
        return PublicationOwner(name, ref, publication_ref, parts[1], source_pin_name, revision)

    def _publication_owner_ref(self, publication_ref: str, publication_revision: str, source_pin_name: str) -> str:
        return f"refs/heads/gitopsctr/owners/{self.publication_owner_name(publication_ref, publication_revision, source_pin_name)}"

    @classmethod
    def publication_lock_name(cls, publication_ref: str) -> str:
        """Return the safe ref name for one publication's coordination fence."""

        branch = cls._publication_branch(publication_ref)
        name = quote(branch, safe="")
        if (
            subprocess.run(
                ("git", "check-ref-format", f"refs/heads/gitopsctr/locks/{name}"),
                check=False,
                capture_output=True,
            ).returncode
            != 0
        ):
            raise OperationError("invalid publication lock ref")
        return name

    def _publication_lock_ref(self, publication_ref: str) -> str:
        return f"refs/heads/gitopsctr/locks/{self.publication_lock_name(publication_ref)}"

    def _expected_publication_owners(
        self, publication_ref: str, publication_revision: str, source_pins: Mapping[str, str]
    ) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            sorted(
                (
                    source_pin_name,
                    self._publication_owner_ref(publication_ref, publication_revision, source_pin_name),
                    self._resolve_commit(source_revision),
                )
                for source_pin_name, source_revision in source_pins.items()
            )
        )

    def create_controller_pin(self, name: str, revision: str) -> ControllerPin:
        """Create a named pin or return the existing pin.

        Resolve ``revision`` locally. The returned pin contains its object ID.
        A concurrent creator cannot replace the pin with another revision.
        """

        return self.create_controller_pins({name: revision})[0]

    def create_controller_pins(self, revisions: Mapping[str, str]) -> tuple[ControllerPin, ...]:
        """Atomically retain a set of exact revisions under controller refs.

        Existing exact pins are idempotent. All requested commits and ref names
        are validated before the first remote mutation, and missing refs are
        created by one atomic push so a failed batch cannot leave a partial set.
        """

        requested = tuple(
            sorted(
                (
                    name,
                    self._controller_pin_ref(name),
                    self._resolve_commit(revision),
                )
                for name, revision in revisions.items()
            )
        )
        if not requested:
            return ()
        for _attempt in range(3):
            missing: list[tuple[str, str, str]] = []
            for name, pin_ref, revision in requested:
                existing_revision = self._remote_ref_revision(pin_ref)
                if existing_revision is None:
                    missing.append((name, pin_ref, revision))
                elif existing_revision != revision:
                    raise OperationError(
                        f"controller pin {name!r} already points to {existing_revision}, "
                        f"not requested revision {revision}"
                    )
            if not missing:
                return tuple(ControllerPin(name, pin_ref, revision) for name, pin_ref, revision in requested)

            pushed = self.git(
                "push",
                "--atomic",
                "origin",
                *(f"{revision}:{pin_ref}" for _name, pin_ref, revision in missing),
                check=False,
            )
            remaining = [
                (name, pin_ref, revision)
                for name, pin_ref, revision in requested
                if self._remote_ref_revision(pin_ref) != revision
            ]
            if not remaining:
                return tuple(ControllerPin(name, pin_ref, revision) for name, pin_ref, revision in requested)
            if pushed.returncode == 0:
                names = ", ".join(repr(name) for name, _pin_ref, _revision in remaining)
                raise OperationError(f"controller pins were not retained at the requested revisions: {names}")

        names = ", ".join(repr(name) for name, _pin_ref, _revision in remaining)
        raise OperationError(pushed.stderr.strip() or f"could not atomically create controller pins: {names}")

    def create_controller_pin_claims(
        self,
        revisions: Mapping[str, str],
        claim: str,
    ) -> tuple[ControllerPin, ...]:
        """Create unique, prepublication-only leases with an encoded timestamp."""

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", claim):
            raise OperationError(f"invalid controller pin claim: {claim!r}")
        created = int(self.clock())
        if created < 0:
            raise OperationError("claim clock returned a negative timestamp")
        lease = f"{created:020d}-{claim}-{uuid.uuid4().hex[:12]}"
        # A fresh runner may have only a publication owner retaining the
        # source object. Hydrate each exact named source before
        # create_controller_pins resolves the revision locally.
        for source_pin_name, revision in revisions.items():
            try:
                self.hydrate_source_revision(source_pin_name, revision)
            except OperationError as hydration_error:
                # The first owner for a source is created by a runner that
                # already has the source object locally. In that case there is
                # no prior retention ref to hydrate; preserve the normal
                # local-object path while still requiring hydration whenever
                # the object is otherwise absent.
                try:
                    self._resolve_commit(revision)
                except OperationError:
                    raise hydration_error from hydration_error
        names = {f"claims/{lease}/{name}": revision for name, revision in revisions.items()}
        return self.create_controller_pins(names)

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

        released = self.git(
            "push",
            f"--force-with-lease={pin_ref}:{expected_revision}",
            "origin",
            f":{pin_ref}",
            check=False,
        )
        remaining_revision = self._remote_ref_revision(pin_ref)
        if remaining_revision is None:
            return True
        if remaining_revision != expected_revision:
            raise OperationError(
                f"controller pin {name!r} changed during release to unexpected revision {remaining_revision}"
            )
        raise OperationError(released.stderr.strip() or f"could not release controller pin {name!r}")

    def release_publication_owner(
        self,
        owner: PublicationOwner,
        accepted_target: AcceptedDesiredTarget | None = None,
    ) -> bool:
        """Atomically release one orphan owner and its canonical source pin.

        The publication lock is a fence for the observation that the
        publication is absent, or for a stale gated candidate whose branch is
        retained. A publication recreation updates that fence in the same
        push as its owner refs, so this transaction fails closed if recreation
        happens after observation.
        """

        lock_ref = self._publication_lock_ref(owner.publication_ref)
        canonical_ref = self._controller_pin_ref(owner.source_pin_name)
        accepted_target_revision = (
            self._remote_ref_revision(accepted_target.ref) if accepted_target is not None else None
        )
        publication_revision = (
            accepted_target_revision
            if accepted_target is not None and owner.publication_ref == accepted_target.ref
            else self._remote_ref_revision(owner.publication_ref)
        )
        observation = _PublicationOwnerCleanupObservation(
            owner_revision=self._remote_ref_revision(owner.ref),
            publication_revision=publication_revision,
            lock_revision=self._remote_ref_revision(lock_ref),
            canonical_revision=self._remote_ref_revision(canonical_ref),
            accepted_target_revision=accepted_target_revision,
        )
        if (
            observation.owner_revision != owner.revision
            or observation.lock_revision is None
            or observation.canonical_revision != owner.revision
        ):
            return False

        if accepted_target is None:
            if observation.publication_revision is not None:
                return False
        else:
            if observation.accepted_target_revision != accepted_target.revision:
                return False
            if owner.publication_ref == accepted_target.ref:
                if observation.publication_revision == owner.publication_revision == accepted_target.revision:
                    return False
            elif observation.publication_revision == owner.publication_revision and self._is_live_candidate_revision(
                owner, accepted_target, observation.publication_revision, observation.accepted_target_revision
            ):
                return False

        owners = self.list_controller_publication_owners()
        other_publication_owners = tuple(
            candidate
            for candidate in owners
            if candidate.publication_ref == owner.publication_ref and candidate.ref != owner.ref
        )
        other_source_owners = tuple(
            candidate
            for candidate in owners
            if candidate.source_pin_name == owner.source_pin_name and candidate.ref != owner.ref
        )
        updates = [f":{owner.ref}"]
        leases = [f"--force-with-lease={owner.ref}:{owner.revision}"]
        if not other_source_owners:
            updates.append(f":{canonical_ref}")
            leases.append(f"--force-with-lease={canonical_ref}:{observation.canonical_revision}")

        publication_branch = self._publication_branch(owner.publication_ref)
        publication_head_ref = f"refs/heads/{publication_branch}"
        retained_publication = observation.publication_revision is not None
        if retained_publication:
            updates.append(f"{observation.publication_revision}:{publication_head_ref}")
            leases.append(f"--force-with-lease={publication_head_ref}:{observation.publication_revision}")
        else:
            updates.append(f":{publication_head_ref}")
            leases.append(f"--force-with-lease={publication_head_ref}:")
        if accepted_target is not None and owner.publication_ref != accepted_target.ref:
            target_branch = self._publication_branch(accepted_target.ref)
            updates.append(f"{accepted_target.revision}:refs/heads/{target_branch}")
            leases.append(f"--force-with-lease=refs/heads/{target_branch}:{accepted_target.revision}")

        if retained_publication or other_publication_owners:
            updates.append(f"{observation.lock_revision}:{lock_ref}")
        else:
            updates.append(f":{lock_ref}")
        leases.append(f"--force-with-lease={lock_ref}:{observation.lock_revision}")
        result = self.git(
            "push",
            "--atomic",
            *leases,
            "origin",
            *updates,
            check=False,
        )
        if self._remote_ref_revision(owner.ref) is None:
            return True
        raise OperationError(result.stderr.strip() or "could not release publication owner")

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

    def list_controller_publication_owners(self) -> tuple[PublicationOwner, ...]:
        """List and validate all durable publication-owner refs."""

        prefix = "refs/heads/gitopsctr/owners/"
        result = self.git("ls-remote", "--refs", "origin", f"{prefix}*", check=False)
        if result.returncode != 0:
            raise OperationError(result.stderr.strip() or "could not inspect publication owners")
        owners: list[PublicationOwner] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not _COMMIT_RE.fullmatch(fields[0]) or not fields[1].startswith(prefix):
                raise OperationError("publication owner inspection returned an invalid result")
            name = fields[1].removeprefix(prefix)
            owner = self._parse_publication_owner(name, fields[0], fields[1])
            marker = self._remote_ref_revision(self._publication_lock_ref(owner.publication_ref))
            owners.append(replace(owner, publication_marker=marker))
        return tuple(sorted(owners, key=lambda owner: owner.name))

    def verify_publication_owners(
        self,
        publication_ref: str,
        publication_revision: str,
        source_pins: Mapping[str, str],
    ) -> bool:
        """Require the exact owner set for one publication, fail closed on partial state."""

        expected = self._expected_publication_owners(publication_ref, publication_revision, source_pins)
        expected_by_ref = {pin_ref: revision for _name, pin_ref, revision in expected}
        owners = self.list_controller_publication_owners()
        matching = tuple(
            owner
            for owner in owners
            if owner.publication_ref == self._publication_branch(publication_ref)
            and owner.publication_revision == publication_revision
        )
        present = {owner.ref: owner.revision for owner in matching}
        if present == expected_by_ref:
            return self._remote_ref_revision(self._publication_lock_ref(publication_ref)) is not None
        if present:
            raise OperationError("publication owner set is partial or points to unexpected source revisions")
        return False

    def publication_owner_is_live(self, owner: PublicationOwner) -> bool:
        """Return true only while the owner still names the exact publication."""

        return self._remote_ref_snapshot(owner.publication_ref).revision == owner.publication_revision

    def publication_owner_is_live_candidate(
        self,
        owner: PublicationOwner,
        accepted_target: AcceptedDesiredTarget,
    ) -> bool:
        """Return whether an owner still protects a current gated proposal.

        Candidate identity is deliberately based on the exact publication
        ref/revision and the commit parent fence, not on a candidate-ref
        naming convention. A proposal remains actionable only while its sole
        parent is the exact accepted desired head.
        """

        if owner.publication_ref == accepted_target.ref:
            return False
        publication_revision = self._remote_ref_revision(owner.publication_ref)
        accepted_target_revision = self._remote_ref_revision(accepted_target.ref)
        return self._is_live_candidate_revision(owner, accepted_target, publication_revision, accepted_target_revision)

    def _is_live_candidate_revision(
        self,
        owner: PublicationOwner,
        accepted_target: AcceptedDesiredTarget,
        publication_revision: str | None,
        accepted_target_revision: str | None,
    ) -> bool:
        """Check candidate commit shape from already observed remote heads."""

        if owner.publication_ref == accepted_target.ref:
            return False
        if accepted_target_revision != accepted_target.revision:
            return False
        candidate = self.fetch(owner.publication_ref)
        if candidate.revision != publication_revision or candidate.revision != owner.publication_revision:
            return False
        parents = self.git("rev-list", "--parents", "-n", "1", owner.publication_revision, check=False)
        if parents.returncode != 0:
            raise OperationError("gated candidate head commit cannot be inspected")
        parent_revisions = parents.stdout.split()
        return len(parent_revisions) == 2 and parent_revisions[1] == accepted_target.revision

    def hydrate_source_revision(self, source_pin_name: str, revision: str) -> str:
        """Hydrate an exact source through canonical, owner, or live claim ownership."""

        if not self._valid_source_pin_name(source_pin_name, revision):
            raise OperationError("invalid StackTemplate source pin identity")

        def fetch_exact(pin_ref: str) -> str | None:
            if self._remote_ref_revision(pin_ref) != revision:
                return None
            local_ref = f"refs/remotes/origin/gitopsctr/source/{hashlib.sha256(pin_ref.encode()).hexdigest()}"
            fetched = self.git(
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                f"+{pin_ref}:{local_ref}",
                check=False,
            )
            if fetched.returncode == 0 and self._local_ref_revision(local_ref) == revision:
                return local_ref
            return None

        canonical = fetch_exact(self._controller_pin_ref(source_pin_name))
        if canonical is not None:
            return canonical

        candidates: list[str] = []
        for owner in self.list_controller_publication_owners():
            if owner.source_pin_name == source_pin_name and owner.revision == revision:
                candidates.append(owner.ref)
        for pin in self.list_controller_pins():
            if not pin.name.startswith("claims/") or not self._valid_source_claim_name(pin.name, revision):
                continue
            if "/".join(pin.name.split("/")[2:]) == source_pin_name:
                created = self._claim_created_at(pin.name)
                if (
                    created is not None
                    and self.clock() <= created + self.claim_expiry_seconds + self.claim_grace_seconds
                ):
                    candidates.append(pin.ref)
        for pin_ref in candidates:
            local_ref = fetch_exact(pin_ref)
            if local_ref is not None:
                return local_ref
        raise OperationError("StackTemplate source ownership is missing or points to an unexpected revision")

    @staticmethod
    def _claim_created_at(name: str) -> int | None:
        parts = name.split("/")
        if len(parts) < 4 or parts[0] != "claims":
            return None
        match = re.fullmatch(r"([0-9]{20})-[a-z0-9][a-z0-9-]{0,31}-[0-9a-f]{12}", parts[1])
        return int(match.group(1)) if match else None

    @classmethod
    def _valid_source_claim_name(cls, name: str, revision: str) -> bool:
        parts = name.split("/")
        return (
            len(parts) >= 4
            and cls._claim_created_at(name) is not None
            and cls._valid_source_pin_name("/".join(parts[2:]), revision)
        )

    def reap_expired_controller_pin_claims(
        self,
        *,
        now: float | None = None,
        expiry_seconds: float | None = None,
        grace_seconds: float | None = None,
    ) -> tuple[ControllerPin, ...]:
        """Compare-and-delete only claims past expiry plus a conservative grace."""

        current = self.clock() if now is None else now
        expiry = self.claim_expiry_seconds if expiry_seconds is None else expiry_seconds
        grace = self.claim_grace_seconds if grace_seconds is None else grace_seconds
        if expiry < 0 or grace < 0:
            raise OperationError("claim expiry and grace must be non-negative")
        reaped: list[ControllerPin] = []
        for claim in self.list_controller_pins():
            created = self._claim_created_at(claim.name)
            if created is None or not self._valid_source_claim_name(claim.name, claim.revision):
                continue
            if current < created + expiry + grace:
                continue
            if self.release_controller_pin(claim.name, claim.revision):
                reaped.append(claim)
        return tuple(reaped)

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

    def _remote_ref_snapshot(self, ref: str) -> GitRefSnapshot:
        remote_ref = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
        result = self.git("ls-remote", "--exit-code", "--refs", "origin", remote_ref, check=False)
        if result.returncode == 2:
            return GitRefSnapshot(ref, None)
        if result.returncode != 0:
            detail = _redact_credentials(result.stderr.strip())
            raise OperationError(detail or f"could not inspect {ref}")
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
        if not revision or revision.startswith("-") or any(c in revision for c in "\r\n\x00"):
            raise OperationError("revision is not a valid commit")
        resolved = self.git("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", check=False)
        if resolved.returncode != 0:
            if _COMMIT_RE.fullmatch(revision):
                self._hydrate_revision_from_canonical_pin(revision)
                resolved = self.git("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", check=False)
            if resolved.returncode != 0:
                raise OperationError("revision is not a valid commit")
        return self._commit_from(resolved, "revision is not a valid commit")

    def _hydrate_revision_from_canonical_pin(self, revision: str) -> bool:
        """Fetch one exact object through an existing canonical or claim pin."""

        if not self._has_origin() or not _COMMIT_RE.fullmatch(revision):
            return False
        prefix = "refs/heads/gitopsctr/pins/"
        result = self.git("ls-remote", "--refs", "origin", f"{prefix}*", check=False)
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] != revision or not fields[1].startswith(prefix):
                continue
            name = fields[1].removeprefix(prefix)
            if name.startswith("claims/") and not self._valid_source_claim_name(name, revision):
                continue
            local_ref = f"refs/remotes/origin/gitopsctr/pins/{name}"
            fetched = self.git(
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                f"+{fields[1]}:{local_ref}",
                check=False,
            )
            if fetched.returncode == 0 and self._local_ref_revision(local_ref) == revision:
                return True
        return False

    def _remote_ref_revision(self, ref: str) -> str | None:
        result = self.git("ls-remote", "--exit-code", "--refs", "origin", ref, check=False)
        if result.returncode == 2:
            return None
        if result.returncode != 0:
            detail = _redact_credentials(result.stderr.strip())
            raise OperationError(detail or f"could not inspect {ref}")
        lines = result.stdout.splitlines()
        if len(lines) != 1 or len(lines[0].split()) != 2:
            raise OperationError(f"remote ref inspection returned an invalid result for {ref}")
        return lines[0].split()[0]

    def hydrate_controller_pin(self, name: str, revision: str) -> None:
        """Fetch one exact canonical controller pin into the local object store.

        This is deliberately read-only with respect to the state remote.  It
        is used when a carried desired document needs its immutable source
        object on a fresh runner.
        """

        if not _COMMIT_RE.fullmatch(revision):
            raise OperationError("controller pin revision is invalid")
        pin_ref = self._controller_pin_ref(name)
        if self._remote_ref_revision(pin_ref) != revision:
            raise OperationError("controller pin does not retain the requested revision")
        local_ref = f"refs/remotes/origin/gitopsctr/pins/{name}"
        fetched = self.git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "origin",
            f"+{pin_ref}:{local_ref}",
            check=False,
        )
        if fetched.returncode != 0 or self._local_ref_revision(local_ref) != revision:
            raise OperationError("could not hydrate the requested controller pin")

    def materialize_source(self, source: GitSourceRevision, output: Path) -> None:
        """Materialize an exact external source without creating a durable ref.

        The temporary local ref is removed on every path.  Durable source
        ownership is established later by the StackTemplate pin transaction.
        """

        if not _COMMIT_RE.fullmatch(source.revision):
            raise OperationError("source revision is invalid")
        if output.exists() and any(output.iterdir()):
            raise OperationError(f"output directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        temporary_ref = f"refs/remotes/gitopsctr/materialize/{uuid.uuid4().hex}"
        fetched_ref = False
        try:
            available_locally = False
            try:
                available_locally = self._resolve_commit(source.revision) == source.revision
            except OperationError:
                pass
            if source.local and not available_locally:
                raise OperationError("source revision is not available locally")
            if not source.local and not available_locally:
                fetched = self.git(
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    source._transport or source.repository,
                    f"+{source.revision}:{temporary_ref}",
                    check=False,
                )
                fetched_ref = True
                if fetched.returncode != 0 or self._local_ref_revision(temporary_ref) != source.revision:
                    detail = _redact_credentials(fetched.stderr.strip())
                    raise OperationError(detail or "could not materialize source revision")
            archive = subprocess.run(
                ("git", "archive", "--format=tar", source.revision),
                check=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
            ).stdout
            with tempfile.TemporaryFile() as stream:
                stream.write(archive)
                stream.seek(0)
                with tarfile.open(fileobj=stream, mode="r:") as tar:
                    tar.extractall(output, filter="data")
        finally:
            if fetched_ref:
                self.git("update-ref", "-d", temporary_ref, check=False)

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

    def _tree_for_directory(self, directory: Path) -> str:
        """Write a deterministic Git tree for a candidate directory."""

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
            return self.git("write-tree", env=identity).stdout.strip()

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

    def publish(
        self,
        ref: str,
        directory: Path,
        parent: str | None,
        message: str,
        source_pins: Mapping[str, str] | None = None,
        *,
        expected_publication_head: str | None,
    ) -> PublishedTree:
        """Create and atomically publish a tree plus its source owners.

        ``expected_publication_head`` is the caller-authorized head from the
        already-validated publication snapshot. ``None`` explicitly means the
        publication ref was expected to be absent. It is never read here to
        establish authorization; the publication update is fenced to exactly
        this value.
        """

        publication_ref = self._publication_branch(ref)
        if expected_publication_head is not None and not _COMMIT_RE.fullmatch(expected_publication_head):
            raise OperationError("expected publication head is invalid")
        tree = self._tree_for_directory(directory)
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
        owner_updates = self._expected_publication_owners(publication_ref, revision, source_pins or {})
        lock_ref = self._publication_lock_ref(publication_ref)
        publication_head_ref = f"refs/heads/{publication_ref}"
        previous_lock = self._remote_ref_revision(lock_ref)
        # A marker is deliberately a new commit even when the publication
        # tree/revision is recreated. The marker, rather than the publication
        # object ID, is the finalization fence.
        marker_tree = self.git("mktree", input_text="").stdout.strip()
        marker = self.git(
            "commit-tree",
            marker_tree,
            input_text=f"publication fence {uuid.uuid4().hex}\n",
            env=identity,
        ).stdout.strip()
        updates = [f"{revision}:refs/heads/{publication_ref}"] + [
            f"{source_revision}:{owner_ref}" for _name, owner_ref, source_revision in owner_updates
        ]
        # Keep lock refs only for publications that carry source ownership, or
        # when a previously owned publication is being recreated. This avoids
        # producing unrelated lock refs for ordinary publications while still
        # fencing an old owner during an ownership-dropping recreation.
        if source_pins or previous_lock is not None:
            updates.append(f"{marker}:{lock_ref}")
        push_args = ["push", "--atomic"]
        if source_pins or previous_lock is not None:
            push_args.append(f"--force-with-lease={lock_ref}:{previous_lock or ''}")
        # Fence every publication to the caller-authorized snapshot, including
        # an expected-absent head. An internal observation here would turn a
        # concurrent ref change into authorization.
        push_args.append(f"--force-with-lease={publication_head_ref}:{expected_publication_head or ''}")
        push_args.extend(("origin", *updates))
        try:
            self.git(*push_args)
        except BaseException:
            try:
                published = self.verify_published_tree(ref, directory, parent)
                if published is not None and source_pins:
                    if not self.verify_publication_owners(ref, published.revision, source_pins):
                        published = None
            except BaseException:
                # Preserve the push error. Callers must treat an inspection
                # failure as ambiguous and retain ownership.
                raise
            if published is not None:
                return published
            raise
        return PublishedTree(ref, revision, parent)

    def verify_published_tree_with_owners(
        self,
        ref: str,
        directory: Path,
        parent: str | None,
        source_pins: Mapping[str, str],
    ) -> PublishedTree | None:
        """Verify exact publication content and its complete owner set."""

        published = self.verify_published_tree(ref, directory, parent)
        owners = self.list_controller_publication_owners()
        publication_ref = self._publication_branch(ref)
        if published is None:
            if any(owner.publication_ref == publication_ref for owner in owners):
                raise OperationError("publication owner exists without the exact publication")
            return None
        if not self.verify_publication_owners(ref, published.revision, source_pins):
            raise OperationError("publication exists without its complete owner set")
        return published

    def verify_published_tree(
        self,
        ref: str,
        directory: Path,
        parent: str | None,
    ) -> PublishedTree | None:
        """Return a publication only when the remote ref has the exact tree.

        This is intentionally conservative: a remote ref that exists but has
        a different parent or tree is treated as an unrelated publication.
        Callers can therefore retain attempt ownership on ambiguous errors
        without mistaking a concurrent ref update for their own result.
        """

        snapshot = self._remote_ref_snapshot(ref)
        if snapshot.revision is None:
            return None
        revision = snapshot.revision
        try:
            resolved = self._resolve_commit(revision)
        except OperationError:
            local_ref = f"refs/remotes/origin/gitopsctr/verify/{hashlib.sha256(ref.encode()).hexdigest()}"
            fetched = self.git(
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                f"+refs/heads/{ref}:{local_ref}",
                check=False,
            )
            if fetched.returncode != 0:
                raise OperationError("could not verify the published remote ref") from None
            resolved = self._local_ref_revision(local_ref)
            if resolved is None:
                raise OperationError("published remote ref has no local commit object") from None
        parents = self.git("rev-list", "--parents", "-n", "1", resolved, check=False)
        if parents.returncode != 0:
            return None
        parent_revisions = parents.stdout.split()
        actual_parent = parent_revisions[1] if len(parent_revisions) == 2 else None
        if actual_parent != parent:
            return None
        tree = self.git("rev-parse", f"{resolved}^{{tree}}", check=False)
        if tree.returncode != 0 or tree.stdout.strip() != self._tree_for_directory(directory):
            return None
        return PublishedTree(ref, resolved, parent)
