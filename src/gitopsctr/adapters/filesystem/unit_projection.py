"""Filesystem host for controller-free driver resolution and materialization."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gitopsctr.adapters.filesystem.workspace import FilesystemWorkspaceAdapter, FilesystemWorkspaceError
from gitopsctr.application.apply_compilers import UnitProjection, UnitProjectionRequest
from gitopsctr.application.apply_projection import PayloadPrefixReplacement, payload_prefix_evidence
from gitopsctr.application.workspace import WorkspaceEntry
from gitopsctr.contracts import MaterializationDocument
from gitopsctr.document import JsonObjectValue
from gitopsctr.driver import (
    DriverError,
    MaterializationCapability,
    MaterializationContext,
    MaterializationResult,
    UnitResolutionContext,
)
from gitopsctr.errors import OperationError
from gitopsctr.operational import materialization_tree_digest, validate_workspace_unit_materialization
from gitopsctr.resources import ResourceCatalog, UnitResource


class FilesystemUnitProjectionError(OperationError):
    """A private driver projection workspace could not be completed safely."""


@dataclass(frozen=True, slots=True)
class FilesystemUnitProjectionHost:
    """Run legacy driver materializers only inside owned private directories.

    Paths never cross into application code.  The result is returned as a
    typed desired Unit plus entries under the core-authorized materialization
    prefix.  Reusing an identical previous resolved model leaves its existing
    payload untouched, which preserves byte-for-byte candidate no-ops.
    """

    catalog: ResourceCatalog
    workspace_adapter: FilesystemWorkspaceAdapter = FilesystemWorkspaceAdapter()

    def project(self, request: UnitProjectionRequest) -> UnitProjection:
        try:
            resolved_result = request.unit.driver.resolve_unit(
                request.unit.spec,
                UnitResolutionContext(source=request.source, resolve_template=request.resolve_template),
            )
        except (DriverError, TypeError, ValueError) as exc:
            raise FilesystemUnitProjectionError(str(exc)) from exc
        resolved = UnitResource(request.unit.gvk, request.metadata, request.unit.driver, resolved_result.unit)
        driver = resolved.driver
        if not isinstance(driver, MaterializationCapability):
            return UnitProjection(resolved)
        if request.source is None or request.selected_source is None:
            raise FilesystemUnitProjectionError(f"{request.qualified_name} requires an exact retained source")
        reused = self._reuse(request, resolved, driver)
        if reused is not None:
            return reused
        try:
            with tempfile.TemporaryDirectory(prefix="gitopsctr-unit-projection-") as temporary:
                root = Path(temporary)
                source_root = root / "source"
                output_root = root / "output"
                self.workspace_adapter.materialize(request.selected_source.plane.workspace, source_root)
                output_root.mkdir(mode=0o700)
                result = driver.materialize(
                    MaterializationContext(
                        environment=str(request.environment_id),
                        source_root=source_root,
                        source_revision=request.source.revision,
                        source_path=request.source.path,
                        unit_name=resolved.name,
                        unit=resolved.spec,
                        output_root=output_root,
                        qualified_name=request.qualified_name,
                    )
                )
                if not isinstance(result, MaterializationResult) or not result.media_type:
                    raise FilesystemUnitProjectionError(
                        f"{driver.driver_name} returned an invalid materialization result"
                    )
                descriptor = MaterializationDocument(
                    digest=materialization_tree_digest(output_root),
                    mediaType=result.media_type,
                    metadata=JsonObjectValue(result.metadata),
                )
                desired = UnitResource(
                    resolved.gvk,
                    resolved.metadata,
                    driver,
                    driver.finalize_materialization(resolved.spec, descriptor),
                )
                payload = self.workspace_adapter.read(output_root)
                prefix = f"materialized/{request.qualified_name}"
                entries = tuple(_prefixed_entry(prefix, entry) for entry in payload.list_entries())
                expected_content_id, expected_entries = payload_prefix_evidence(request.current_workspace, prefix)
                return UnitProjection(
                    desired,
                    payload_prefixes=(prefix,),
                    payload_replacements=(
                        PayloadPrefixReplacement(prefix, expected_content_id, expected_entries, entries),
                    ),
                )
        except (DriverError, FilesystemWorkspaceError, OSError, OperationError, TypeError, ValueError) as exc:
            if isinstance(exc, FilesystemUnitProjectionError):
                raise
            raise FilesystemUnitProjectionError(str(exc)) from exc

    def _reuse(
        self,
        request: UnitProjectionRequest,
        resolved: UnitResource[Any],
        driver: MaterializationCapability[Any, Any],
    ) -> UnitProjection | None:
        previous = request.previous
        if previous is None:
            return None
        try:
            prior = self.catalog.parse_unit(previous.mutable_document(), profile="desired")
        except (OperationError, TypeError, ValueError) as exc:
            raise FilesystemUnitProjectionError(
                "previous desired Unit cannot be parsed for materialization reuse"
            ) from exc
        if prior.gvk != resolved.gvk or prior.driver_name != resolved.driver_name:
            return None
        try:
            if driver.resolved_from_desired(prior.spec) != resolved.spec:
                return None
            descriptor = getattr(prior.spec, "materialization", None)
            if (
                descriptor is None
                or not isinstance(getattr(descriptor, "digest", None), str)
                or not isinstance(getattr(descriptor, "mediaType", None), str)
            ):
                return None
            descriptor_metadata = getattr(descriptor, "metadata", None)
            if hasattr(descriptor_metadata, "to_dict"):
                descriptor_metadata = descriptor_metadata.to_dict()
            if not isinstance(descriptor_metadata, Mapping):
                return None
            prior_descriptor = MaterializationDocument(
                digest=descriptor.digest,
                mediaType=descriptor.mediaType,
                metadata=JsonObjectValue(dict(descriptor_metadata)),
            )
            desired = UnitResource(
                resolved.gvk,
                resolved.metadata,
                resolved.driver,
                driver.finalize_materialization(resolved.spec, prior_descriptor),
            )
            validate_workspace_unit_materialization(request.current_workspace, request.qualified_name, prior)
        except (DriverError, TypeError, ValueError):
            return None
        except OperationError:
            return None
        return UnitProjection(desired)


def _prefixed_entry(prefix: str, entry: WorkspaceEntry) -> WorkspaceEntry:
    key = f"{prefix}/{entry.key}"
    return WorkspaceEntry(key, entry.kind, entry.content, entry.executable, entry.target)
