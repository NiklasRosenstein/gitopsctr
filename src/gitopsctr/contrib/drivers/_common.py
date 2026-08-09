"""Shared helpers for contributed drivers."""

from __future__ import annotations

from collections.abc import Mapping

from gitopsctr.document import JsonObject
from gitopsctr.driver import DriverError, ReconciliationResult, SemanticResultSelector


def require_strings(values: JsonObject, names: tuple[str, ...], contract: str) -> None:
    missing = [name for name in names if not isinstance(values.get(name), str) or not values[name]]
    if missing:
        raise DriverError(f"{contract} is missing string values: {', '.join(missing)}")


def select_result_fields(*names: str) -> SemanticResultSelector:
    def select(result: object) -> ReconciliationResult:
        if isinstance(result, Mapping):
            missing = [name for name in names if name not in result]
            if missing:
                raise DriverError(f"driver result is missing semantic fields: {', '.join(missing)}")
            return {name: result[name] for name in names}
        missing = [name for name in names if not hasattr(result, name)]
        if missing:
            raise DriverError(f"driver result is missing semantic fields: {', '.join(missing)}")
        return {name: getattr(result, name) for name in names}

    return select
