"""Git-backed infrastructure adapters."""

from gitopsctr.adapters.git.inspection import GitResourceInspector
from gitopsctr.adapters.git.snapshots import GitSnapshotReader

__all__ = ["GitResourceInspector", "GitSnapshotReader"]
