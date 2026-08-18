"""Typed, backend-neutral vocabulary for dependency inspection."""

from __future__ import annotations

from dataclasses import dataclass

from gitopsctr.application.model import SnapshotId


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{description} must be a non-empty string without NUL")
    return value


@dataclass(frozen=True, slots=True)
class DependencyCommand:
    """Request the dependency graph for one immutable authored source view."""

    environment: str
    source_selector: str = "HEAD"
    units: tuple[str, ...] = ()
    depth: int | None = None

    def __post_init__(self) -> None:
        _text(self.environment, "dependency environment")
        _text(self.source_selector, "dependency source selector")
        if not isinstance(self.units, tuple) or any(not isinstance(value, str) or not value for value in self.units):
            raise TypeError("dependency units must be a tuple of non-empty strings")
        if self.depth is not None and (type(self.depth) is not int or self.depth < 0):
            raise ValueError("dependency depth must be zero or a positive integer")


@dataclass(frozen=True, slots=True)
class DependencyEntry:
    """One stable, display-ready graph node and its direct dependencies."""

    name: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.name, "dependency entry name")
        if not isinstance(self.dependencies, tuple) or any(
            not isinstance(value, str) or not value for value in self.dependencies
        ):
            raise TypeError("dependency entry dependencies must be a tuple of non-empty strings")


@dataclass(frozen=True, slots=True)
class DependencyResult:
    """Closed dependency graph selected from one exact source snapshot."""

    environment: str
    source_revision: str
    source_snapshot: SnapshotId
    targets: tuple[str, ...]
    entries: tuple[DependencyEntry, ...]

    def __post_init__(self) -> None:
        _text(self.environment, "dependency result environment")
        _text(self.source_revision, "dependency source revision")
        if not isinstance(self.source_snapshot, SnapshotId):
            raise TypeError("dependency result source snapshot must be a SnapshotId")
        if not isinstance(self.targets, tuple) or any(
            not isinstance(value, str) or not value for value in self.targets
        ):
            raise TypeError("dependency targets must be a tuple of non-empty strings")
        if not isinstance(self.entries, tuple) or any(not isinstance(value, DependencyEntry) for value in self.entries):
            raise TypeError("dependency entries must be a tuple of DependencyEntry values")
        names = tuple(entry.name for entry in self.entries)
        if len(set(names)) != len(names):
            raise ValueError("dependency result entries must have unique names")
        available = set(names)
        if not set(self.targets).issubset(available):
            raise ValueError("dependency result targets must appear in entries")
        if any(not set(entry.dependencies).issubset(available) for entry in self.entries):
            raise ValueError("dependency result dependencies must appear in entries")
