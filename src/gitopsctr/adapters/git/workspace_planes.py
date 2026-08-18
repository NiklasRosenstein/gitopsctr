"""Exact Git snapshot selection for workspace-backed read-only inspection."""

from __future__ import annotations

from pathlib import Path

from gitopsctr.adapters.filesystem import FilesystemWorkspaceAdapter
from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.application.snapshots import SnapshotNotFoundError
from gitopsctr.application.workspace import InMemoryWorkspace
from gitopsctr.errors import OperationError
from gitopsctr.formats import Project, parse_document_bytes, validate_project_document
from gitopsctr.resource_model import ResourcePlane
from gitopsctr.workspace_inspection import WorkspacePlaneProvider, WorkspaceSnapshot


class GitWorkspacePlaneProvider(WorkspacePlaneProvider):
    """Resolve Git selector hints to exact snapshots without claiming CAS fences."""

    def __init__(self, repository_root: Path, snapshot_reader: GitSnapshotReader) -> None:
        self._source = FilesystemWorkspaceAdapter().read(repository_root, excluded_top_level=frozenset((".git",)))
        self._snapshot_reader = snapshot_reader
        self._snapshots: dict[tuple[ResourcePlane, str, str | None], WorkspaceSnapshot] = {}

    def close(self) -> None:
        """The application service owns the shared snapshot-reader lifecycle."""

    def source(self) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(ResourcePlane.SOURCE, None, None, None, self._source, {})

    def project(self) -> Project:
        candidates = tuple(key for key in ("gitopsctr.yaml", "gitopsctr.yml", "gitopsctr.json") if self._has_file(key))
        if not candidates:
            raise OperationError("source tree has no Project configuration: gitopsctr.yaml")
        if len(candidates) != 1:
            raise OperationError("multiple Project configuration files exist: " + ", ".join(candidates))
        key = candidates[0]
        try:
            return validate_project_document(parse_document_bytes(self._source.read(key), Path(key)), Path(key))
        except Exception as exc:
            raise OperationError(str(exc)) from exc

    def snapshot(
        self,
        plane: ResourcePlane,
        reference: str,
        revision: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> WorkspaceSnapshot:
        if plane is ResourcePlane.SOURCE:
            raise ValueError("source inspection uses source(), not a Git ref")
        if not reference:
            raise ValueError("desired and observed snapshots require a ref")
        key = (plane, reference, revision)
        cached = self._snapshots.get(key)
        if cached is not None:
            if cached.revision is None and not allow_missing:
                raise OperationError(f"{plane} ref {reference!r} does not exist")
            return cached
        try:
            head_id = self._snapshot_reader.snapshot_id_for_revision(reference)
            snapshot_id = self._snapshot_reader.snapshot_id_for_revision(revision) if revision is not None else head_id
            if revision is not None and not self._snapshot_reader.is_ancestor_snapshot(snapshot_id, head_id):
                raise OperationError(f"requested revision is not part of {reference} history")
            view = self._snapshot_reader.open_snapshot(snapshot_id)
            selected = self._snapshot_reader.revision_for_snapshot(snapshot_id)
            result = WorkspaceSnapshot(
                plane,
                reference,
                selected,
                snapshot_id,
                view.workspace,
                {str(path): value for path, value in self._snapshot_reader.blob_ids_for_snapshot(snapshot_id).items()},
            )
        except SnapshotNotFoundError as exc:
            if revision is not None or not allow_missing:
                raise OperationError(f"{plane} ref {reference!r} does not exist") from exc
            result = WorkspaceSnapshot(plane, reference, None, None, InMemoryWorkspace(mutable=False), {})
        self._snapshots[key] = result
        return result

    def _has_file(self, key: str) -> bool:
        try:
            self._source.read(key)
        except Exception:
            return False
        return True


# Compatibility spelling for integrations created during Phase 3a.
GitWorkspacePlaneSession = GitWorkspacePlaneProvider
