"""Typed, backend-neutral vocabulary for environment reconciliation status."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{description} must be a non-empty string without NUL")
    return value


@dataclass(frozen=True, slots=True)
class StatusCommand:
    """Request one coherent read of an environment's reconciliation state."""

    environment: str
    desired_reference: str | None = None
    desired_snapshot: str | None = None
    observed_reference: str | None = None
    observed_snapshot: str | None = None
    unit: str | None = None
    verbose: bool = False

    def __post_init__(self) -> None:
        _text(self.environment, "status environment")
        for value, description in (
            (self.desired_reference, "desired status reference"),
            (self.desired_snapshot, "desired status snapshot"),
            (self.observed_reference, "observed status reference"),
            (self.observed_snapshot, "observed status snapshot"),
            (self.unit, "status unit selector"),
        ):
            if value is not None:
                _text(value, description)
        if type(self.verbose) is not bool:
            raise TypeError("status verbose must be a bool")


@dataclass(frozen=True, slots=True)
class StatusEntry:
    """One renderable reconciliation state with no adapter-owned objects."""

    name: str
    state: StatusState
    reason: str
    explanation: StatusExplanation | None = None

    def __post_init__(self) -> None:
        _text(self.name, "status entry name")
        if not isinstance(self.state, StatusState):
            raise TypeError("status entry state must be a StatusState")
        if not isinstance(self.reason, str):
            raise TypeError("status entry reason must be a string")
        if self.explanation is not None and not isinstance(self.explanation, StatusExplanation):
            raise TypeError("status entry explanation must be a StatusExplanation")


@dataclass(frozen=True, slots=True)
class StatusExplanation:
    """Closed evidence used by the incoming adapter to explain a READY Unit."""

    previous_desired_revision: str
    previous_source_revision: str | None
    current_source_revision: str | None
    causes: tuple[str, ...]
    commits: tuple[str, ...]
    files: tuple[str, ...]
    specification_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.previous_desired_revision, "previous desired revision")
        for value in (self.previous_source_revision, self.current_source_revision):
            if value is not None:
                _text(value, "source revision")
        for values in (self.causes, self.commits, self.files, self.specification_paths):
            if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
                raise TypeError("status explanation evidence must be tuples of strings")


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Closed result for one exact desired/observed snapshot selection."""

    environment: str
    desired_reference: str
    desired_revision: str | None
    observed_reference: str
    observed_revision: str | None
    entries: tuple[StatusEntry, ...]

    def __post_init__(self) -> None:
        _text(self.environment, "status result environment")
        _text(self.desired_reference, "status desired reference")
        _text(self.observed_reference, "status observed reference")
        for value, description in (
            (self.desired_revision, "status desired revision"),
            (self.observed_revision, "status observed revision"),
        ):
            if value is not None:
                _text(value, description)
        if not isinstance(self.entries, tuple) or any(not isinstance(item, StatusEntry) for item in self.entries):
            raise TypeError("status entries must be a tuple of StatusEntry values")


class StatusState(StrEnum):
    """Closed reconciliation states exposed by the status application port."""

    CLEAN = "CLEAN"
    READY = "READY"
    WAIT = "WAIT"
    MATERIALIZED = "MATERIALIZED"
