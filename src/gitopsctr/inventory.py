"""Registry-driven resource inventory and relationship evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import cast

from gitopsctr.contracts import StackProjectionUnitBinding
from gitopsctr.errors import OperationError
from gitopsctr.formats import DocumentFormatError, load_document, load_project_config
from gitopsctr.operational import (
    ObservationEvidence,
    ReconciliationState,
    classify_before_observation,
    classify_observation,
    load_desired_transition_blocks,
)
from gitopsctr.plane_repositories import PlaneRepositorySession, PlaneSnapshot
from gitopsctr.resource_api import GVK, JsonObject
from gitopsctr.resource_model import (
    ArtifactDescriptionDefinition,
    ArtifactLink,
    ArtifactResolutionContext,
    CollectionReadContext,
    DiscoveredResource,
    EnvironmentInspectionSummary,
    InspectionRecord,
    LocalResourceIdentity,
    ObservationDefinition,
    RelationshipResource,
    ResourceAddress,
    ResourceFamilyDefinition,
    ResourceIdentity,
    ResourceModelError,
    ResourcePlacement,
    ResourcePlane,
    ResourceRegistry,
    ResourceSelection,
    StackInspectionSummary,
)
from gitopsctr.resources import ResourceMetadata, StackResource, UnitResource, desired_unit_binding_digest


class InventoryError(OperationError):
    """A persisted resource or registered relationship cannot be inspected."""


def _names_selection(names: frozenset[str]) -> ResourceSelection:
    return ResourceSelection.segment("name", names)


class InventoryObservationState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING = "MISSING"
    NOT_APPLICABLE = "N/A"
    ORPHAN = "ORPHAN"


@dataclass(frozen=True)
class InventoryRecord:
    """One exact persisted resource and its storage provenance."""

    environment: str | None
    family: ResourceFamilyDefinition
    plane: ResourcePlane
    ref: str | None
    revision: str | None
    path: PurePosixPath
    document: JsonObject
    gvk: GVK
    name: str
    parsed: object
    blob_id: str | None
    content_digest: str
    media_type: str | None
    local_identity: LocalResourceIdentity
    storage_qualified_name: str
    snapshot_root: Path | None = None

    @property
    def identity(self) -> ResourceIdentity:
        return ResourceIdentity(self.gvk.api_version, self.gvk.kind, self.name)

    @property
    def qualified_name(self) -> str:
        return self.storage_qualified_name

    @property
    def address(self) -> ResourceAddress:
        placement = InventorySession._placement(self.family, self.plane)
        return ResourceAddress(self.family.name, placement.scope, self.environment, self.storage_qualified_name)

    @property
    def logical_identity(self) -> ResourceAddress:
        """Collection-owned identity used to detect duplicate persisted resources."""

        return self.address

    def relationship_resource(self) -> RelationshipResource:
        return RelationshipResource(
            self.identity,
            self.document,
            self.parsed,
            self.path,
            self.blob_id,
            self.content_digest,
            self.media_type,
        )


def _metadata_value(record: InventoryRecord, field: str) -> str | None:
    metadata = record.document.get("metadata")
    value = metadata.get(field) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) else None


def _specification_value(record: InventoryRecord, field: str) -> str | None:
    specification = record.document.get("spec")
    value = specification.get(field) if isinstance(specification, dict) else None
    return value if isinstance(value, str) else None


def _graph_parsed(record: InventoryRecord) -> object:
    """Adapt core contract models to the typed graph resource surface."""

    if record.family.name not in {"stack", "stacktemplate"}:
        return record.parsed
    parsed = record.parsed
    metadata = getattr(parsed, "metadata", None)
    specification = getattr(parsed, "spec", None)
    if metadata is None or specification is None:
        raise InventoryError(f"desired {record.family.singular} {record.name!r} has no typed graph representation")
    return StackResource(
        record.gvk,
        ResourceMetadata(
            name=metadata.name,
            uid=getattr(metadata, "uid", None),
            labels=getattr(metadata, "labels", None),
            ownerReferences=getattr(metadata, "ownerReferences", None),
            deletion=getattr(metadata, "deletion", None),
        ),
        specification,
    )


@dataclass(frozen=True)
class UnitOperationalState:
    unit: InventoryRecord
    observation: InventoryObservationState
    reconciliation: ReconciliationState
    reason: str
    receipt: InventoryRecord | None = None
    artifacts: tuple[ArtifactLink, ...] = ()


@dataclass(frozen=True)
class ReceiptOperationalState:
    receipt: InventoryRecord
    observation: InventoryObservationState
    unit: InventoryRecord | None
    artifacts: tuple[ArtifactLink, ...] = ()
    artifact_count: int = 0


@dataclass(frozen=True)
class ArtifactOperationalState:
    """Authentication state for one observed Artifact and its exact producer."""

    artifact: InventoryRecord
    authentication: InventoryObservationState
    producer: InventoryRecord | None
    receipt: InventoryRecord | None


@dataclass(frozen=True)
class ObservationOperationalState:
    """Generic state for one side of a registry-defined observation relationship."""

    resource: InventoryRecord
    observation: InventoryObservationState
    counterpart: InventoryRecord | None


@dataclass(frozen=True)
class ObservationRelationshipEvaluation:
    """Generic subject and observer states for one exact observation definition."""

    subjects: tuple[ObservationOperationalState, ...]
    observers: tuple[ObservationOperationalState, ...]


@dataclass(frozen=True)
class RelationshipEvaluation:
    units: tuple[UnitOperationalState, ...]
    receipts: tuple[ReceiptOperationalState, ...]
    artifacts: tuple[ArtifactOperationalState, ...] = ()


class InventorySession:
    """Discover registered resources against one command's snapshot cache."""

    def __init__(
        self,
        repository_root: Path,
        registry: ResourceRegistry,
        planes: PlaneRepositorySession | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.registry = registry
        self.planes = planes or PlaneRepositorySession(self.repository_root)
        self._owns_planes = planes is None
        try:
            self.project = load_project_config(self.repository_root)
        except DocumentFormatError as exc:
            raise InventoryError(str(exc)) from exc
        self._stack_inspection_cache: dict[
            tuple[str, str, str | None, str, str | None, bool],
            dict[PurePosixPath, StackInspectionSummary],
        ] = {}
        self._stack_inspection_observed_keys: dict[tuple[str, str, str | None], tuple[str, str | None, bool]] = {}
        self._qualified_name_cache: dict[tuple[str | None, str | None, PurePosixPath], str] = {}

    def __enter__(self) -> InventorySession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_planes:
            self.planes.close()

    def resource_qualified_name(self, record: object) -> str:
        if not isinstance(record, InventoryRecord):
            raise InventoryError("resource address resolution requires an inventory record")
        key = record.ref, record.revision, record.path
        cached = self._qualified_name_cache.get(key)
        if cached is not None:
            return cached
        try:
            value = record.family.addressing.qualified_name(record, self)
            record.family.addressing.validate(value)
            if value != record.storage_qualified_name:
                raise ResourceModelError(
                    f"derived address {value!r} does not match canonical storage address "
                    f"{record.storage_qualified_name!r}"
                )
        except ResourceModelError as exc:
            raise InventoryError(
                f"could not resolve {record.family.singular} {record.name!r} address at {record.path}: {exc}"
            ) from exc
        self._qualified_name_cache[key] = value
        return value

    def relationship_sources(self, relationship: str, target: object) -> tuple[InventoryRecord, ...]:
        if not isinstance(target, InventoryRecord):
            raise InventoryError("relationship address resolution requires an inventory record")
        definition = self.registry.graph_relationship(relationship)
        if target.family.name != definition.target_family or target.plane is not definition.target_plane:
            raise InventoryError(f"relationship {relationship!r} does not target {target.family.name!r}")
        candidates = self.resources(
            definition.source_family,
            environment=target.environment,
            plane=definition.source_plane,
            ref=target.ref,
            revision=target.revision,
            allow_missing_ref=False,
        )
        matches: list[InventoryRecord] = []
        for source in candidates:
            try:
                definition.binding.validate(_graph_parsed(source), _graph_parsed(target))
            except ResourceModelError:
                continue
            matches.append(source)
        return tuple(matches)

    def resources(
        self,
        selector: str,
        *,
        environment: str | None = None,
        plane: ResourcePlane | None = None,
        ref: str | None = None,
        revision: str | None = None,
        allow_missing_ref: bool = False,
        selection: ResourceSelection | None = None,
    ) -> tuple[InventoryRecord, ...]:
        family = self.registry.family(selector)
        placement = self._placement(family, plane)
        if placement.scope.value == "environment" and environment is None:
            raise InventoryError(f"resource {family.plural!r} requires an environment")
        if placement.plane is ResourcePlane.SOURCE:
            if ref is not None or revision is not None:
                raise InventoryError("source resources do not accept ref or revision overrides")
            snapshot = self.planes.source()
        else:
            if ref is None:
                raise InventoryError(f"{placement.plane} resources require a ref")
            try:
                snapshot = self.planes.snapshot(placement.plane, ref, revision, allow_missing=allow_missing_ref)
            except OperationError as exc:
                raise InventoryError(str(exc)) from exc
        return self._discover(family, placement, snapshot, environment, selection)

    def _discover(
        self,
        family: ResourceFamilyDefinition,
        placement: ResourcePlacement,
        snapshot: PlaneSnapshot,
        environment: str | None,
        selection: ResourceSelection | None,
    ) -> tuple[InventoryRecord, ...]:
        collection = self.registry.collection(placement.collection)
        context = CollectionReadContext(
            snapshot.root,
            self.repository_root,
            self.project,
            environment,
            family,
            placement,
            self.registry.api_kinds,
            self.registry.contracts_for(family.name, placement.contract_profile),
            snapshot.blob_ids,
            selection,
        )
        try:
            discovered = tuple(collection.provider.discover(context))
        except ResourceModelError as exc:
            location = f"environment {environment!r}, " if environment is not None else ""
            raise InventoryError(f"{location}{placement.plane} {family.plural}: {exc}") from exc
        records = tuple(self._record(item, family, snapshot, environment) for item in discovered)
        # A collection path is a storage claim.  Relationship-derived address
        # authentication is performed by presenters and relationship joins
        # when a record is used; discovery itself must remain able to surface
        # orphaned or transition-state records for diagnostics.
        identities: dict[ResourceAddress, InventoryRecord] = {}
        for record in records:
            previous = identities.get(record.logical_identity)
            if previous is not None:
                location = f"environment {environment!r}, " if environment is not None else ""
                raise InventoryError(
                    f"{location}{placement.plane}: duplicate logical {record.gvk} resource "
                    f"{record.qualified_name!r} "
                    f"at {previous.path} and {record.path}"
                )
            identities[record.logical_identity] = record
        return tuple(
            sorted(records, key=lambda item: (item.environment or "", str(item.gvk), item.qualified_name, item.path))
        )

    @staticmethod
    def _record(
        item: DiscoveredResource,
        family: ResourceFamilyDefinition,
        snapshot: PlaneSnapshot,
        environment: str | None,
    ) -> InventoryRecord:
        return InventoryRecord(
            environment,
            family,
            snapshot.plane,
            snapshot.ref,
            snapshot.revision,
            item.path,
            item.document,
            item.gvk,
            item.name,
            item.parsed,
            item.blob_id,
            item.content_digest,
            item.media_type,
            item.local_identity,
            item.storage_qualified_name,
            snapshot.root,
        )

    @staticmethod
    def _placement(family: ResourceFamilyDefinition, plane: ResourcePlane | None) -> ResourcePlacement:
        selected_plane = family.inspection.default_plane if plane is None and family.inspection is not None else plane
        candidates = tuple(item for item in family.placements if item.plane is selected_plane)
        if len(candidates) != 1:
            description = "default" if plane is None else str(plane)
            raise InventoryError(f"resource family {family.name!r} has no unambiguous {description} placement")
        return candidates[0]

    def deployment_refs(self, environment: str) -> tuple[str, str]:
        records = self.resources("environment", environment=environment)
        if len(records) != 1:
            raise InventoryError(f"expected exactly one Environment resource for {environment!r}")
        specification = records[0].document.get("spec")
        refs = specification.get("refs", {}) if isinstance(specification, dict) else {}
        if not isinstance(refs, dict):
            raise InventoryError(f"Environment {environment!r} has invalid refs")
        defaults = self.project.environment_defaults.refs
        desired = refs.get("desired") or defaults.desired.replace("{environment}", environment)
        observed = refs.get("observed") or defaults.observed.replace("{environment}", environment)
        if not isinstance(desired, str) or not isinstance(observed, str) or not desired or not observed:
            raise InventoryError(f"Environment {environment!r} has invalid desired or observed refs")
        if desired == observed:
            raise InventoryError(f"Environment {environment!r} desired and observed refs must differ")
        return desired, observed

    def environment_inventory(
        self,
        environment: str,
        *,
        desired_ref: str | None = None,
        desired_revision: str | None = None,
        observed_ref: str | None = None,
        observed_revision: str | None = None,
    ) -> tuple[tuple[InventoryRecord, ...], tuple[InventoryRecord, ...], tuple[InventoryRecord, ...]]:
        configured_desired, configured_observed = self.deployment_refs(environment)
        desired_ref = desired_ref or configured_desired
        observed_ref = observed_ref or configured_observed
        units = self.resources(
            "unit", environment=environment, ref=desired_ref, revision=desired_revision, allow_missing_ref=True
        )
        receipts = self.resources(
            "receipt", environment=environment, ref=observed_ref, revision=observed_revision, allow_missing_ref=True
        )
        artifacts = self.resources(
            "artifact",
            environment=environment,
            plane=ResourcePlane.OBSERVED,
            ref=observed_ref,
            revision=observed_revision,
            allow_missing_ref=True,
        )
        return units, receipts, artifacts

    def evaluate_environment(
        self,
        environment: str,
        *,
        desired_ref: str | None = None,
        desired_revision: str | None = None,
        observed_ref: str | None = None,
        observed_revision: str | None = None,
        resolve_artifacts: bool = True,
    ) -> RelationshipEvaluation:
        unit_view = self.registry.family("unit").inspection
        if unit_view is None or unit_view.observation is None or unit_view.artifact_description is None:
            raise InventoryError("resource family 'unit' has no registered inspection relationships")
        units, receipts, artifacts = self.environment_inventory(
            environment,
            desired_ref=desired_ref,
            desired_revision=desired_revision,
            observed_ref=observed_ref,
            observed_revision=observed_revision,
        )
        return evaluate_relationships(
            self.registry,
            units,
            receipts,
            artifacts if resolve_artifacts else (),
            resolve_artifacts=resolve_artifacts,
            observation=self.registry.observation(unit_view.observation),
            description=self.registry.artifact_description(unit_view.artifact_description),
            address_runtime=self,
        )

    def reconciliation_counts(self, environment: str) -> dict[ReconciliationState, int]:
        """Count desired Units and retained opaque cleanup roots."""

        evaluation = self.evaluate_environment(environment, resolve_artifacts=False)
        counts = {state: 0 for state in ReconciliationState}
        for unit in evaluation.units:
            counts[unit.reconciliation] += 1
        desired_ref, _observed_ref = self.deployment_refs(environment)
        snapshot = self.planes.snapshot(ResourcePlane.DESIRED, desired_ref, allow_missing=True)
        cleanup_directory = snapshot.root / ".gitopsctr" / "cleanup" / "units"
        cleanup_paths = (
            tuple(
                sorted(
                    path
                    for path in cleanup_directory.iterdir()
                    if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}
                )
            )
            if cleanup_directory.is_dir()
            else ()
        )
        cleanup_by_name: dict[str, Path] = {}
        for path in cleanup_paths:
            previous = cleanup_by_name.get(path.stem)
            if previous is not None:
                raise InventoryError(
                    f"environment {environment!r}, desired: duplicate opaque cleanup root {path.stem!r} "
                    f"at {previous.relative_to(snapshot.root)} and {path.relative_to(snapshot.root)}"
                )
            cleanup_by_name[path.stem] = path
        desired_names = {unit.unit.name for unit in evaluation.units}
        overlap = desired_names.intersection(cleanup_by_name)
        if overlap:
            raise InventoryError(
                f"environment {environment!r}, desired: Unit and opaque cleanup root identities overlap: "
                f"{sorted(overlap)}"
            )
        counts[ReconciliationState.WAIT] += len(cleanup_by_name)
        return counts

    def environment_summary(self, environment: str) -> EnvironmentInspectionSummary:
        """Return registry-presenter inputs for one environment namespace."""

        desired_ref, observed_ref = self.deployment_refs(environment)
        desired = self.planes.snapshot(ResourcePlane.DESIRED, desired_ref, allow_missing=True)
        observed = self.planes.snapshot(ResourcePlane.OBSERVED, observed_ref, allow_missing=True)
        counts = self.reconciliation_counts(environment)
        reconciliation = (
            " ".join(f"{state.value.lower()}={count}" for state, count in counts.items() if count) or "none"
        )
        return EnvironmentInspectionSummary(
            desired_ref,
            desired.revision,
            observed_ref,
            observed.revision,
            reconciliation,
        )

    def stack_inspection_summary(self, record: InspectionRecord) -> StackInspectionSummary:
        """Return one lazy Stack relationship summary for the record's snapshots."""

        if not isinstance(record, InventoryRecord):
            raise InventoryError("inspection presenter received a non-inventory record")
        if record.family.name not in {"stack", "stacktemplate"}:
            raise InventoryError(f"resource {record.name!r} has no Stack inspection summary")
        if record.environment is None or record.ref is None:
            raise InventoryError(f"resource {record.name!r} has no desired environment provenance")

        desired_key = (record.environment, record.ref, record.revision)
        observed_key = self._stack_inspection_observed_keys.get(desired_key)
        if observed_key is None:
            _configured_desired, observed_ref = self.deployment_refs(record.environment)
            observed = self.planes.snapshot(ResourcePlane.OBSERVED, observed_ref, allow_missing=True)
            observed_key = (observed_ref, observed.revision, True)
        observed_ref, observed_revision, allow_missing_observed_ref = observed_key
        self.prepare_stack_inspection(
            (record,),
            observed_ref=observed_ref,
            observed_revision=observed_revision,
            allow_missing_observed_ref=allow_missing_observed_ref,
        )
        cache_key = self._stack_inspection_key(record, observed_ref, observed_revision, allow_missing_observed_ref)
        summaries = self._stack_inspection_cache[cache_key]
        try:
            return summaries[record.path]
        except KeyError as exc:
            raise InventoryError(
                f"resource {record.name!r} is not present in its cached Stack inspection snapshot"
            ) from exc

    @staticmethod
    def _stack_inspection_key(
        record: InventoryRecord,
        observed_ref: str,
        observed_revision: str | None,
        allow_missing_observed_ref: bool,
    ) -> tuple[str, str, str | None, str, str | None, bool]:
        if record.environment is None or record.ref is None:
            raise InventoryError(f"resource {record.name!r} has no desired environment provenance")
        return (
            record.environment,
            record.ref,
            record.revision,
            observed_ref,
            observed_revision,
            allow_missing_observed_ref,
        )

    def prepare_stack_inspection(
        self,
        records: tuple[InventoryRecord, ...],
        *,
        observed_ref: str,
        observed_revision: str | None,
        allow_missing_observed_ref: bool = True,
    ) -> None:
        """Prepare indexed summaries only for the desired records being rendered."""

        if not records:
            return
        first = records[0]
        if first.family.name not in {"stack", "stacktemplate"}:
            return
        if any(
            record.environment != first.environment or record.ref != first.ref or record.revision != first.revision
            for record in records
        ):
            raise InventoryError("Stack inspection records must share one desired snapshot")
        cache_key = self._stack_inspection_key(first, observed_ref, observed_revision, allow_missing_observed_ref)
        desired_key = (cast(str, first.environment), cast(str, first.ref), first.revision)
        self._stack_inspection_observed_keys[desired_key] = (
            observed_ref,
            observed_revision,
            allow_missing_observed_ref,
        )
        summaries = self._stack_inspection_cache.setdefault(cache_key, {})
        missing = tuple(record for record in records if record.path not in summaries)
        if missing:
            summaries.update(
                self._build_stack_inspection_summaries(
                    missing,
                    observed_ref=observed_ref,
                    observed_revision=observed_revision,
                    allow_missing_observed_ref=allow_missing_observed_ref,
                )
            )

    @staticmethod
    def _active_projection_bindings(record: InventoryRecord) -> tuple[StackProjectionUnitBinding, ...]:
        specification = getattr(record.parsed, "spec", None)
        active = getattr(specification, "activeProjection", None)
        units = getattr(active, "units", None)
        return (
            tuple(value for value in units.values() if isinstance(value, StackProjectionUnitBinding))
            if isinstance(units, dict)
            else ()
        )

    @staticmethod
    def _template_reference(record: InventoryRecord) -> tuple[str, str, str] | None:
        specification = getattr(record.parsed, "spec", None)
        reference = getattr(specification, "templateRef", None)
        name = getattr(reference, "name", None)
        uid = getattr(reference, "uid", None)
        digest = getattr(reference, "contentDigest", None)
        if not all(isinstance(value, str) for value in (name, uid, digest)):
            return None
        return cast(str, name), cast(str, uid), cast(str, digest)

    @staticmethod
    def _raw_stack_names_for_templates(root: Path, templates: tuple[InventoryRecord, ...]) -> frozenset[str]:
        fences = {
            (template.name, _metadata_value(template, "uid"), _specification_value(template, "contentDigest"))
            for template in templates
        }
        if not fences:
            return frozenset()
        stack_directory = root / "stacks"
        if not stack_directory.is_dir():
            return frozenset()
        names: set[str] = set()
        for path in sorted(stack_directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                continue
            try:
                document = load_document(path)
            except Exception:
                continue
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata")
            specification = document.get("spec")
            reference = specification.get("templateRef") if isinstance(specification, dict) else None
            fence = (
                (
                    reference.get("name"),
                    reference.get("uid"),
                    reference.get("contentDigest"),
                )
                if isinstance(reference, dict)
                else None
            )
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if isinstance(name, str) and fence in fences:
                names.add(name)
        return frozenset(names)

    def _build_stack_inspection_summaries(
        self,
        records: tuple[InventoryRecord, ...],
        *,
        observed_ref: str,
        observed_revision: str | None,
        allow_missing_observed_ref: bool,
    ) -> dict[PurePosixPath, StackInspectionSummary]:
        stack_template_relation = self.registry.graph_relationship("stack-selects-stacktemplate")
        stack_unit_relation = self.registry.graph_relationship("stack-owns-unit")
        first = records[0]
        if first.environment is None or first.ref is None:
            raise InventoryError(f"resource {first.name!r} has no desired environment provenance")
        desired_ref, desired_revision = first.ref, first.revision
        if first.family.name == "stack":
            stacks = records
            template_names = frozenset(
                reference[0] for stack in stacks if (reference := self._template_reference(stack)) is not None
            )
            templates = self.resources(
                stack_template_relation.target_family,
                environment=first.environment,
                plane=stack_template_relation.target_plane,
                ref=desired_ref,
                revision=desired_revision,
                selection=_names_selection(template_names),
            )
            unit_names = frozenset(
                binding.name for stack in stacks for binding in self._active_projection_bindings(stack)
            )
            units = self.resources(
                stack_unit_relation.target_family,
                environment=first.environment,
                plane=stack_unit_relation.target_plane,
                ref=desired_ref,
                revision=desired_revision,
                selection=_names_selection(unit_names),
            )
            templates_for_references = templates
        else:
            templates = records
            stack_names = self._raw_stack_names_for_templates(first.snapshot_root or Path(), templates)
            stacks = self.resources(
                stack_template_relation.source_family,
                environment=first.environment,
                plane=stack_template_relation.source_plane,
                ref=desired_ref,
                revision=desired_revision,
                selection=_names_selection(stack_names),
            )
            units = ()
            templates_for_references = templates

        template_by_fence = {
            (template.name, _metadata_value(template, "uid"), _specification_value(template, "contentDigest")): template
            for template in templates
        }
        units_by_identity: dict[tuple[str, str, str], list[InventoryRecord]] = {}
        units_by_binding: dict[tuple[str, str, str, str, str], InventoryRecord] = {}
        units_by_owner: dict[tuple[str, str, str, str], list[InventoryRecord]] = {}
        for unit in units:
            identity_key = (unit.gvk.api_version, unit.gvk.kind, unit.name)
            units_by_identity.setdefault(identity_key, []).append(unit)
            parsed = unit.parsed
            if isinstance(parsed, UnitResource) and parsed.metadata.uid is not None:
                digest = desired_unit_binding_digest(parsed)
                units_by_binding[(*identity_key, parsed.metadata.uid, digest)] = unit
                owners = parsed.metadata.ownerReferences or []
                if len(owners) == 1:
                    owner = owners[0]
                    units_by_owner.setdefault((owner.apiVersion, owner.kind, owner.name, owner.uid), []).append(unit)

        valid_units: list[InventoryRecord] = []
        child_observations: dict[PurePosixPath, list[str]] = {}
        unit_states: dict[PurePosixPath, UnitOperationalState] = {}
        unit_view = self.registry.family(stack_unit_relation.target_family).inspection
        if unit_view is None or unit_view.observation is None:
            raise InventoryError(
                f"resource family {stack_unit_relation.target_family!r} has no registered observation relationship"
            )
        observation = self.registry.observation(unit_view.observation)
        active_names = {binding.name for stack in stacks for binding in self._active_projection_bindings(stack)}
        receipts = self.resources(
            observation.observer_family,
            environment=first.environment,
            ref=observed_ref,
            revision=observed_revision,
            allow_missing_ref=allow_missing_observed_ref,
            selection=_names_selection(frozenset(active_names)),
        )

        def record_child_state(stack: InventoryRecord, binding: StackProjectionUnitBinding) -> InventoryRecord | None:
            name = binding.name
            key = (binding.apiVersion, binding.kind, name)
            exact = units_by_binding.get((*key, binding.uid, binding.desiredDigest))
            if exact is None:
                candidates = units_by_identity.get(key, [])
                reason = "missing" if not candidates else "mismatch"
                child_observations.setdefault(stack.path, []).append(f"BROKEN({name}:{reason})")
                return None
            owner_key = (
                stack.gvk.api_version,
                stack.gvk.kind,
                stack.name,
                _metadata_value(stack, "uid") or "",
            )
            if exact not in units_by_owner.get(owner_key, ()):
                child_observations.setdefault(stack.path, []).append(f"BROKEN({name}:owner)")
                return None
            try:
                stack_unit_relation.binding.validate(_graph_parsed(stack), exact.parsed)
            except ResourceModelError:
                child_observations.setdefault(stack.path, []).append(f"BROKEN({name}:owner)")
                return None
            valid_units.append(exact)
            return exact

        by_path: dict[PurePosixPath, StackInspectionSummary] = {}
        template_references: dict[PurePosixPath, list[str]] = {
            template.path: [] for template in templates_for_references
        }
        for stack in stacks:
            reference = self._template_reference(stack)
            template = template_by_fence.get(reference) if reference is not None else None
            if template is None:
                if first.family.name == "stacktemplate":
                    continue
                raise InventoryError(
                    f"environment {first.environment!r}, desired Stack {stack.name!r} has no uniquely fenced "
                    "StackTemplate relationship"
                )
            try:
                stack_template_relation.binding.validate(_graph_parsed(stack), _graph_parsed(template))
            except ResourceModelError as exc:
                if first.family.name == "stacktemplate":
                    continue
                raise InventoryError(
                    f"environment {first.environment!r}, desired Stack {stack.name!r} has a broken "
                    "StackTemplate relationship"
                ) from exc
            template_references.setdefault(template.path, []).append(stack.name)
            if first.family.name != "stack":
                continue
            for binding in self._active_projection_bindings(stack):
                record_child_state(stack, binding)
            by_path[stack.path] = StackInspectionSummary(
                template_name=template.name,
                template_uid=_metadata_value(template, "uid"),
                template_digest=_specification_value(template, "contentDigest"),
                child_observations=tuple(sorted(set(child_observations.get(stack.path, ())))) or ("N/A",),
            )
        if valid_units:
            unit_view = self.registry.family("unit").inspection
            if unit_view is None or unit_view.observation is None or unit_view.artifact_description is None:
                raise InventoryError("resource family 'unit' has no registered inspection relationships")
            evaluation = evaluate_relationships(
                self.registry,
                tuple({unit.path: unit for unit in valid_units}.values()),
                receipts,
                (),
                resolve_artifacts=False,
                observation=self.registry.observation(unit_view.observation),
                description=self.registry.artifact_description(unit_view.artifact_description),
                address_runtime=self,
            )
            unit_states = {value.unit.path: value for value in evaluation.units}
        for stack in stacks:
            if stack.path not in by_path:
                continue
            statuses = child_observations.setdefault(stack.path, [])
            for binding in self._active_projection_bindings(stack):
                key = (binding.apiVersion, binding.kind, binding.name)
                exact = units_by_binding.get((*key, binding.uid, binding.desiredDigest))
                if exact is not None and exact.path in unit_states:
                    statuses.append(unit_states[exact.path].observation.value)
            by_path[stack.path] = replace(
                by_path[stack.path],
                child_observations=tuple(sorted(set(statuses))) or ("N/A",),
            )
        for template in templates_for_references:
            by_path[template.path] = StackInspectionSummary(
                references=tuple(sorted(template_references[template.path]))
            )
        return by_path

    def resource_partition(self, record: object) -> str | None:
        """Resolve a desired resource's partition, following UID-fenced owner references."""

        if not isinstance(record, InventoryRecord):
            raise InventoryError("inspection presenter received a non-inventory record")
        if record.plane is not ResourcePlane.DESIRED or record.environment is None or record.ref is None:
            raise InventoryError(f"resource {record.name!r} has no desired environment provenance")
        return self._resource_partition(record, set())

    def _resource_partition(
        self,
        record: InventoryRecord,
        visited: set[tuple[str, str, str, str]],
    ) -> str | None:
        metadata = record.document.get("metadata")
        if not isinstance(metadata, dict):
            raise InventoryError(f"desired resource {record.name!r} has invalid metadata")
        labels = metadata.get("labels")
        partition = labels.get("gitopsctr.io/partition") if isinstance(labels, dict) else None
        owners = metadata.get("ownerReferences")
        if owners is None:
            return partition if isinstance(partition, str) else None
        if not isinstance(owners, list) or len(owners) != 1 or not isinstance(owners[0], dict):
            raise InventoryError(f"desired resource {record.name!r} has invalid owner references")
        owner = owners[0]
        api_version, kind, name, uid = (
            owner.get("apiVersion"),
            owner.get("kind"),
            owner.get("name"),
            owner.get("uid"),
        )
        if not all(isinstance(value, str) and value for value in (api_version, kind, name, uid)):
            raise InventoryError(f"desired resource {record.name!r} has an invalid owner identity")
        assert isinstance(api_version, str)
        assert isinstance(kind, str)
        assert isinstance(name, str)
        assert isinstance(uid, str)
        key = (api_version, kind, name, uid)
        if key in visited:
            raise InventoryError(f"desired ownership cycle includes {api_version}/{kind} {name!r}")
        visited.add(key)
        try:
            owner_gvk = GVK(api_version, kind)
            owner_family = self.registry.family_for_api_kind(owner_gvk)
        except (KeyError, ValueError) as exc:
            raise InventoryError(f"desired resource {record.name!r} references an unregistered owner") from exc
        candidates = self.resources(
            owner_family.name,
            environment=record.environment,
            plane=ResourcePlane.DESIRED,
            ref=record.ref,
            revision=record.revision,
            selection=_names_selection(frozenset((name,))),
        )
        owners_by_identity = [
            candidate for candidate in candidates if candidate.gvk == owner_gvk and candidate.name == name
        ]
        if len(owners_by_identity) != 1:
            raise InventoryError(f"desired resource {record.name!r} references a missing owner {owner_gvk} {name!r}")
        owner_record = owners_by_identity[0]
        owner_metadata = owner_record.document.get("metadata")
        if not isinstance(owner_metadata, dict) or owner_metadata.get("uid") != uid:
            raise InventoryError(f"desired resource {record.name!r} has a stale owner UID fence")
        return self._resource_partition(owner_record, visited)


def evaluate_relationships(
    registry: ResourceRegistry,
    units: tuple[InventoryRecord, ...],
    receipts: tuple[InventoryRecord, ...],
    artifacts: tuple[InventoryRecord, ...],
    *,
    resolve_artifacts: bool = True,
    strict_artifacts: bool = True,
    observation: ObservationDefinition | None = None,
    description: ArtifactDescriptionDefinition | None = None,
    address_runtime: InventorySession | None = None,
) -> RelationshipEvaluation:
    """Evaluate registered observations and artifact descriptions without joining documents."""

    if observation is None or description is None:
        unit_families = {item.family.name for item in units}
        if len(unit_families) == 1:
            unit_family = registry.family(next(iter(unit_families)))
            view = unit_family.inspection
            if view is None or view.observation is None or view.artifact_description is None:
                raise InventoryError(
                    f"resource family {unit_family.name!r} has no registered observation and "
                    "artifact-description relationships"
                )
            observation = observation or registry.observation(view.observation)
            description = description or registry.artifact_description(view.artifact_description)
        else:
            observer_families = {item.family.name for item in receipts}
            if observation is None:
                observations = tuple(
                    definition
                    for definition in registry.observations
                    if (not unit_families or definition.subject_family in unit_families)
                    and (not observer_families or definition.observer_family in observer_families)
                )
                if len(observations) != 1:
                    raise InventoryError("registered Unit observation relationship is not unambiguous")
                observation = observations[0]
            if description is None:
                descriptions = tuple(
                    definition
                    for definition in registry.artifact_descriptions
                    if definition.producer_family == observation.subject_family
                    and definition.describer_family == observation.observer_family
                )
                if len(descriptions) != 1:
                    raise InventoryError("registered Unit artifact-description relationship is not unambiguous")
                description = descriptions[0]
    units_by_qualified_name = {item.qualified_name: item for item in units}
    artifacts_by_path = {item.path: item.relationship_resource() for item in artifacts}
    artifact_records_by_path = {item.path: item for item in artifacts}
    receipt_by_subject: dict[str, InventoryRecord] = {}
    states_by_receipt: dict[PurePosixPath, ReceiptOperationalState] = {}
    linked_artifact_paths: set[PurePosixPath] = set()
    artifact_states_by_path: dict[PurePosixPath, ArtifactOperationalState] = {}

    for receipt in receipts:
        observer = receipt.relationship_resource()
        try:
            subject_identity = observation.binding.subject_identity(observer)
        except ResourceModelError as exc:
            raise InventoryError(f"environment {receipt.environment!r}, observed {receipt.path}: {exc}") from exc
        subject_qualified_name = receipt.qualified_name
        if subject_qualified_name in receipt_by_subject:
            previous = receipt_by_subject[subject_qualified_name]
            raise InventoryError(
                f"environment {receipt.environment!r}: Receipts {previous.path} and {receipt.path} both observe "
                f"{subject_identity.gvk} {subject_qualified_name!r}"
            )
        receipt_by_subject[subject_qualified_name] = receipt
        unit = units_by_qualified_name.get(subject_qualified_name)
        if unit is not None and unit.identity != subject_identity:
            raise InventoryError(
                f"environment {receipt.environment!r}, observed {receipt.path}: Receipt subject identity "
                f"does not authenticate desired Unit {subject_qualified_name!r}"
            )
        if unit is not None and address_runtime is not None:
            specification = receipt.document.get("spec")
            subject = specification.get("subject") if isinstance(specification, dict) else None
            qualified_name = subject.get("qualifiedName") if isinstance(subject, dict) else None
            expected_qualified_name = address_runtime.resource_qualified_name(unit)
            if qualified_name != expected_qualified_name:
                raise InventoryError(
                    f"environment {receipt.environment!r}, observed {receipt.path}: Receipt subject qualifiedName "
                    f"does not authenticate desired Unit {expected_qualified_name!r}"
                )
        producer = unit.relationship_resource() if unit is not None else None
        try:
            freshness = observation.binding.evaluate(observer, producer) if producer is not None else None
            state = (
                InventoryObservationState.ORPHAN if freshness is None else InventoryObservationState(freshness.value)
            )
            artifact_count = description.binding.descriptor_count(observer)
            links: tuple[ArtifactLink, ...] = ()
            if resolve_artifacts and freshness is not None and producer is not None:
                links = description.binding.resolve(
                    observer,
                    ArtifactResolutionContext(
                        producer,
                        subject_qualified_name,
                        artifacts_by_path,
                        registry.artifact_outputs_for(subject_identity.gvk),
                        freshness,
                    ),
                )
        except (KeyError, ResourceModelError) as exc:
            raise InventoryError(f"environment {receipt.environment!r}, observed {receipt.path}: {exc}") from exc
        states_by_receipt[receipt.path] = ReceiptOperationalState(receipt, state, unit, links, artifact_count)
        linked_artifact_paths.update(link.artifact.path for link in links)
        for link in links:
            artifact = artifact_records_by_path[link.artifact.path]
            if unit is not None and address_runtime is not None:
                producer = artifact.document.get("producer")
                qualified_name = producer.get("qualifiedName") if isinstance(producer, dict) else None
                expected_qualified_name = address_runtime.resource_qualified_name(unit)
                if qualified_name != expected_qualified_name:
                    raise InventoryError(
                        f"environment {artifact.environment!r}, observed {artifact.path}: Artifact producer "
                        f"qualifiedName does not authenticate desired Unit {expected_qualified_name!r}"
                    )
            artifact_states_by_path[artifact.path] = ArtifactOperationalState(
                artifact,
                state,
                unit if state is InventoryObservationState.CURRENT else None,
                receipt,
            )

    orphan_artifact_paths = set(artifacts_by_path).difference(linked_artifact_paths)
    if strict_artifacts and orphan_artifact_paths:
        paths = ", ".join(str(path) for path in sorted(orphan_artifact_paths))
        environment = artifacts[0].environment if artifacts else None
        raise InventoryError(f"environment {environment!r}: observed Artifacts are not described by a Receipt: {paths}")

    for artifact in artifacts:
        if artifact.path in artifact_states_by_path:
            continue
        artifact_states_by_path[artifact.path] = ArtifactOperationalState(
            artifact,
            InventoryObservationState.ORPHAN,
            None,
            None,
        )

    unit_states: list[UnitOperationalState] = []
    for unit in units:
        receipt = receipt_by_subject.get(unit.qualified_name)
        observation_state = (
            InventoryObservationState.MISSING if receipt is None else states_by_receipt[receipt.path].observation
        )
        if unit.snapshot_root is None:
            raise InventoryError(f"desired Unit {unit.name!r} has no materialized snapshot root")
        if not isinstance(unit.parsed, UnitResource):
            raise InventoryError(f"desired Unit {unit.name!r} has no typed Unit representation")
        try:
            transition_reason = load_desired_transition_blocks(unit.snapshot_root).get(unit.qualified_name)
            status = classify_before_observation(
                unit.snapshot_root,
                unit.qualified_name,
                unit.document,
                unit.parsed,
                transition_reason,
            )
            if status is None:
                evidence = (
                    ObservationEvidence.MISSING
                    if receipt is None
                    else ObservationEvidence(states_by_receipt[receipt.path].observation.value)
                )
                status = classify_observation(evidence)
        except (OperationError, ValueError) as exc:
            raise InventoryError(f"environment {unit.environment!r}, desired {unit.path}: {exc}") from exc
        if status.reconciliation is ReconciliationState.MATERIALIZED:
            observation_state = InventoryObservationState.NOT_APPLICABLE
        receipt_state = states_by_receipt.get(receipt.path) if receipt is not None else None
        unit_states.append(
            UnitOperationalState(
                unit,
                observation_state,
                status.reconciliation,
                status.reason,
                receipt,
                (
                    receipt_state.artifacts
                    if receipt_state is not None and receipt_state.observation is InventoryObservationState.CURRENT
                    else ()
                ),
            )
        )

    return RelationshipEvaluation(
        tuple(sorted(unit_states, key=lambda item: (item.unit.environment or "", item.unit.name, str(item.unit.gvk)))),
        tuple(sorted(states_by_receipt.values(), key=lambda item: (item.receipt.environment or "", item.receipt.name))),
        tuple(
            sorted(
                artifact_states_by_path.values(),
                key=lambda item: (item.artifact.environment or "", item.artifact.qualified_name),
            )
        ),
    )


def evaluate_observation_relationship(
    observation: ObservationDefinition,
    subjects: tuple[InventoryRecord, ...],
    observers: tuple[InventoryRecord, ...],
) -> ObservationRelationshipEvaluation:
    """Evaluate an observation without assuming Unit, Receipt, or Artifact semantics."""

    subjects_by_identity = {item.identity: item for item in subjects}
    observer_by_subject: dict[ResourceIdentity, InventoryRecord] = {}
    observer_states: list[ObservationOperationalState] = []
    for observer in observers:
        relationship_observer = observer.relationship_resource()
        try:
            subject_identity = observation.binding.subject_identity(relationship_observer)
        except ResourceModelError as exc:
            raise InventoryError(f"environment {observer.environment!r}, observed {observer.path}: {exc}") from exc
        if subject_identity in observer_by_subject:
            previous = observer_by_subject[subject_identity]
            raise InventoryError(
                f"environment {observer.environment!r}: observers {previous.path} and {observer.path} both observe "
                f"{subject_identity.gvk} {subject_identity.name!r}"
            )
        observer_by_subject[subject_identity] = observer
        subject = subjects_by_identity.get(subject_identity)
        if subject is None:
            state = InventoryObservationState.ORPHAN
        else:
            try:
                state = InventoryObservationState(
                    observation.binding.evaluate(relationship_observer, subject.relationship_resource()).value
                )
            except ResourceModelError as exc:
                raise InventoryError(f"environment {observer.environment!r}, observed {observer.path}: {exc}") from exc
        observer_states.append(ObservationOperationalState(observer, state, subject))

    observer_state_by_path = {item.resource.path: item for item in observer_states}
    subject_states: list[ObservationOperationalState] = []
    for subject in subjects:
        observer = observer_by_subject.get(subject.identity)
        state = (
            InventoryObservationState.MISSING if observer is None else observer_state_by_path[observer.path].observation
        )
        subject_states.append(ObservationOperationalState(subject, state, observer))
    return ObservationRelationshipEvaluation(tuple(subject_states), tuple(observer_states))
