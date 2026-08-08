"""Shared helpers for contributed drivers."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gitopsctr.document import JsonObject
from gitopsctr.driver import DriverError, ReconciliationResult, SemanticResultSelector


def run(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "check": check,
        "text": True,
        "cwd": cwd,
        "env": env,
        "input": input_text,
    }
    if capture:
        kwargs["capture_output"] = True
    else:
        kwargs.update(stdout=sys.stderr, stderr=sys.stderr)
    return subprocess.run(args, **kwargs)


def require_strings(values: JsonObject, names: tuple[str, ...], contract: str) -> None:
    missing = [name for name in names if not isinstance(values.get(name), str) or not values[name]]
    if missing:
        raise DriverError(f"{contract} is missing string values: {', '.join(missing)}")


def select_result_fields(*names: str) -> SemanticResultSelector:
    def select(result: object) -> ReconciliationResult:
        if not isinstance(result, Mapping):
            raise DriverError("driver result must be an object")
        missing = [name for name in names if name not in result]
        if missing:
            raise DriverError(f"driver result is missing semantic fields: {', '.join(missing)}")
        return {name: result[name] for name in names}

    return select
