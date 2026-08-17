"""Controller-independent operational semantics for persisted desired Units."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, cast

from gitopsctr.document import JsonObject
from gitopsctr.driver import DriverError, MaterializationCapability, ReconciliationCapability
from gitopsctr.errors import OperationError
from gitopsctr.formats import load_document
from gitopsctr.resources import UnitResource
from gitopsctr.templates import TemplateError, contains_reference, parse_template_value

DESIRED_TRANSITION_BLOCKS_PATH = PurePosixPath(".gitopsctr/transition-blocks.json")


class ReconciliationState(StrEnum):
    CLEAN = "CLEAN"
    READY = "READY"
    WAIT = "WAIT"
    MATERIALIZED = "MATERIALIZED"


class ObservationEvidence(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True)
class OperationalStatus:
    reconciliation: ReconciliationState
    reason: str


def load_desired_transition_blocks(root: Path) -> dict[str, str]:
    """Load the durable reasons that fence desired Unit transitions."""

    path = root / DESIRED_TRANSITION_BLOCKS_PATH
    if not path.is_file():
        return {}
    try:
        document = load_document(path)
    except Exception as exc:
        raise OperationError("invalid desired transition-block document") from exc
    blocks = document.get("blocks") if isinstance(document, dict) else None
    if not isinstance(blocks, dict) or not all(
        isinstance(name, str) and isinstance(reason, str) for name, reason in blocks.items()
    ):
        raise OperationError("invalid desired transition-block document")
    return cast(dict[str, str], blocks)


def raw_unit_contains_reference(document: object) -> bool:
    """Check an untrusted persisted Unit before requiring its final desired contract."""

    value = document.get("spec") if isinstance(document, dict) and isinstance(document.get("spec"), dict) else document
    try:
        return contains_reference(parse_template_value(value))
    except TemplateError as exc:
        raise OperationError(str(exc)) from exc


def unit_requires_reconciliation(unit: UnitResource[Any]) -> bool:
    """Return whether a desired Unit requires an observed Receipt."""

    driver = unit.driver
    if not isinstance(driver, ReconciliationCapability):
        return False
    try:
        return driver.reconciliation_required(unit.spec)
    except DriverError as exc:
        raise OperationError(str(exc)) from exc


def materialization_tree_digest(root: Path) -> str:
    """Digest one complete, canonical materialization tree."""

    if not root.is_dir() or root.is_symlink():
        raise OperationError("materialization output must be a directory")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise OperationError(f"materialization output contains a symbolic link: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise OperationError(f"materialization output contains a non-file: {path.relative_to(root)}")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                "contentHash": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not entries:
        raise OperationError("materialization output is empty")
    payload = {"materializationHashVersion": 1, "files": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def validate_unit_materialization(desired_root: Path, unit_name: str, unit: UnitResource[Any]) -> None:
    """Validate capability, descriptor, canonical path, tree, and digest as one invariant."""

    expects_materialization = isinstance(unit.driver, MaterializationCapability)
    descriptor = getattr(unit.spec, "materialization", None)
    if not expects_materialization:
        if descriptor is not None:
            raise OperationError(f"{unit_name} records materialization for a plugin without that capability")
        return
    if descriptor is None:
        raise OperationError(f"{unit_name} has an invalid materialization descriptor")
    expected_path = PurePosixPath("materialized", *unit_name.split("/"))
    if not isinstance(descriptor.digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor.digest):
        raise OperationError(f"{unit_name} has an invalid materialization digest")
    if not descriptor.mediaType:
        raise OperationError(f"{unit_name} has an invalid materialization media type")
    actual = materialization_tree_digest(desired_root / expected_path)
    if actual != descriptor.digest:
        raise OperationError(f"{unit_name} materialized payload does not match its digest")


def deletion_reason(unit: UnitResource[Any]) -> str:
    deletion = unit.metadata.deletion
    if deletion is None:
        raise OperationError(f"desired resource {unit.name!r} is not marked for deletion")
    return f"deletion pending finalization (UID {unit.metadata.uid}, generation {deletion.generation})"


def classify_before_observation(
    desired_root: Path,
    unit_name: str,
    document: JsonObject | None,
    unit: UnitResource[Any] | None,
    transition_reason: str | None = None,
) -> OperationalStatus | None:
    """Classify desired-only gates before considering any Receipt evidence."""

    if unit is not None and unit.metadata.deletion is not None:
        return OperationalStatus(ReconciliationState.WAIT, deletion_reason(unit))
    if transition_reason is not None:
        return OperationalStatus(ReconciliationState.WAIT, transition_reason)
    if document is None or unit is None:
        return OperationalStatus(ReconciliationState.WAIT, "desired inputs are not materialized")
    if raw_unit_contains_reference(document):
        return OperationalStatus(ReconciliationState.WAIT, "desired inputs are not materialized")
    validate_unit_materialization(desired_root, unit_name, unit)
    if not unit_requires_reconciliation(unit):
        return OperationalStatus(
            ReconciliationState.MATERIALIZED,
            "desired payload is published for external delivery",
        )
    return None


def classify_observation(evidence: ObservationEvidence) -> OperationalStatus:
    if evidence is ObservationEvidence.MISSING:
        return OperationalStatus(ReconciliationState.READY, "no observation receipt")
    if evidence is ObservationEvidence.CURRENT:
        return OperationalStatus(ReconciliationState.CLEAN, "observation matches desired state")
    return OperationalStatus(ReconciliationState.READY, "desired inputs changed since its last receipt")
