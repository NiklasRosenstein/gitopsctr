"""Logical plane selection through a controlled authority snapshot service."""

from __future__ import annotations

import re
from pathlib import Path

from gitopsctr.adapters.filesystem import FilesystemWorkspaceAdapter
from gitopsctr.adapters.git.remote_authority import ControlledGitPublicationAuthority
from gitopsctr.application.model import ChannelId, SnapshotId
from gitopsctr.application.workspace import InMemoryWorkspace
from gitopsctr.errors import OperationError
from gitopsctr.formats import Project, parse_document_bytes, validate_project_document
from gitopsctr.resource_model import ResourcePlane
from gitopsctr.workspace_inspection import WorkspacePlaneProvider, WorkspaceSnapshot

_GIT_COMMIT = re.compile(r"[0-9a-f]{40}$")


class ControlledGitWorkspacePlaneProvider(WorkspacePlaneProvider):
    """Select exact desired/observed workspaces from one authenticated authority."""

    def __init__(self, repository_root: Path, authority: ControlledGitPublicationAuthority) -> None:
        self._source = FilesystemWorkspaceAdapter().read(repository_root, excluded_top_level=frozenset((".git",)))
        self._authority = authority
        self._snapshots: dict[tuple[ResourcePlane, str, str | None], WorkspaceSnapshot] = {}

    def close(self) -> None:
        """The application owns the shared authority session."""

    def source(self) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            ResourcePlane.SOURCE,
            None,
            None,
            None,
            self._source,
            self._source.entry_content_ids(),
        )

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
            raise ValueError("source inspection uses source(), not an authority channel")
        if not reference:
            raise ValueError("desired and observed snapshots require a channel")
        key = (plane, reference, revision)
        cached = self._snapshots.get(key)
        if cached is not None:
            if cached.revision is None and not allow_missing:
                raise OperationError(f"{plane} channel {reference!r} does not exist")
            return cached
        channel = ChannelId(reference)
        head = self._authority.resolve_head(channel)
        if head.snapshot_id is None:
            if revision is not None or not allow_missing:
                raise OperationError(f"{plane} channel {reference!r} does not exist")
            empty = InMemoryWorkspace(mutable=False)
            result = WorkspaceSnapshot(plane, reference, None, None, empty, empty.entry_content_ids())
        else:
            selected = head.snapshot_id
            if revision is not None:
                if _GIT_COMMIT.fullmatch(revision) is None:
                    raise OperationError(f"requested revision is not part of {reference} history")
                selected = SnapshotId(f"git-commit:{revision}")
                if not self._authority.is_ancestor_snapshot(selected, head.snapshot_id):
                    raise OperationError(f"requested revision is not part of {reference} history")
            view = self._authority.open_snapshot(selected)
            result = WorkspaceSnapshot(
                plane,
                reference,
                self._authority.revision_for_snapshot(selected),
                selected,
                view.workspace,
                view.workspace.entry_content_ids(),
            )
        self._snapshots[key] = result
        return result

    def _has_file(self, key: str) -> bool:
        try:
            self._source.read(key)
        except Exception:
            return False
        return True
