"""Typed, backend-neutral vocabulary for read-only resource inspection.

The command deliberately describes *what* to inspect and the requested
presentation shape.  It contains neither local paths nor Git values; the
default inspector adapter translates its snapshot/reference hints at the
infrastructure boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any


def _require_display_text(value: object, description: str) -> str:
    """Accept one CLI-originated label without imposing storage semantics."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{description} must be a non-empty string")
    if "\0" in value:
        raise ValueError(f"{description} must not contain NUL")
    return value


def _require_json_value(value: Any) -> None:
    """Reject values whose identity or behavior cannot cross the result port."""

    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if isfinite(value):
            return
        raise TypeError("inspection result document cannot contain a non-finite number")
    if isinstance(value, list):
        for item in value:
            _require_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("inspection result document object keys must be strings")
            _require_json_value(item)
        return
    raise TypeError(f"inspection result document contains non-JSON value {type(value).__name__}")


class InspectionOutputFormat(StrEnum):
    """Presentation form requested by an incoming inspection adapter."""

    TABLE = "table"
    WIDE = "wide"
    YAML = "yaml"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class InspectionFilter:
    """One catalog-defined logical identity constraint."""

    name: str
    value: str

    def __post_init__(self) -> None:
        _require_display_text(self.name, "inspection filter name")
        _require_display_text(self.value, "inspection filter value")


@dataclass(frozen=True, slots=True)
class ResourceInspectionCommand:
    """A backend-neutral request to inspect persisted product resources.

    ``desired_*`` and ``observed_*`` are opaque selection hints.  The
    application does not infer whether a particular adapter realizes them as a
    ref, revision, timestamp, or another immutable-snapshot selector.
    """

    selector: str
    name: str | None = None
    environment: str | None = None
    all_environments: bool = False
    desired_reference: str | None = None
    desired_snapshot: str | None = None
    observed_reference: str | None = None
    observed_snapshot: str | None = None
    output: InspectionOutputFormat = InspectionOutputFormat.TABLE
    filters: tuple[InspectionFilter, ...] = ()
    artifact: str | None = None
    artifacts: bool = False
    as_list: bool = False

    def __post_init__(self) -> None:
        _require_display_text(self.selector, "inspection selector")
        for value, description in (
            (self.name, "inspection resource name"),
            (self.environment, "inspection environment"),
            (self.desired_reference, "desired snapshot reference"),
            (self.desired_snapshot, "desired snapshot selector"),
            (self.observed_reference, "observed snapshot reference"),
            (self.observed_snapshot, "observed snapshot selector"),
            (self.artifact, "inspection artifact name"),
        ):
            if value is not None:
                _require_display_text(value, description)
        if type(self.all_environments) is not bool:
            raise TypeError("all_environments must be a bool")
        if type(self.artifacts) is not bool:
            raise TypeError("artifacts must be a bool")
        if type(self.as_list) is not bool:
            raise TypeError("as_list must be a bool")
        if not isinstance(self.output, InspectionOutputFormat):
            raise TypeError("output must be an InspectionOutputFormat")
        if not isinstance(self.filters, tuple) or any(not isinstance(item, InspectionFilter) for item in self.filters):
            raise TypeError("filters must be a tuple of InspectionFilter values")
        names = tuple(item.name for item in self.filters)
        if len(set(names)) != len(names):
            raise ValueError("inspection filters must not repeat a name")


@dataclass(frozen=True, slots=True)
class InspectionTable:
    """One CLI-renderable table with no dependency on an output device."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    heading: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.headers, tuple):
            raise TypeError("inspection table headers must be a tuple")
        if not self.headers or any(not isinstance(value, str) or not value for value in self.headers):
            raise ValueError("inspection table headers must be non-empty strings")
        if self.heading is not None:
            _require_display_text(self.heading, "inspection table heading")
        if not isinstance(self.rows, tuple):
            raise TypeError("inspection table rows must be a tuple")
        for row in self.rows:
            if not isinstance(row, tuple) or len(row) != len(self.headers):
                raise ValueError("inspection table rows must match the header width")
            if any(not isinstance(value, str) for value in row):
                raise TypeError("inspection table cell values must be strings")


@dataclass(frozen=True, slots=True, init=False)
class ResourceInspectionResult:
    """Structured inspection data for the incoming adapter to render.

    A document is serialized at the port boundary and decoded afresh for each
    reader.  That keeps adapter-owned objects out of the result and prevents a
    caller from mutating an adapter's retained document after the application
    returns.
    """

    tables: tuple[InspectionTable, ...]
    _document_json: str | None

    def __init__(self, tables: tuple[InspectionTable, ...] = (), document: Any | None = None) -> None:
        if not isinstance(tables, tuple) or any(not isinstance(table, InspectionTable) for table in tables):
            raise TypeError("tables must be a tuple of InspectionTable values")
        if tables and document is not None:
            raise ValueError("an inspection result cannot contain both tables and a document")
        serialized: str | None = None
        if document is not None:
            try:
                _require_json_value(document)
                serialized = json.dumps(document, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
                json.loads(serialized)
            except (TypeError, ValueError) as exc:
                raise TypeError("inspection result document must be JSON-compatible data") from exc
        object.__setattr__(self, "tables", tables)
        object.__setattr__(self, "_document_json", serialized)

    @property
    def document(self) -> Any | None:
        """Return an independent JSON value, never adapter-owned mutable data."""

        return json.loads(self._document_json) if self._document_json is not None else None

    @property
    def is_empty(self) -> bool:
        """Whether table inspection selected no renderable collection."""

        return not self.tables and self._document_json is None
