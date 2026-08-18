"""Git-backed adapter for typed logical-workspace status."""

from __future__ import annotations

import glob as globlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.application.model import SnapshotId
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.status import StatusCommand, StatusEntry, StatusExplanation, StatusResult, StatusState
from gitopsctr.formats import parse_document_bytes
from gitopsctr.resource_model import ResourceRegistry
from gitopsctr.workspace_inspection import WorkspacePlaneProvider
from gitopsctr.workspace_status import status_workspace_provider


class GitHistorySnapshotReader(Protocol):
    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView: ...

    def snapshot_id_for_revision(self, revision: str) -> SnapshotId: ...


@dataclass(frozen=True, slots=True)
class GitStatusInspector:
    """Run status against exact Git workspaces selected for one command."""

    repository_root: Path
    snapshot_reader: GitHistorySnapshotReader
    registry: ResourceRegistry

    def close(self) -> None:
        """Each request owns only a lightweight workspace provider."""

    def status(self, command: StatusCommand) -> StatusResult:
        return self.status_with_provider(
            GitWorkspacePlaneProvider(
                self.repository_root,
                cast(GitSnapshotReader, self.snapshot_reader),
            ),
            command,
        )

    def status_with_provider(self, provider: WorkspacePlaneProvider, command: StatusCommand) -> StatusResult:
        """Run shared Git-shaped explanations over an injected logical plane provider."""

        result = status_workspace_provider(provider, self.registry, command)
        if result.desired_revision is None or result.observed_revision is None:
            return result
        desired = self.snapshot_reader.open_snapshot(
            self.snapshot_reader.snapshot_id_for_revision(result.desired_revision)
        ).workspace
        observed = self.snapshot_reader.open_snapshot(
            self.snapshot_reader.snapshot_id_for_revision(result.observed_revision)
        ).workspace
        entries = tuple(
            StatusEntry(entry.name, entry.state, entry.reason, self._explanation(entry, desired, observed))
            if entry.state is StatusState.READY
            else entry
            for entry in result.entries
        )
        return StatusResult(
            result.environment,
            result.desired_reference,
            result.desired_revision,
            result.observed_reference,
            result.observed_revision,
            entries,
        )

    def _explanation(self, entry: StatusEntry, desired, observed) -> StatusExplanation | None:
        current_path, current = _document_for_name(desired, entry.name)
        _receipt_path, receipt = _document_for_name(observed, entry.name)
        if current_path is None or current is None or receipt is None:
            return None
        specification = receipt.get("spec")
        desired_record = specification.get("desired") if isinstance(specification, dict) else None
        previous_revision = desired_record.get("revision") if isinstance(desired_record, dict) else None
        if not isinstance(previous_revision, str):
            return None
        try:
            previous_workspace = self.snapshot_reader.open_snapshot(
                self.snapshot_reader.snapshot_id_for_revision(previous_revision)
            ).workspace
            previous = parse_document_bytes(previous_workspace.read(current_path.as_posix()), current_path)
        except Exception:
            return None
        if not isinstance(previous, dict):
            return None
        previous_source = _source(previous)
        current_source = _source(current)
        causes: list[str] = []
        if previous.get("kind") != current.get("kind") or previous_source.get("driverVersion") != current_source.get(
            "driverVersion"
        ):
            causes.append("reconciliation driver changed")
        source_changed = previous_source.get("inputHash") != current_source.get("inputHash")
        commits, files = self._source_evidence(previous_source, current_source) if source_changed else ((), ())
        if files:
            causes.append("source inputs changed")
        previous_inputs = _resolved_inputs(previous)
        current_inputs = _resolved_inputs(current)
        if _observation_values(previous_inputs) != _observation_values(current_inputs):
            changed = sorted(set(_observation_values(previous_inputs)) | set(_observation_values(current_inputs)))
            causes.append("upstream observations changed: " + ", ".join(PurePosixPath(path).name for path in changed))
        previous_promotions = previous_inputs.get("promotions")
        current_promotions = current_inputs.get("promotions")
        previous_promotions = previous_promotions if isinstance(previous_promotions, dict) else {}
        current_promotions = current_promotions if isinstance(current_promotions, dict) else {}
        if previous_promotions != current_promotions:
            changed = sorted(
                key
                for key in set(previous_promotions) | set(current_promotions)
                if isinstance(key, str) and previous_promotions.get(key) != current_promotions.get(key)
            )
            causes.append("reviewed promotion inputs changed: " + ", ".join(changed))
        specification_paths = tuple(_changed_paths(_without_volatile_spec(previous), _without_volatile_spec(current)))
        if specification_paths:
            causes.append("unit specification changed")
        if source_changed and not files and not causes:
            causes.append("source input fingerprint changed")
        if not causes:
            causes.append("desired unit content changed")
        return StatusExplanation(
            previous_revision,
            _text_value(previous_source.get("revision")),
            _text_value(current_source.get("revision")),
            tuple(causes),
            commits,
            files,
            specification_paths,
        )

    def _source_evidence(
        self, previous: dict[str, object], current: dict[str, object]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        old, new, path = (
            _text_value(previous.get("revision")),
            _text_value(current.get("revision")),
            _text_value(current.get("path")),
        )
        if old is None or new is None or path is None:
            return (), ()
        inputs = current.get("inputs")
        suffixes = tuple(inputs) if isinstance(inputs, list) and all(isinstance(item, str) for item in inputs) else ()
        paths = tuple(
            f":(glob){PurePosixPath(path) / item}" if globlib.has_magic(item) else str(PurePosixPath(path) / item)
            for item in suffixes
        ) or (path,)

        def run(*args: str) -> tuple[str, ...]:
            completed = subprocess.run(
                ("git", "-C", str(self.repository_root), *args), check=False, capture_output=True, text=True
            )
            return tuple(line for line in completed.stdout.splitlines() if line) if completed.returncode == 0 else ()

        return run("log", "--format=%h %s", "--no-merges", f"{old}..{new}", "--", *paths), run(
            "diff", "--name-status", old, new, "--", *paths
        )


def _document_for_name(workspace, name: str) -> tuple[PurePosixPath | None, dict[str, object] | None]:
    for entry in workspace.list_entries("units"):
        if entry.content is None:
            continue
        path = PurePosixPath(entry.key)
        try:
            document = parse_document_bytes(entry.content, path)
        except Exception:
            continue
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if path.parent.as_posix().removeprefix("units/") + (
            "/" if path.parent.as_posix() != "units" else ""
        ) + path.stem == name or (isinstance(metadata, dict) and metadata.get("name") == name):
            return path, document if isinstance(document, dict) else None
    return None, None


def _source(document: dict[str, object]) -> dict[str, object]:
    spec = document.get("spec")
    source = spec.get("source") if isinstance(spec, dict) else None
    return source if isinstance(source, dict) else {}


def _resolved_inputs(document: dict[str, object]) -> dict[str, object]:
    spec = document.get("spec")
    value = spec.get("resolvedInputs") if isinstance(spec, dict) else None
    return value if isinstance(value, dict) else {}


def _observation_values(inputs: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("receipts", "artifacts"):
        value = inputs.get(name)
        if isinstance(value, dict):
            result.update(value)
    return result


def _without_volatile_spec(document: dict[str, object]) -> dict[str, object]:
    spec = document.get("spec")
    return (
        {key: value for key, value in spec.items() if key not in {"source", "resolvedInputs"}}
        if isinstance(spec, dict)
        else {}
    )


def _changed_paths(previous: object, current: object, prefix: str = "") -> list[str]:
    if isinstance(previous, dict) and isinstance(current, dict):
        return [
            path
            for key in sorted(set(previous) | set(current))
            for path in _changed_paths(previous.get(key), current.get(key), f"{prefix}/{key}")
        ]
    if previous != current:
        return [prefix or "/"]
    return []


def _text_value(value: object) -> str | None:
    return value if isinstance(value, str) else None
