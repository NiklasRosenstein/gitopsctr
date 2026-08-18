"""Logical-workspace collection discovery for read-only inspection.

This is the phase-3 replacement for walking a materialized repository tree.
It deliberately interprets the existing catalog's collection layout, while
all reads, traversal, raw-byte provenance, and duplicate candidates are
expressed as workspace keys and entries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from gitopsctr.application.workspace import ImmutableWorkspace, WorkspaceEntryKind
from gitopsctr.formats import DocumentFormatError, Project, parse_document_bytes
from gitopsctr.resource_api import (
    GVK,
    ApiKind,
    JsonObject,
    LocalResourceIdentity,
    ResourceSelection,
    TypedDocumentContract,
)
from gitopsctr.resource_model import (
    CollectionDocumentMode,
    DiscoveredResource,
    EnvironmentDirectories,
    FilesystemCollectionProvider,
    ProjectConfiguredDirectory,
    RepositoryRoot,
    ResourceCollection,
    ResourceFamilyDefinition,
    ResourceModelError,
    ResourcePlacement,
    SnapshotSubdirectory,
)


@dataclass(frozen=True, slots=True)
class WorkspaceCollectionReadContext:
    """Inputs needed to discover one registered collection from logical content."""

    workspace: ImmutableWorkspace
    project: Project
    environment: str | None
    family: ResourceFamilyDefinition
    placement: ResourcePlacement
    api_kinds: Mapping[GVK, ApiKind[object]]
    contracts: Mapping[GVK, TypedDocumentContract[object]]
    selection: ResourceSelection | None = None


def discover_workspace_collection(
    collection: ResourceCollection,
    context: WorkspaceCollectionReadContext,
) -> tuple[DiscoveredResource, ...]:
    """Discover catalog-defined resources without opening a filesystem path."""

    provider = collection.provider
    if not isinstance(provider, FilesystemCollectionProvider):
        raise ResourceModelError(f"collection {collection.name!r} has no logical-workspace reader")
    keys = _workspace_document_keys(provider, context)
    discovered: list[DiscoveredResource] = []
    for key in keys:
        path = PurePosixPath(key)
        try:
            raw = context.workspace.read(key)
            loaded = parse_document_bytes(raw, path)
        except (DocumentFormatError, ValueError) as exc:
            raise ResourceModelError(f"could not load {context.placement.plane} resource {path}: {exc}") from exc
        document = cast(JsonObject, loaded)
        api_version, kind, metadata = document.get("apiVersion"), document.get("kind"), document.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(name, str):
            raise ResourceModelError(f"resource {path} requires apiVersion, kind, and metadata.name")
        try:
            gvk = GVK(api_version, kind)
        except ValueError as exc:
            raise ResourceModelError(f"resource {path} has an invalid API kind: {exc}") from exc
        contract = context.contracts.get(gvk)
        if contract is None:
            raise ResourceModelError(
                f"resource {path} has API kind {gvk}, which is not in family {context.family.name!r}"
            )
        try:
            parsed = contract.parse(document)
        except Exception as exc:
            raise ResourceModelError(f"invalid {context.placement.plane} resource {path}: {exc}") from exc
        qualified_name = _storage_qualified_name(provider, context, key, name)
        local_identity = _local_identity(context, qualified_name, name)
        if not context.family.identity.matches(local_identity, context.selection):
            continue
        media_type = _media_type(provider, context, gvk, path)
        discovered.append(
            DiscoveredResource(
                path,
                document,
                gvk,
                name,
                parsed,
                str(context.workspace.entry_content_ids()[key]),
                f"sha256:{hashlib.sha256(raw).hexdigest()}",
                media_type,
                local_identity,
                qualified_name,
            )
        )
    return tuple(discovered)


def _workspace_document_keys(
    provider: FilesystemCollectionProvider, context: WorkspaceCollectionReadContext
) -> tuple[str, ...]:
    directories = _workspace_directories(provider, context)
    files = tuple(
        entry.key
        for entry in context.workspace.list_entries()
        if entry.kind is WorkspaceEntryKind.FILE
        and PurePosixPath(entry.key).suffix.lower() in {".yaml", ".yml", ".json"}
    )
    if provider.documents.mode is CollectionDocumentMode.ADDRESS:
        selected = _selected_values(context, "name")
        candidates = tuple(key for key in files if any(_under_directory(key, directory) for directory in directories))
        return (
            candidates if selected is None else tuple(key for key in candidates if PurePosixPath(key).stem in selected)
        )
    if provider.documents.mode is CollectionDocumentMode.DOCUMENT_NAME:
        return tuple(
            key
            for directory in directories
            for stem in provider.documents.stems
            for key in _fixed_candidates(files, directory, stem)
        )
    stem = provider.documents.stems[0]
    candidates: list[str] = []
    for directory in directories:
        values = _stem_candidates(files, directory, stem)
        if not values:
            raise ResourceModelError(f"directory {directory or '.'} has no {stem}.yaml, {stem}.yml, or {stem}.json")
        candidates.extend(values)
    return tuple(candidates)


def _workspace_directories(
    provider: FilesystemCollectionProvider, context: WorkspaceCollectionReadContext
) -> tuple[str, ...]:
    root = provider.root
    if isinstance(root, RepositoryRoot):
        return ("",)
    if isinstance(root, SnapshotSubdirectory):
        return (root.child,)
    if isinstance(root, ProjectConfiguredDirectory):
        value = getattr(context.project, root.field, None)
        if not isinstance(value, PurePosixPath):
            raise ResourceModelError(f"Project has no relative path field {root.field!r}")
        parts = list(value.parts)
        if root.environment_scoped:
            if context.environment is None:
                raise ResourceModelError(f"collection requires an environment for {root.field!r}")
            parts.append(context.environment)
        if root.child is not None:
            parts.append(root.child)
        return ("/".join(parts),)
    if isinstance(root, EnvironmentDirectories):
        value = getattr(context.project, root.field, None)
        if not isinstance(value, PurePosixPath):
            raise ResourceModelError(f"Project has no relative path field {root.field!r}")
        base = value.as_posix()
        if context.environment is not None:
            return (f"{base}/{context.environment}",)
        selected = _selected_values(context, "name")
        names = {
            entry.key.removeprefix(f"{base}/").split("/", 1)[0]
            for entry in context.workspace.list_entries()
            if entry.key.startswith(f"{base}/")
        }
        return tuple(f"{base}/{name}" for name in sorted(names) if selected is None or name in selected)
    raise ResourceModelError(f"collection root {type(root).__name__} has no logical-workspace implementation")


def _selected_values(context: WorkspaceCollectionReadContext, segment: str) -> frozenset[str] | None:
    if segment not in {item.name for item in context.family.identity.segments} or context.selection is None:
        return None
    constrained = context.selection.values_for(segment)
    if constrained is not None:
        return constrained
    if context.selection.exact is None:
        return None
    return frozenset((context.family.identity.value(context.selection.exact, segment),))


def _under_directory(key: str, directory: str) -> bool:
    return not directory or key.startswith(f"{directory}/")


def _stem_candidates(files: tuple[str, ...], directory: str, stem: str) -> tuple[str, ...]:
    prefix = f"{directory}/" if directory else ""
    return tuple(key for key in files if key in {f"{prefix}{stem}.yaml", f"{prefix}{stem}.yml", f"{prefix}{stem}.json"})


def _fixed_candidates(files: tuple[str, ...], directory: str, stem: str) -> tuple[str, ...]:
    suffix = PurePosixPath(stem).suffix.lower()
    if suffix in {".yaml", ".yml", ".json"}:
        key = f"{directory}/{stem}" if directory else stem
        return (key,) if key in files else ()
    return _stem_candidates(files, directory, stem)


def _storage_qualified_name(
    provider: FilesystemCollectionProvider,
    context: WorkspaceCollectionReadContext,
    key: str,
    name: str,
) -> str:
    directories = _workspace_directories(provider, context)
    matches = tuple(directory for directory in directories if _under_directory(key, directory))
    if len(matches) != 1:
        raise ResourceModelError(f"resource path {key} is not under exactly one collection root")
    directory = matches[0]
    if provider.documents.mode is CollectionDocumentMode.ADDRESS:
        relative = key.removeprefix(f"{directory}/") if directory else key
        qualified_name = PurePosixPath(relative).with_suffix("").as_posix()
        context.family.addressing.validate(qualified_name)
        expected = context.family.addressing.filter_value(qualified_name, "name", context.family.identity)
        if expected != name:
            raise ResourceModelError(
                f"resource metadata.name {name!r} in {key} does not match address terminal {expected!r}"
            )
        return qualified_name
    if provider.documents.mode is CollectionDocumentMode.DIRECTORY_NAME:
        qualified_name = PurePosixPath(directory).name
        context.family.addressing.validate(qualified_name)
        if name != qualified_name:
            raise ResourceModelError(
                f"resource metadata.name {name!r} in {key} does not match directory address {qualified_name!r}"
            )
        return qualified_name
    return name


def _local_identity(context: WorkspaceCollectionReadContext, qualified_name: str, name: str) -> LocalResourceIdentity:
    values = tuple(
        context.family.addressing.filter_value(qualified_name, segment.name, context.family.identity)
        for segment in context.family.identity.segments
    )
    identity = context.family.identity.build(values)
    if identity.values[-1] != name:
        raise ResourceModelError(f"resource metadata.name {name!r} does not match its registered address identity")
    return identity


def _media_type(
    provider: FilesystemCollectionProvider,
    context: WorkspaceCollectionReadContext,
    gvk: GVK,
    path: PurePosixPath,
) -> str | None:
    if not provider.media_typed:
        return None
    api_kind = context.api_kinds.get(gvk)
    if api_kind is None:
        raise ResourceModelError(f"resource {path} has no authoritative API registration")
    base = getattr(api_kind.spec, "media_type", None)
    if not isinstance(base, str) or not base:
        raise ResourceModelError(f"Artifact API {gvk} does not declare a media type")
    suffix = "json" if path.suffix.lower() == ".json" else "yaml"
    return f"{base}+{suffix}"
