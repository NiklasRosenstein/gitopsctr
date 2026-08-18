"""Workspace-backed implementation of the default Git read-only inspector."""

from __future__ import annotations

import argparse
from pathlib import Path

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.git.workspace_planes import GitWorkspacePlaneProvider
from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.resource_model import ResourceRegistry
from gitopsctr.workspace_get import build_workspace_resource_inspection
from gitopsctr.workspace_inspection import WorkspacePlaneProvider


def inspect_git_workspaces(
    repository_root: Path,
    snapshot_reader: GitSnapshotReader,
    registry: ResourceRegistry,
    command: ResourceInspectionCommand,
) -> ResourceInspectionResult:
    """Run existing relationship semantics over exact logical plane workspaces."""

    planes = GitWorkspacePlaneProvider(repository_root, snapshot_reader)
    return inspect_workspace_provider(planes, registry, command)


def inspect_workspace_provider(
    planes: WorkspacePlaneProvider,
    registry: ResourceRegistry,
    command: ResourceInspectionCommand,
) -> ResourceInspectionResult:
    """Execute a backend-neutral command against any logical-plane provider."""
    values: dict[str, object] = {
        "selector": command.selector,
        "name": command.name,
        "environment": command.environment,
        "all_environments": command.all_environments,
        "desired_ref": command.desired_reference,
        "desired_revision": command.desired_snapshot,
        "observed_ref": command.observed_reference,
        "observed_revision": command.observed_snapshot,
        "output": command.output.value,
        "artifact": command.artifact,
        "artifacts": command.artifacts,
        "as_list": command.as_list,
    }
    values.update({item.name: item.value for item in command.filters})
    return build_workspace_resource_inspection(planes, argparse.Namespace(**values), registry=registry)
