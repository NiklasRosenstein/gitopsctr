"""Cached, immutable views of source, desired, and observed Git planes."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gitopsctr.errors import OperationError
from gitopsctr.resource_model import ResourcePlane
from gitopsctr.state import GitStateStore


@dataclass(frozen=True)
class PlaneSnapshot:
    """One materialized plane with the provenance needed for inspection."""

    plane: ResourcePlane
    root: Path
    ref: str | None
    revision: str | None
    blob_ids: dict[PurePosixPath, str]


class PlaneRepositorySession:
    """Resolve and materialize each requested Git snapshot at most once.

    The session owns its temporary trees.  Callers must keep it open while
    consuming records whose provenance points into those trees.
    """

    def __init__(self, repository_root: Path, state_store: GitStateStore | None = None) -> None:
        self.repository_root = repository_root.resolve()
        self.state_store = state_store or GitStateStore(self.repository_root)
        self._temporary = tempfile.TemporaryDirectory(prefix="gitopsctr-inventory-")
        self._requests: dict[tuple[ResourcePlane, str, str | None], PlaneSnapshot] = {}
        self._materialized: dict[tuple[ResourcePlane, str], PlaneSnapshot] = {}

    def __enter__(self) -> PlaneRepositorySession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._temporary.cleanup()

    def source(self) -> PlaneSnapshot:
        """Return the live source working tree.

        Source inspection deliberately includes uncommitted authoring changes;
        desired and observed inspection always use immutable Git snapshots.
        """

        return PlaneSnapshot(ResourcePlane.SOURCE, self.repository_root, None, None, {})

    def snapshot(
        self,
        plane: ResourcePlane,
        ref: str,
        revision: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> PlaneSnapshot:
        if plane is ResourcePlane.SOURCE:
            raise ValueError("source inspection uses source(), not a Git ref")
        if not ref:
            raise ValueError("desired and observed snapshots require a ref")
        request_key = (plane, ref, revision)
        cached = self._requests.get(request_key)
        if cached is not None:
            if cached.revision is None and not allow_missing:
                raise OperationError(f"{plane} ref {ref!r} does not exist")
            return cached

        if revision is not None:
            selected = self.state_store.resolve(ref, revision).revision
            assert selected is not None
        else:
            head = self.state_store.fetch(ref)
            if head.revision is None:
                empty = Path(self._temporary.name) / f"missing-{len(self._requests)}"
                empty.mkdir()
                result = PlaneSnapshot(plane, empty, ref, None, {})
                self._requests[request_key] = result
                if not allow_missing:
                    raise OperationError(f"{plane} ref {ref!r} does not exist")
                return result
            selected = head.revision
        materialized_key = (plane, selected)
        result = self._materialized.get(materialized_key)
        if result is None:
            target = Path(self._temporary.name) / f"{plane}-{selected}"
            self.state_store.materialize(selected, target)
            result = PlaneSnapshot(plane, target, ref, selected, self._blob_ids(selected))
            self._materialized[materialized_key] = result
        elif result.ref != ref:
            result = PlaneSnapshot(plane, result.root, ref, result.revision, result.blob_ids)
        self._requests[request_key] = result
        return result

    def _blob_ids(self, revision: str) -> dict[PurePosixPath, str]:
        listed = self.state_store.git("ls-tree", "-r", revision)
        result: dict[PurePosixPath, str] = {}
        for line in listed.stdout.splitlines():
            metadata, separator, path = line.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or fields[1] != "blob":
                raise OperationError(f"could not read Git tree for revision {revision!r}")
            result[PurePosixPath(path)] = fields[2]
        return result
