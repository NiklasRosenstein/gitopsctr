"""Local-Git acquisition and durable retention for typed source snapshots."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from gitopsctr.adapters.git.snapshots import GitSnapshotEntryError, GitSnapshotReader
from gitopsctr.application.model import (
    ContentId,
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
    _issue_retained_source,
)
from gitopsctr.application.snapshots import SnapshotNotFoundError, SnapshotReadError
from gitopsctr.application.sources import (
    RetainedSourceLocator,
    SourceError,
    SourceNotFoundError,
    SourceRepository,
    SourceRequest,
    SourceRetentionError,
    SourceSnapshot,
    copied_source_snapshot,
    same_source_payload,
)
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceCapabilities, WorkspaceEntry, WorkspaceEntryKind
from gitopsctr.errors import OperationError
from gitopsctr.git_local import DulwichLocalRepository, GitRepositoryError

_SOURCE_SNAPSHOT_PREFIX = "git-source:"
_GIT_SNAPSHOT_PREFIX = "git-commit:"
_RETENTION_FILENAME = "gitopsctr-source-retention-v1.json"
_RETENTION_LOCK_FILENAME = "gitopsctr-source-retention-v1.lock"


@dataclass(slots=True)
class GitSourceRetentionStore:
    """An independently owned durable store for retained logical source payloads.

    ``retention_root`` belongs to the operation/target side, never to the
    mutable source repository.  A private child directory, random store
    identity, random handles, atomic replacement, and an advisory process
    lock prevent cross-store recovery and keep concurrent lifecycle operations
    deterministic.
    """

    retention_root: Path
    _thread_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        """Durably copy and bind one exact source snapshot under a fresh handle."""

        with self._locked():
            store = self._load()
            store_id = RetentionStoreId(store["store_id"])
            records = store["records"]
            handle = RetainedSourceHandle(f"git-retained:{secrets.token_urlsafe(32)}")
            while handle.value in records:
                handle = RetainedSourceHandle(f"git-retained:{secrets.token_urlsafe(32)}")
            retained = _issue_retained_source(handle, store_id, source.source_snapshot_id, source.content_id)
            records[handle.value] = _source_record(source)
            self._write(store)
            return retained

    def retain_for_request(self, source: SourceSnapshot, request_token: str) -> RetainedSource:
        """Idempotently retain one host-authenticated remote request."""

        if not isinstance(source, SourceSnapshot):
            raise TypeError("source must be a SourceSnapshot")
        if (
            not isinstance(request_token, str)
            or len(request_token) != 64
            or any(character not in "0123456789abcdef" for character in request_token)
        ):
            raise ValueError("source retention request token must be one private canonical digest")
        with self._locked():
            store = self._load()
            store_id = RetentionStoreId(store["store_id"])
            records = store["records"]
            handle = RetainedSourceHandle(f"git-retained:request-{request_token}")
            record = records.get(handle.value)
            expected = _source_record(source)
            if record is not None:
                if record != expected:
                    raise SourceRetentionError("source retention request token was already bound to other content")
                return _issue_retained_source(handle, store_id, source.source_snapshot_id, source.content_id)
            retained = _issue_retained_source(handle, store_id, source.source_snapshot_id, source.content_id)
            records[handle.value] = expected
            self._write(store)
            return retained

    def recover(self, retained: RetainedSource, source_id: SourceId) -> SourceSnapshot:
        """Recover a retained payload without opening the source repository."""

        with self._locked():
            store = self._load()
            record = self._validated_record(store, _locator_from_retained(retained), source_id)
            return _source_from_record(record)

    def release(self, retained: RetainedSource, source_id: SourceId) -> None:
        """Atomically remove one exact record; repeated release fails closed."""

        with self._locked():
            store = self._load()
            self._validated_record(store, _locator_from_retained(retained), source_id)
            del store["records"][retained.handle.value]
            self._write(store)

    def reissue(self, locator: RetainedSourceLocator, source_id: SourceId) -> RetainedSource:
        """Validate persisted locator evidence and issue a fresh capability."""

        with self._locked():
            store = self._load()
            source = _source_from_record(self._validated_record(store, locator, source_id))
            return _issue_retained_source(
                locator.handle,
                locator.retention_store_id,
                source.source_snapshot_id,
                source.content_id,
            )

    def retained_snapshot(self, source_snapshot_id: SourceSnapshotId) -> tuple[RetainedSource, SourceSnapshot] | None:
        """Recover one already-retained exact snapshot without consulting its source.

        Multiple publication owners may retain the same immutable snapshot under
        different handles.  They are interchangeable only when their copied
        payloads have the same exact ContentId; divergent records fail closed.
        """

        if not isinstance(source_snapshot_id, SourceSnapshotId):
            raise TypeError("source_snapshot_id must be a SourceSnapshotId")
        with self._locked():
            store = self._load()
            matches: list[tuple[str, SourceSnapshot]] = []
            for handle, record in store["records"].items():
                source = _source_from_record(record)
                if source.source_snapshot_id == source_snapshot_id:
                    matches.append((handle, source))
            if not matches:
                return None
            content_ids = {source.content_id for _, source in matches}
            if len(content_ids) != 1:
                raise SourceRetentionError("retained source snapshot has inconsistent copied payloads")
            handle, source = min(matches, key=lambda item: item[0])
            retained = _issue_retained_source(
                RetainedSourceHandle(handle),
                RetentionStoreId(store["store_id"]),
                source.source_snapshot_id,
                source.content_id,
            )
            return retained, source

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize in-process and cross-process read-modify-write operations."""

        storage_root = self._storage_root()
        lock_path = storage_root / _RETENTION_LOCK_FILENAME
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        with self._thread_lock, os.fdopen(os.open(lock_path, flags, 0o600), "a+b") as lock_file:
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _storage_root(self) -> Path:
        if not self.retention_root.is_dir():
            raise SourceError("independent retention root cannot be opened")
        root = self.retention_root / "gitopsctr-source-retention"
        try:
            root.mkdir(mode=0o700, exist_ok=True)
            metadata = root.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("retention storage is not a private directory")
            os.chmod(root, 0o700)
            return root
        except (OSError, ValueError) as exc:
            raise SourceError("independent retention storage cannot be opened") from exc

    def _load(self) -> dict[str, Any]:
        path = self._storage_root() / _RETENTION_FILENAME
        if not path.exists():
            return {"version": 1, "store_id": f"git-retention-store:{secrets.token_urlsafe(24)}", "records": {}}
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("retention store is not a regular file")
            value = json.loads(path.read_text())
            if (
                not isinstance(value, dict)
                or value.get("version") != 1
                or not isinstance(value.get("store_id"), str)
                or not isinstance(value.get("records"), dict)
            ):
                raise ValueError("retention store has an unsupported shape")
            RetentionStoreId(value["store_id"])
            return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise SourceError("local Git retention storage is corrupted or unreadable") from exc

    def _write(self, store: dict[str, Any]) -> None:
        root = self._storage_root()
        path = root / _RETENTION_FILENAME
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, delete=False) as temporary:
                os.fchmod(temporary.fileno(), 0o600)
                json.dump(store, temporary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise SourceError("local Git retention storage cannot be written") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _validated_record(store: dict[str, Any], locator: RetainedSourceLocator, source_id: SourceId) -> dict[str, Any]:
        if not isinstance(locator, RetainedSourceLocator):
            raise TypeError("locator must be a RetainedSourceLocator")
        if locator.retention_store_id.value != store["store_id"]:
            raise SourceRetentionError("retained source belongs to a different retention store")
        try:
            record = store["records"][locator.handle.value]
            source = _source_from_record(record)
        except (KeyError, SourceError) as exc:
            raise SourceRetentionError("retained source handle is unknown or has been released") from exc
        if (
            source.source_snapshot_id.source_id != source_id
            or source.source_snapshot_id != locator.source_snapshot_id
            or source.content_id != locator.content_id
        ):
            raise SourceRetentionError("retained source does not match its issued store, snapshot, and content")
        return record


@dataclass(slots=True)
class GitSourceRepository(SourceRepository):
    """Resolve local Git selectors into exact commits and retain logical copies."""

    source_id: SourceId
    repository: DulwichLocalRepository
    retention_root: Path
    retention_store: GitSourceRetentionStore | None = None
    _resolved: dict[SourceSnapshotId, SourceSnapshot] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, SourceId):
            raise TypeError("source_id must be a SourceId")
        if not isinstance(self.repository, DulwichLocalRepository):
            raise TypeError("repository must be a DulwichLocalRepository")
        if not isinstance(self.retention_root, Path):
            raise TypeError("retention_root must be a Path")
        try:
            canonical_retention_root = self.retention_root.resolve()
            canonical_source_root = self.repository.root.resolve()
            canonical_retention_root.relative_to(canonical_source_root)
        except ValueError:
            pass
        else:
            raise ValueError("retention_root must be independently owned outside the source repository")
        if self.retention_store is None:
            self.retention_store = GitSourceRetentionStore(self.retention_root)
        if not isinstance(self.retention_store, GitSourceRetentionStore):
            raise TypeError("retention_store must be a GitSourceRetentionStore")
        if self.retention_store.retention_root.resolve() != canonical_retention_root:
            raise ValueError("retention_store root must exactly match the independently owned retention_root")

    @classmethod
    def from_path(cls, source_id: SourceId, path: Path, retention_root: Path) -> GitSourceRepository:
        """Construct source and independently owned retention boundaries."""

        return cls(source_id, DulwichLocalRepository(path), retention_root)

    def close(self) -> None:
        """Release only the local Git repository handle; retention remains durable."""

        self.repository.close()

    def resolve(self, request: SourceRequest) -> SourceSnapshot:
        """Resolve a current, historical, or tag selector to one exact commit."""

        if not isinstance(request, SourceRequest):
            raise TypeError("request must be a SourceRequest")
        if request.source_id != self.source_id:
            raise SourceNotFoundError("source request was not issued for this Git source")
        try:
            revision = self.repository.resolve_commit(request.selector, "Git source selector cannot be resolved")
        except GitRepositoryError as exc:
            raise SourceError("local Git source repository cannot be opened") from exc
        except OperationError as exc:
            if self._selector_ref_is_present_but_unreadable(request.selector):
                raise SourceError("Git source selector resolves to a missing or corrupt object") from exc
            raise SourceNotFoundError("Git source selector cannot be resolved") from exc

        source_snapshot_id = SourceSnapshotId(self.source_id, SnapshotId(f"{_SOURCE_SNAPSHOT_PREFIX}{revision}"))
        with self._lock:
            existing = self._resolved.get(source_snapshot_id)
            if existing is not None:
                return copied_source_snapshot(existing)
        try:
            view = GitSnapshotReader(self.repository).open_snapshot(SnapshotId(f"{_GIT_SNAPSHOT_PREFIX}{revision}"))
        except GitSnapshotEntryError as exc:
            raise SourceError("Git source snapshot cannot be represented as a logical workspace") from exc
        except SnapshotNotFoundError as exc:
            raise SourceError("Git source object disappeared after selector resolution") from exc
        except SnapshotReadError as exc:
            raise SourceError("Git source snapshot cannot be read") from exc
        source = SourceSnapshot(source_snapshot_id, view.content_id, view.workspace)
        immutable = copied_source_snapshot(source)
        with self._lock:
            existing = self._resolved.setdefault(source_snapshot_id, immutable)
            if not same_source_payload(existing, immutable):
                raise SourceError("Git source snapshot identity resolved to inconsistent logical content")
            return copied_source_snapshot(existing)

    def retain(self, source: SourceSnapshot) -> RetainedSource:
        """Durably retain an adapter-issued exact source snapshot."""

        assert self.retention_store is not None
        return self.retention_store.retain(self._canonical_resolved(source))

    def recover(self, retained: RetainedSource) -> SourceSnapshot:
        """Recover a durable source copy without consulting Git or its refs."""

        assert self.retention_store is not None
        try:
            retained._validate()
        except (TypeError, ValueError) as exc:
            raise SourceRetentionError("retained source has no valid Git retention issuance") from exc
        return self.retention_store.recover(retained, retained.source_snapshot_id.source_id)

    def release(self, retained: RetainedSource) -> None:
        """Release only the exact store-issued retention value."""

        assert self.retention_store is not None
        try:
            retained._validate()
        except (TypeError, ValueError) as exc:
            raise SourceRetentionError("retained source has no valid Git retention issuance") from exc
        self.retention_store.release(retained, retained.source_snapshot_id.source_id)

    def reissue(self, locator: RetainedSourceLocator) -> RetainedSource:
        """Reissue a capability from untrusted persisted retention evidence."""

        assert self.retention_store is not None
        return self.retention_store.reissue(locator, locator.source_snapshot_id.source_id)

    def _canonical_resolved(self, source: SourceSnapshot) -> SourceSnapshot:
        if not isinstance(source, SourceSnapshot):
            raise TypeError("source must be a SourceSnapshot")
        if source.source_snapshot_id.source_id != self.source_id:
            raise SourceRetentionError("source snapshot belongs to a different source repository")
        with self._lock:
            canonical = self._resolved.get(source.source_snapshot_id)
        if canonical is None or not same_source_payload(canonical, source):
            raise SourceRetentionError("source snapshot was not issued by this Git source repository")
        return canonical

    def _selector_ref_is_present_but_unreadable(self, selector: str) -> bool:
        """Distinguish an absent selector from a ref pointing at a bad object."""

        candidates = (
            ("HEAD",)
            if selector == "HEAD"
            else (selector,)
            if selector.startswith("refs/")
            else (f"refs/heads/{selector}", f"refs/tags/{selector}", f"refs/remotes/origin/{selector}")
        )
        for candidate in candidates:
            try:
                if self.repository.ref_revision(candidate) is not None:
                    return False
            except GitRepositoryError as exc:
                raise SourceError("local Git source repository cannot be opened") from exc
            except OperationError:
                return True
        return False


def _source_record(source: SourceSnapshot) -> dict[str, object]:
    return {
        "capabilities": {
            "executable_mode": source.workspace.capabilities.executable_mode,
            "explicit_directories": source.workspace.capabilities.explicit_directories,
            "symlinks": source.workspace.capabilities.symlinks,
        },
        "content_id": source.content_id.value,
        "entries": [
            {
                "content": base64.b64encode(entry.content).decode("ascii") if entry.content is not None else None,
                "executable": entry.executable,
                "key": entry.key,
                "kind": entry.kind.value,
                "target": entry.target,
            }
            for entry in source.workspace.list_entries()
        ],
        "snapshot_id": source.source_snapshot_id.snapshot_id.value,
        "source_id": source.source_snapshot_id.source_id.value,
    }


def _locator_from_retained(retained: RetainedSource) -> RetainedSourceLocator:
    try:
        return RetainedSourceLocator.from_retained(retained)
    except TypeError as exc:
        raise SourceRetentionError("retained source is not an issued retention value") from exc


def _source_from_record(record: object) -> SourceSnapshot:
    try:
        if not isinstance(record, dict):
            raise ValueError("record is not an object")
        capabilities = record["capabilities"]
        entries = record["entries"]
        if not isinstance(capabilities, dict) or not isinstance(entries, list):
            raise ValueError("record has invalid workspace fields")
        workspace = InMemoryWorkspace(
            tuple(
                WorkspaceEntry(
                    entry["key"],
                    WorkspaceEntryKind(entry["kind"]),
                    base64.b64decode(entry["content"], validate=True) if entry["content"] is not None else None,
                    entry["executable"],
                    entry["target"],
                )
                for entry in entries
                if isinstance(entry, dict)
            ),
            capabilities=WorkspaceCapabilities(
                symlinks=capabilities["symlinks"],
                explicit_directories=capabilities["explicit_directories"],
                executable_mode=capabilities["executable_mode"],
            ),
            mutable=False,
        )
        if len(workspace.list_entries()) != len(entries):
            raise ValueError("record contains non-object entries")
        return SourceSnapshot(
            SourceSnapshotId(SourceId(record["source_id"]), SnapshotId(record["snapshot_id"])),
            ContentId(record["content_id"]),
            workspace,
        )
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise SourceError("retained Git source record is corrupted") from exc
