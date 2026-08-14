"""Registry-driven resource inventory and relationship evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from gitopsctr.api import GVK
from gitopsctr.document import JsonObject
from gitopsctr.errors import OperationError
from gitopsctr.formats import DocumentFormatError, load_project_config
from gitopsctr.operational import (
    ObservationEvidence,
    ReconciliationState,
    classify_before_observation,
    classify_observation,
    load_desired_transition_blocks,
)
from gitopsctr.plane_repositories import PlaneRepositorySession, PlaneSnapshot
from gitopsctr.resource_model import (
    ArtifactLink,
    ArtifactResolutionContext,
    CollectionReadContext,
    DiscoveredResource,
    ObservationState,
    RelationshipResource,
    ResourceFamilyDefinition,
    ResourceIdentity,
    ResourceModelError,
    ResourcePlacement,
    ResourcePlane,
    ResourceRegistry,
)
from gitopsctr.resources import UnitResource


class InventoryError(OperationError):
    """A persisted resource or registered relationship cannot be inspected."""


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
    identity_qualifier: tuple[str, ...] = ()
    snapshot_root: Path | None = None

    @property
    def identity(self) -> ResourceIdentity:
        return ResourceIdentity(self.gvk.api_version, self.gvk.kind, self.name)

    @property
    def logical_identity(self) -> tuple[str, ...]:
        """Collection-owned identity used to detect duplicate persisted resources."""

        return (self.family.name, self.name, *self.identity_qualifier)

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
class RelationshipEvaluation:
    units: tuple[UnitOperationalState, ...]
    receipts: tuple[ReceiptOperationalState, ...]


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

    def __enter__(self) -> InventorySession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_planes:
            self.planes.close()

    def resources(
        self,
        selector: str,
        *,
        environment: str | None = None,
        plane: ResourcePlane | None = None,
        ref: str | None = None,
        revision: str | None = None,
        allow_missing_ref: bool = False,
        names: frozenset[str] | None = None,
        producer_names: frozenset[str] | None = None,
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
        return self._discover(family, placement, snapshot, environment, names, producer_names)

    def _discover(
        self,
        family: ResourceFamilyDefinition,
        placement: ResourcePlacement,
        snapshot: PlaneSnapshot,
        environment: str | None,
        names: frozenset[str] | None,
        producer_names: frozenset[str] | None,
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
            names,
            producer_names,
        )
        try:
            discovered = tuple(collection.provider.discover(context))
        except ResourceModelError as exc:
            location = f"environment {environment!r}, " if environment is not None else ""
            raise InventoryError(f"{location}{placement.plane} {family.plural}: {exc}") from exc
        records = tuple(self._record(item, family, snapshot, environment) for item in discovered)
        identities: dict[tuple[str, ...], InventoryRecord] = {}
        for record in records:
            previous = identities.get(record.logical_identity)
            if previous is not None:
                location = f"environment {environment!r}, " if environment is not None else ""
                raise InventoryError(
                    f"{location}{placement.plane}: duplicate logical {record.gvk} resource {record.name!r} "
                    f"at {previous.path} and {record.path}"
                )
            identities[record.logical_identity] = record
        return tuple(sorted(records, key=lambda item: (item.environment or "", str(item.gvk), item.name, item.path)))

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
            item.identity_qualifier,
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
        return evaluate_relationships(
            self.registry,
            *self.environment_inventory(
                environment,
                desired_ref=desired_ref,
                desired_revision=desired_revision,
                observed_ref=observed_ref,
                observed_revision=observed_revision,
            ),
            resolve_artifacts=resolve_artifacts,
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

    def environment_summary(self, environment: str) -> tuple[str, str | None, str, str | None, str]:
        """Return registry-presenter inputs for one environment namespace."""

        desired_ref, observed_ref = self.deployment_refs(environment)
        desired = self.planes.snapshot(ResourcePlane.DESIRED, desired_ref, allow_missing=True)
        observed = self.planes.snapshot(ResourcePlane.OBSERVED, observed_ref, allow_missing=True)
        counts = self.reconciliation_counts(environment)
        reconciliation = (
            " ".join(f"{state.value.lower()}={count}" for state, count in counts.items() if count) or "none"
        )
        return desired_ref, desired.revision, observed_ref, observed.revision, reconciliation

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
            names=frozenset((name,)),
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
) -> RelationshipEvaluation:
    """Evaluate registered observations and artifact descriptions without joining documents."""

    view = registry.family("unit").inspection
    if view is None or view.observation is None or view.artifact_description is None:
        raise InventoryError("Unit inspection has no registered observation and artifact-description relationships")
    observation = registry.observation(view.observation)
    description = registry.artifact_description(view.artifact_description)
    units_by_identity = {item.identity: item for item in units}
    artifacts_by_path = {item.path: item.relationship_resource() for item in artifacts}
    receipt_by_subject: dict[ResourceIdentity, InventoryRecord] = {}
    states_by_receipt: dict[PurePosixPath, ReceiptOperationalState] = {}
    linked_artifact_paths: set[PurePosixPath] = set()
    described_producer_names: set[str] = set()

    for receipt in receipts:
        observer = receipt.relationship_resource()
        try:
            subject_identity = observation.binding.subject_identity(observer)
        except ResourceModelError as exc:
            raise InventoryError(f"environment {receipt.environment!r}, observed {receipt.path}: {exc}") from exc
        if subject_identity in receipt_by_subject:
            previous = receipt_by_subject[subject_identity]
            raise InventoryError(
                f"environment {receipt.environment!r}: Receipts {previous.path} and {receipt.path} both observe "
                f"{subject_identity.gvk} {subject_identity.name!r}"
            )
        receipt_by_subject[subject_identity] = receipt
        unit = units_by_identity.get(subject_identity)
        producer = unit.relationship_resource() if unit is not None else None
        try:
            freshness = observation.binding.evaluate(observer, producer) if producer is not None else None
            state = (
                InventoryObservationState.ORPHAN if freshness is None else InventoryObservationState(freshness.value)
            )
            artifact_count = description.binding.descriptor_count(observer)
            links: tuple[ArtifactLink, ...] = ()
            if resolve_artifacts and freshness is ObservationState.CURRENT and producer is not None:
                links = description.binding.resolve(
                    observer,
                    ArtifactResolutionContext(
                        producer,
                        artifacts_by_path,
                        registry.artifact_outputs_for(subject_identity.gvk),
                        freshness,
                    ),
                )
        except (KeyError, ResourceModelError) as exc:
            raise InventoryError(f"environment {receipt.environment!r}, observed {receipt.path}: {exc}") from exc
        states_by_receipt[receipt.path] = ReceiptOperationalState(receipt, state, unit, links, artifact_count)
        described_producer_names.add(subject_identity.name)
        linked_artifact_paths.update(link.artifact.path for link in links)

    orphan_artifact_paths = {
        path
        for path in set(artifacts_by_path).difference(linked_artifact_paths)
        if len(path.parts) < 2 or path.parts[-2] not in described_producer_names
    }
    if orphan_artifact_paths:
        paths = ", ".join(str(path) for path in sorted(orphan_artifact_paths))
        environment = artifacts[0].environment if artifacts else None
        raise InventoryError(f"environment {environment!r}: observed Artifacts are not described by a Receipt: {paths}")

    unit_states: list[UnitOperationalState] = []
    for unit in units:
        receipt = receipt_by_subject.get(unit.identity)
        observation_state = (
            InventoryObservationState.MISSING if receipt is None else states_by_receipt[receipt.path].observation
        )
        if unit.snapshot_root is None:
            raise InventoryError(f"desired Unit {unit.name!r} has no materialized snapshot root")
        if not isinstance(unit.parsed, UnitResource):
            raise InventoryError(f"desired Unit {unit.name!r} has no typed Unit representation")
        try:
            transition_reason = load_desired_transition_blocks(unit.snapshot_root).get(unit.name)
            status = classify_before_observation(
                unit.snapshot_root,
                unit.name,
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
                receipt_state.artifacts if receipt_state is not None else (),
            )
        )

    return RelationshipEvaluation(
        tuple(sorted(unit_states, key=lambda item: (item.unit.environment or "", item.unit.name, str(item.unit.gvk)))),
        tuple(sorted(states_by_receipt.values(), key=lambda item: (item.receipt.environment or "", item.receipt.name))),
    )
