"""Read exact local Git commits as logical immutable snapshot workspaces."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gitopsctr.application.model import SnapshotId
from gitopsctr.application.snapshots import SnapshotNotFoundError, SnapshotReadError, SnapshotView
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceCapabilities, WorkspaceEntry, WorkspaceError
from gitopsctr.errors import OperationError
from gitopsctr.git_local import DulwichLocalRepository, GitRepositoryError, GitTreeContentError

_SNAPSHOT_PREFIX = "git-commit:"


class GitSnapshotEntryError(SnapshotReadError):
    """Raised when a local Git tree cannot be represented as a safe workspace."""


@dataclass(frozen=True, slots=True)
class GitSnapshotReader:
    """Open immutable commits without admitting Git or filesystem values upstream.

    ``SnapshotId`` values issued by this adapter use an internal prefix followed
    by a canonical commit ID.  Application callers only retain the opaque
    snapshot value; they never need a local path, ref, tree, or blob identity.
    This reader deliberately does not implement ``ChannelReader`` because a
    mutable Git ref alone cannot provide an ABA-safe incarnation observation.
    """

    repository: DulwichLocalRepository

    @classmethod
    def from_path(cls, path: Path) -> GitSnapshotReader:
        """Construct the adapter at the infrastructure boundary from a local path."""

        return cls(DulwichLocalRepository(path))

    def close(self) -> None:
        """Release the owned local repository handle; repeated calls are safe."""

        self.repository.close()

    def __enter__(self) -> GitSnapshotReader:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def snapshot_id_for_revision(self, revision: str) -> SnapshotId:
        """Issue an opaque snapshot identity for one exact resolved commit."""

        try:
            commit = self.repository.resolve_commit(revision, "Git revision cannot be opened as a snapshot")
        except GitRepositoryError as exc:
            raise SnapshotReadError("local Git snapshot repository cannot be opened") from exc
        except OperationError as exc:
            raise SnapshotNotFoundError("Git revision cannot be opened as a snapshot") from exc
        return SnapshotId(f"{_SNAPSHOT_PREFIX}{commit}")

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        """Open the exact commit encoded by an adapter-issued snapshot identity."""

        if not isinstance(snapshot_id, SnapshotId):
            raise TypeError("snapshot_id must be a SnapshotId")
        revision = self._revision_for(snapshot_id)
        try:
            entries = self.repository.tree_entries(revision)
        except GitRepositoryError as exc:
            raise SnapshotReadError("local Git snapshot repository cannot be opened") from exc
        except GitTreeContentError as exc:
            raise GitSnapshotEntryError(f"Git snapshot content cannot be represented safely: {snapshot_id}") from exc
        except OperationError as exc:
            raise SnapshotNotFoundError(f"Git snapshot does not exist: {snapshot_id}") from exc

        try:
            workspace_entries: list[WorkspaceEntry] = []
            for entry in entries:
                entry_type = stat.S_IFMT(entry.mode)
                if entry_type == stat.S_IFLNK:
                    raise GitSnapshotEntryError(f"Git snapshot contains an unsupported symbolic link: {entry.path!r}")
                if entry_type != stat.S_IFREG:
                    raise GitSnapshotEntryError(f"Git snapshot contains an unsupported entry: {entry.path!r}")
                if entry.data is None:
                    raise GitSnapshotEntryError(f"Git snapshot regular-file entry is not a blob: {entry.path!r}")
                workspace_entries.append(
                    WorkspaceEntry.file(entry.path, entry.data, executable=bool(entry.mode & 0o111))
                )
            workspace = InMemoryWorkspace(
                workspace_entries,
                capabilities=WorkspaceCapabilities(executable_mode=True),
                mutable=False,
            )
        except WorkspaceError as exc:
            raise GitSnapshotEntryError(f"Git snapshot contains an invalid logical workspace entry: {exc}") from exc
        return SnapshotView(snapshot_id, workspace.content_id, workspace)

    def revision_for_snapshot(self, snapshot_id: SnapshotId) -> str:
        """Return this adapter's exact revision spelling for one issued snapshot."""

        return self._revision_for(snapshot_id)

    def blob_ids_for_snapshot(self, snapshot_id: SnapshotId) -> dict[PurePosixPath, str]:
        """Return raw Git blob provenance for logical keys in one exact snapshot."""

        revision = self._revision_for(snapshot_id)
        try:
            return self.repository.blob_ids(revision)
        except GitRepositoryError as exc:
            raise SnapshotReadError("local Git snapshot repository cannot be opened") from exc
        except OperationError as exc:
            raise SnapshotNotFoundError(f"Git snapshot does not exist: {snapshot_id}") from exc

    def is_ancestor_snapshot(self, ancestor: SnapshotId, descendant: SnapshotId) -> bool:
        """Check immutable Git lineage for read-only historical selection only."""

        try:
            return self.repository.is_ancestor(self._revision_for(ancestor), self._revision_for(descendant))
        except GitRepositoryError as exc:
            raise SnapshotReadError("local Git snapshot repository cannot be opened") from exc
        except OperationError as exc:
            raise SnapshotNotFoundError("Git snapshot cannot be checked for lineage") from exc

    @staticmethod
    def _revision_for(snapshot_id: SnapshotId) -> str:
        if not snapshot_id.value.startswith(_SNAPSHOT_PREFIX):
            raise SnapshotNotFoundError("snapshot ID was not issued by the local Git snapshot reader")
        revision = snapshot_id.value.removeprefix(_SNAPSHOT_PREFIX)
        if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
            raise SnapshotNotFoundError("snapshot ID does not encode a canonical local Git commit")
        return revision
