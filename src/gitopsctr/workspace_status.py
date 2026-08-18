"""Logical-workspace orchestration for the environment status use case."""

from __future__ import annotations

from pathlib import PurePosixPath

from gitopsctr.application.status import StatusCommand, StatusEntry, StatusResult, StatusState
from gitopsctr.application.workspace import WorkspaceEntryKind
from gitopsctr.errors import OperationError
from gitopsctr.operational import classify_workspace_before_observation
from gitopsctr.resource_model import ResourcePlane, ResourceRegistry
from gitopsctr.workspace_inspection import WorkspacePlaneProvider
from gitopsctr.workspace_inventory import WorkspaceInventorySession


class _PinnedWorkspacePlanes:
    """Expose the three snapshots selected at command start without re-resolving heads."""

    def __init__(self, owner: WorkspacePlaneProvider, project, source, desired, observed) -> None:
        self._owner = owner
        self._closed = False
        self._project = project
        self._source = source
        self._selected = {(desired.plane, desired.reference): desired, (observed.plane, observed.reference): observed}

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.close()

    def project(self):
        return self._project

    def source(self):
        return self._source

    def snapshot(self, plane, reference, revision=None, *, allow_missing=False):
        selected = self._selected.get((plane, reference))
        if selected is None or (revision is not None and revision != selected.revision):
            raise OperationError(f"{plane} ref {reference!r} does not exist")
        if selected.revision is None and not allow_missing:
            raise OperationError(f"{plane} ref {reference!r} does not exist")
        return selected


class _SourcePinnedPlanes:
    """Hold the authored source view while deployment refs are discovered."""

    def __init__(self, delegate: WorkspacePlaneProvider, source) -> None:
        self._delegate = delegate
        self._source = source
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._delegate.close()

    def project(self):
        return self._delegate.project()

    def source(self):
        return self._source

    def snapshot(self, plane, reference, revision=None, *, allow_missing=False):
        return self._delegate.snapshot(plane, reference, revision, allow_missing=allow_missing)


def _cleanup_names(snapshot) -> tuple[str, ...]:
    """Return opaque cleanup identities from one already-selected desired view."""

    paths = tuple(
        entry.key
        for entry in snapshot.workspace.list_entries(".gitopsctr/cleanup/units")
        if entry.kind is WorkspaceEntryKind.FILE
        and PurePosixPath(entry.key).suffix.lower() in {".json", ".yaml", ".yml"}
    )
    names: dict[str, str] = {}
    for path in paths:
        name = PurePosixPath(path).stem
        previous = names.get(name)
        if previous is not None:
            raise OperationError(
                f"environment {snapshot.reference!r}, desired: duplicate opaque cleanup root {name!r} "
                f"at {previous} and {path}"
            )
        names[name] = path
    return tuple(sorted(names))


def status_workspace_provider(
    planes: WorkspacePlaneProvider,
    registry: ResourceRegistry,
    command: StatusCommand,
) -> StatusResult:
    """Evaluate status from one coherent, path-free source/desired/observed read.

    Plane selection happens before inventory discovery.  The provider cache then
    makes every collection and relationship read in this command use precisely
    those selected workspaces rather than resolving mutable heads independently.
    """

    # ``WorkspaceInventorySession`` reads project configuration in its
    # constructor, before its context manager can own cleanup.  Retain outer
    # ownership until construction succeeds; from then on the active pinned
    # wrapper transfers and closes it exactly once.
    try:
        inventory = WorkspaceInventorySession(registry, planes)
    except Exception:
        planes.close()
        raise
    with inventory:
        source = planes.source()
        inventory.planes = _SourcePinnedPlanes(planes, source)
        desired_ref, observed_ref = inventory.deployment_refs(command.environment)
        desired_ref = command.desired_reference or desired_ref
        observed_ref = command.observed_reference or observed_ref

        # A missing default observed channel is an empty observation set. Once
        # the caller explicitly chooses either selector, absence is an error.
        desired = planes.snapshot(
            ResourcePlane.DESIRED,
            desired_ref,
            command.desired_snapshot,
            allow_missing=command.desired_snapshot is None,
        )
        observed = planes.snapshot(
            ResourcePlane.OBSERVED,
            observed_ref,
            command.observed_snapshot,
            allow_missing=command.observed_snapshot is None and command.observed_reference is None,
        )

        # Inventory must observe only the views selected above.  This wrapper
        # deliberately has no fallback to the mutable provider.
        inventory.planes = _PinnedWorkspacePlanes(planes, inventory.project, source, desired, observed)

        source_units = inventory.resources("unit", environment=command.environment, plane=ResourcePlane.SOURCE)
        evaluation = inventory.evaluate_environment(
            command.environment,
            desired_ref=desired_ref,
            desired_revision=desired.revision,
            observed_ref=observed_ref,
            observed_revision=observed.revision,
            resolve_artifacts=False,
        )

        entries = _status_entries(evaluation, source_units, desired, command)

        return StatusResult(
            command.environment,
            desired_ref,
            desired.revision,
            observed_ref,
            observed.revision,
            entries,
        )


def _status_entries(evaluation, source_units, desired, command: StatusCommand) -> tuple[StatusEntry, ...]:
    """Merge evaluated, source-only, and cleanup states by qualified identity."""

    entries_by_identity: dict[str, StatusEntry] = {
        state.unit.qualified_name: StatusEntry(
            state.unit.qualified_name, StatusState(state.reconciliation.value), state.reason
        )
        for state in evaluation.units
    }
    # Authored Units not yet materialized into desired state retain the
    # historical status semantics, without creating a temporary tree.
    for unit in source_units:
        if unit.qualified_name not in entries_by_identity:
            status = classify_workspace_before_observation(desired.workspace, unit.name, None, None)
            assert status is not None
            entries_by_identity[unit.qualified_name] = StatusEntry(
                unit.qualified_name, StatusState(status.reconciliation.value), status.reason
            )

    for name in _cleanup_names(desired):
        if name in entries_by_identity:
            continue
        status = classify_workspace_before_observation(desired.workspace, name, None, None)
        assert status is not None
        entries_by_identity[name] = StatusEntry(name, StatusState(status.reconciliation.value), status.reason)

    if command.unit is not None:
        selected = entries_by_identity.get(command.unit)
        if selected is None:
            available = ", ".join(sorted(entries_by_identity)) or "none"
            raise OperationError(
                f"unknown unit {command.unit!r} for environment {command.environment!r}; available units: {available}"
            )
        return (selected,)
    return tuple(entries_by_identity[name] for name in sorted(entries_by_identity))
