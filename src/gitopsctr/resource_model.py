"""Executable definitions for resource placement and cross-resource invariants."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

from gitopsctr.api import GVK, ApiKind
from gitopsctr.document import ContractError, JsonObject, JsonValue, TypedDocumentContract, require_json_value
from gitopsctr.formats import PROJECT_CONFIG_NAMES, Project, load_document


class ResourceModelError(ValueError):
    """A resource-model definition or persisted relationship is inconsistent."""


class ResourcePlane(StrEnum):
    SOURCE = "source"
    DESIRED = "desired"
    OBSERVED = "observed"


class ResourceScope(StrEnum):
    PROJECT = "project"
    ENVIRONMENT = "environment"


@runtime_checkable
class InspectionRecord(Protocol):
    """Persisted-record surface available to registry-owned presenters."""

    @property
    def document(self) -> JsonObject: ...

    @property
    def gvk(self) -> GVK: ...

    @property
    def name(self) -> str: ...

    @property
    def blob_id(self) -> str | None: ...


@dataclass(frozen=True)
class EnvironmentInspectionSummary:
    """Named registry-presenter inputs for one environment namespace."""

    desired_ref: str
    desired_revision: str | None
    observed_ref: str
    observed_revision: str | None
    reconciliation: str


@dataclass(frozen=True)
class StackInspectionSummary:
    """Relationship-derived facts for one Stack or StackTemplate table row."""

    template_name: str | None = None
    template_uid: str | None = None
    template_digest: str | None = None
    references: tuple[str, ...] = ()
    child_observations: tuple[str, ...] = ()


@runtime_checkable
class InspectionRuntime(Protocol):
    """Inventory services available to registry-owned presenters."""

    def environment_summary(self, environment: str) -> EnvironmentInspectionSummary: ...

    def resource_partition(self, record: InspectionRecord) -> str | None: ...

    def stack_inspection_summary(self, record: InspectionRecord) -> StackInspectionSummary: ...


@runtime_checkable
class InspectionPresenter(Protocol):
    """Executable table presenter for one inspectable resource family."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]: ...


class ObservationCardinality(StrEnum):
    ZERO_OR_ONE = "zero-or-one"


class ObservationState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


class CollectionLayout(StrEnum):
    PROJECT = "project"
    ENVIRONMENTS = "environments"
    SOURCE_UNITS = "source-units"
    SOURCE_STACKS = "source-stacks"
    SOURCE_STACKTEMPLATES = "source-stacktemplates"
    DESIRED_UNITS = "desired-units"
    DESIRED_STACKS = "desired-stacks"
    DESIRED_STACKTEMPLATES = "desired-stacktemplates"
    DESIRED_PROMOTION = "desired-promotion"
    OBSERVED_RECEIPTS = "observed-receipts"
    OBSERVED_ARTIFACTS = "observed-artifacts"


@runtime_checkable
class ApiKindMembership(Protocol):
    """Executable family membership and representation-contract rule."""

    def matches(self, api_kind: ApiKind[object]) -> bool: ...

    def contract(self, api_kind: ApiKind[object], profile: str) -> TypedDocumentContract[Any] | None: ...


@dataclass(frozen=True)
class ProfiledApiMembership:
    """Membership for exact GVKs whose API spec supplies named contracts."""

    gvks: frozenset[GVK]
    interface: type[object]

    def matches(self, api_kind: ApiKind[object]) -> bool:
        return api_kind.gvk in self.gvks and isinstance(api_kind.spec, self.interface)

    def contract(self, api_kind: ApiKind[object], profile: str) -> TypedDocumentContract[Any] | None:
        provider = getattr(api_kind.spec, "contract", None)
        value = provider(profile) if callable(provider) else None
        return value if isinstance(value, TypedDocumentContract) else None


@dataclass(frozen=True)
class UnitApiMembership:
    """Membership and contract selection for installed UnitDriver APIs."""

    interface: type[object]

    def matches(self, api_kind: ApiKind[object]) -> bool:
        return isinstance(api_kind.spec, self.interface)

    def contract(self, api_kind: ApiKind[object], profile: str) -> TypedDocumentContract[Any] | None:
        attribute = {"authored": "unit_contract", "desired": "desired_unit_contract"}.get(profile)
        value = getattr(api_kind.spec, attribute, None) if attribute is not None else None
        if not isinstance(value, TypedDocumentContract):
            return None
        return UnitResourceContract(api_kind, value, profile)


@dataclass(frozen=True)
class UnitResourceContract(TypedDocumentContract[Any]):
    """Adapt a Unit driver's specification contract to its persisted resource envelope."""

    api_kind: ApiKind[object]
    specification: TypedDocumentContract[Any]
    profile: str

    def parse(self, document: object) -> object:
        from gitopsctr.contracts import DesiredResourceMetadata
        from gitopsctr.resources import ResourceMetadata, UnitResource

        try:
            value = require_json_value(document)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            allowed = {"$schema", "apiVersion", "kind", "metadata", "spec"}
            unexpected = set(value) - allowed
            if unexpected:
                raise ValueError(f"unexpected Unit resource fields: {sorted(unexpected)}")
            schema_hint = value.get("$schema")
            if schema_hint is not None and not isinstance(schema_hint, str):
                raise ValueError("Unit resource $schema must be a string")
            if value.get("apiVersion") != self.api_kind.gvk.api_version or value.get("kind") != self.api_kind.gvk.kind:
                raise ValueError(f"expected {self.api_kind.gvk}")
            metadata = value.get("metadata")
            specification = value.get("spec")
            if not isinstance(metadata, dict) or not isinstance(specification, dict):
                raise ValueError("Unit resource requires metadata and spec objects")
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Unit resource metadata requires a non-empty name")
            if self.profile == "authored":
                if set(metadata) != {"name"}:
                    raise ValueError("authored Unit metadata may contain only name")
                parsed_metadata = ResourceMetadata(name=name)
            elif self.profile == "desired":
                desired = DesiredResourceMetadata.from_dict(metadata)
                parsed_metadata = ResourceMetadata(
                    name=desired.name,
                    uid=desired.uid,
                    labels=desired.labels,
                    ownerReferences=desired.ownerReferences,
                    deletion=desired.deletion,
                )
            else:
                raise ValueError(f"unsupported Unit resource profile {self.profile!r}")
            parsed_specification = self.specification.parse(specification)
            return UnitResource(self.api_kind.gvk, parsed_metadata, cast(Any, self.api_kind.spec), parsed_specification)
        except (TypeError, ValueError, LookupError) as exc:
            raise ContractError(str(exc)) from exc

    def dump(self, value: object) -> JsonObject:
        from gitopsctr.resources import UnitResource

        if (
            not isinstance(value, UnitResource)
            or value.gvk != self.api_kind.gvk
            or value.driver is not self.api_kind.spec
        ):
            raise ContractError(f"expected a typed {self.api_kind.gvk} Unit resource")
        try:
            metadata = value.metadata.document(profile=cast(Any, self.profile))
            return {
                "apiVersion": self.api_kind.gvk.api_version,
                "kind": self.api_kind.gvk.kind,
                "metadata": metadata,
                "spec": self.specification.dump(value.spec),
            }
        except (TypeError, ValueError, LookupError) as exc:
            raise ContractError(str(exc)) from exc

    def validate(self, document: object) -> JsonObject:
        self.parse(document)
        return cast(JsonObject, document)

    def json_schema(self) -> JsonObject:
        from gitopsctr.schemas import unit_resource_schema

        driver_name = getattr(self.api_kind.spec, "driver_name", None)
        if not isinstance(driver_name, str) or not driver_name:
            raise ContractError(f"Unit API {self.api_kind.gvk} has no driver name")
        return unit_resource_schema(driver_name, self.profile)


@dataclass(frozen=True)
class ReceiptResourceContract(TypedDocumentContract[Any]):
    """Dispatch a Receipt envelope through its subject Unit driver's contract."""

    api_kinds: Mapping[GVK, ApiKind[object]]

    def _catalog(self):
        from gitopsctr.driver import UnitDriver
        from gitopsctr.resources import ResourceCatalog

        drivers = {
            api_kind.spec.driver_name: api_kind.spec
            for api_kind in self.api_kinds.values()
            if isinstance(api_kind.spec, UnitDriver)
        }
        names_by_gvk = {
            str(api_kind.gvk): api_kind.spec.driver_name
            for api_kind in self.api_kinds.values()
            if isinstance(api_kind.spec, UnitDriver)
        }
        gvks_by_name = {
            api_kind.spec.driver_name: str(api_kind.gvk)
            for api_kind in self.api_kinds.values()
            if isinstance(api_kind.spec, UnitDriver)
        }
        return ResourceCatalog(drivers, names_by_gvk, gvks_by_name)

    def parse(self, document: object) -> object:
        from gitopsctr.errors import OperationError

        try:
            value = require_json_value(document)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return self._catalog().parse_receipt(value)
        except (OperationError, TypeError, ValueError, KeyError) as exc:
            raise ContractError(str(exc)) from exc

    def dump(self, value: object) -> JsonObject:
        from gitopsctr.resources import ReceiptResource

        if not isinstance(value, ReceiptResource):
            raise ContractError("expected a typed Receipt resource")
        return self._catalog().serialize_receipt(value)

    def validate(self, document: object) -> JsonObject:
        self.parse(document)
        return cast(JsonObject, document)

    def json_schema(self) -> JsonObject:
        from gitopsctr.driver import UnitDriver
        from gitopsctr.schemas import receipt_resource_schema

        schemas = [
            receipt_resource_schema(api_kind.spec.driver_name)
            for api_kind in sorted(self.api_kinds.values(), key=lambda item: str(item.gvk))
            if isinstance(api_kind.spec, UnitDriver)
        ]
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "oneOf": cast(Any, schemas)}


@dataclass(frozen=True)
class ReceiptApiMembership:
    """Exact core Receipt membership with subject-driver-dispatched validation."""

    gvk: GVK
    interface: type[object]
    api_kinds: Mapping[GVK, ApiKind[object]]

    def matches(self, api_kind: ApiKind[object]) -> bool:
        return api_kind.gvk == self.gvk and isinstance(api_kind.spec, self.interface)

    def contract(self, api_kind: ApiKind[object], profile: str) -> TypedDocumentContract[Any] | None:
        return ReceiptResourceContract(self.api_kinds) if profile == "observed" else None


@dataclass(frozen=True)
class ArtifactApiMembership:
    """Membership and contract selection for installed Artifact APIs."""

    interface: type[object]

    def matches(self, api_kind: ApiKind[object]) -> bool:
        return isinstance(api_kind.spec, self.interface)

    def contract(self, api_kind: ApiKind[object], profile: str) -> TypedDocumentContract[Any] | None:
        value = getattr(api_kind.spec, "contract", None) if profile == "observed" else None
        return value if isinstance(value, TypedDocumentContract) else None


@dataclass(frozen=True)
class ResourcePlacement:
    """One persisted representation of a resource family."""

    plane: ResourcePlane
    scope: ResourceScope
    collection: str
    contract_profile: str
    default_for_inspection: bool = False


def _mapping_field(value: Mapping[str, JsonValue], field: str) -> Mapping[str, JsonValue]:
    candidate = value.get(field)
    return candidate if isinstance(candidate, dict) else {}


def _short_revision(value: object) -> str:
    return value[:12] if isinstance(value, str) and value else "-"


def _short_digest(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    return value if len(value) <= 19 else f"{value[:19]}..."


def _digest(value: object) -> str:
    return value if isinstance(value, str) and value else "-"


def _deletion_state(metadata: Mapping[str, JsonValue]) -> str:
    deletion = _mapping_field(metadata, "deletion")
    if not deletion:
        return "ACTIVE"
    generation = deletion.get("generation")
    return f"DELETING(generation={generation})" if generation is not None else "DELETING"


def _acquisition(specification: Mapping[str, JsonValue]) -> str:
    acquisition = _mapping_field(specification, "acquisition")
    modes = [
        {"fromInput": "input", "fromGit": "git", "fromPromotion": "promotion"}.get(key, key)
        for key in acquisition
        if key != "documentDigest"
    ]
    mode = "+".join(modes) or "-"
    document_digest = acquisition.get("documentDigest")
    if document_digest is None:
        return mode
    return f"{mode}(document={_short_digest(document_digest)})"


def _projection_topology(projection: Mapping[str, JsonValue]) -> str:
    units = projection.get("units")
    if not isinstance(units, dict):
        return "-"
    topology: list[str] = []
    for logical_name, value in sorted(units.items()):
        unit = value if isinstance(value, dict) else {}
        dependencies = unit.get("dependsOn")
        dependency_names = (
            ",".join(str(item) for item in dependencies) if isinstance(dependencies, list) and dependencies else "-"
        )
        topology.append(f"{logical_name}<-{dependency_names}")
    return ";".join(topology) or "-"


def _projection_summary(projection: object, *, active: bool) -> str:
    value = projection if isinstance(projection, dict) else {}
    if not value:
        return "-"
    identity = value if active else _mapping_field(value, "identity")
    digest = identity.get("projectionDigest")
    context_digest = identity.get("projectionContextDigest")
    if active:
        prefix = f"projection={_short_digest(digest)} source={_short_digest(identity.get('sourceProjectionDigest'))}"
    else:
        prefix = f"projection={_short_digest(digest)}"
    units = value.get("units")
    rendered_units: list[str] = []
    if isinstance(units, dict):
        for logical_name, item in sorted(units.items()):
            unit = item if isinstance(item, dict) else {}
            if active:
                rendered_units.append(
                    f"{logical_name}:{unit.get('name', '-')}@{_short_digest(unit.get('desiredDigest'))}"
                )
            else:
                rendered_units.append(f"{logical_name}:{unit.get('kind', '-')}")
    return f"{prefix} context={_short_digest(context_digest)} units={','.join(rendered_units) or '-'}"


def _enum_value(value: object, description: str) -> str:
    candidate = getattr(value, "value", None)
    if not isinstance(candidate, str):
        raise ResourceModelError(f"inspection relationship has no {description}")
    return candidate


@dataclass(frozen=True)
class EnvironmentInspectionPresenter:
    """Present deployment-ref and reconciliation summaries for a namespace."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        summary = runtime.environment_summary(record.name)
        desired_ref = summary.desired_ref
        desired_revision = summary.desired_revision
        observed_ref = summary.observed_ref
        observed_revision = summary.observed_revision
        reconciliation = summary.reconciliation
        desired = f"{desired_ref}@{_short_revision(desired_revision)}" if desired_revision else f"{desired_ref}@missing"
        observed = (
            f"{observed_ref}@{_short_revision(observed_revision)}" if observed_revision else f"{observed_ref}@missing"
        )
        return record.name, desired, observed, reconciliation


@dataclass(frozen=True)
class UnitInspectionPresenter:
    """Present desired Unit state derived through its registered observation."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        if relationship is None:
            raise ResourceModelError(f"Unit {record.name!r} has no evaluated observation state")
        observation = _enum_value(getattr(relationship, "observation", None), "observation state")
        reconciliation = _enum_value(getattr(relationship, "reconciliation", None), "reconciliation state")
        reason = getattr(relationship, "reason", None)
        if not isinstance(reason, str):
            raise ResourceModelError(f"Unit {record.name!r} has no reconciliation reason")
        return record.name, record.gvk.kind, _short_revision(record.blob_id), observation, reconciliation, reason


@dataclass(frozen=True)
class StackInspectionPresenter:
    """Present desired Stack identity fences, projections, and child observation."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        metadata = _mapping_field(record.document, "metadata")
        specification = _mapping_field(record.document, "spec")
        structural_projection = specification.get("structuralProjection")
        if not isinstance(structural_projection, dict):
            structural_projection = {}
        summary = runtime.stack_inspection_summary(record)
        return (
            record.name,
            str(metadata.get("uid", "-")),
            summary.template_name or "-",
            summary.template_uid or "-",
            _digest(summary.template_digest),
            runtime.resource_partition(record) or "-",
            _projection_summary(structural_projection, active=False),
            _projection_summary(specification.get("activeProjection"), active=True),
            _projection_topology(structural_projection),
            ",".join(summary.child_observations) or "N/A",
            _deletion_state(metadata),
        )


@dataclass(frozen=True)
class StackTemplateInspectionPresenter:
    """Present desired StackTemplate provenance, partition, and references."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        metadata = _mapping_field(record.document, "metadata")
        specification = _mapping_field(record.document, "spec")
        parameters = specification.get("parameters")
        units = specification.get("unitTemplates")
        summary = runtime.stack_inspection_summary(record)
        return (
            record.name,
            str(metadata.get("uid", "-")),
            _digest(specification.get("contentDigest")),
            _acquisition(specification),
            f"context@{_short_revision(_mapping_field(specification, 'sourceContext').get('revision'))}"
            if _mapping_field(specification, "sourceContext")
            else "-",
            str(len(parameters)) if isinstance(parameters, list) else "0",
            str(len(units)) if isinstance(units, (list, dict)) else "0",
            runtime.resource_partition(record) or "-",
            ",".join(summary.references) or "-",
            _deletion_state(metadata),
        )


@dataclass(frozen=True)
class PromotionInspectionPresenter:
    """Present immutable Promotion lineage pins."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        specification = _mapping_field(record.document, "spec")
        source = _mapping_field(specification, "source")
        environment = source.get("environment")
        return (
            record.name,
            environment if isinstance(environment, str) else "-",
            _short_revision(source.get("desiredRevision")),
            _short_revision(source.get("observedRevision")),
            _short_revision(specification.get("specificationRevision")),
        )


@dataclass(frozen=True)
class ReceiptInspectionPresenter:
    """Present Receipt freshness without embedding its observed Artifacts."""

    def row(
        self,
        record: InspectionRecord,
        relationship: object | None,
        runtime: InspectionRuntime,
    ) -> tuple[str, ...]:
        if relationship is None:
            raise ResourceModelError(f"Receipt {record.name!r} has no evaluated observation state")
        specification = _mapping_field(record.document, "spec")
        subject = _mapping_field(specification, "subject")
        kind = subject.get("kind")
        artifact_count = getattr(relationship, "artifact_count", None)
        if not isinstance(artifact_count, int):
            raise ResourceModelError(f"Receipt {record.name!r} has no artifact descriptor count")
        return (
            record.name,
            kind if isinstance(kind, str) else "-",
            _enum_value(getattr(relationship, "observation", None), "observation state"),
            str(artifact_count),
        )


@dataclass(frozen=True)
class InspectionViewDefinition:
    """Presentation metadata kept separate from resource invariants."""

    default_plane: ResourcePlane
    columns: tuple[str, ...]
    presenter: InspectionPresenter
    observation: str | None = None
    artifact_description: str | None = None


@dataclass(frozen=True)
class ResourceFamilyDefinition:
    """A family of API kinds sharing selectors, placement, and behavior."""

    name: str
    singular: str
    plural: str
    placements: tuple[ResourcePlacement, ...]
    membership_rules: tuple[ApiKindMembership, ...]
    aliases: tuple[str, ...] = ()
    inspection: InspectionViewDefinition | None = None
    namespace_boundary: bool = False

    @property
    def selectors(self) -> tuple[str, ...]:
        return (self.singular, self.plural, *self.aliases)


@dataclass(frozen=True)
class CollectionReadContext:
    """Everything a registered collection provider needs to discover documents."""

    root: Path
    repository_root: Path
    project: Project
    environment: str | None
    family: ResourceFamilyDefinition
    placement: ResourcePlacement
    api_kinds: Mapping[GVK, ApiKind[object]]
    contracts: Mapping[GVK, TypedDocumentContract[Any]]
    blob_ids: Mapping[PurePosixPath, str] = field(default_factory=dict)
    names: frozenset[str] | None = None
    producer_names: frozenset[str] | None = None


@dataclass(frozen=True)
class DiscoveredResource:
    """Exact persisted document plus its parsed family representation."""

    path: PurePosixPath
    document: JsonObject
    gvk: GVK
    name: str
    parsed: object
    blob_id: str | None
    content_digest: str
    media_type: str | None
    identity_qualifier: tuple[str, ...] = ()


@runtime_checkable
class ResourceCollectionProvider(Protocol):
    def discover(self, context: CollectionReadContext) -> Iterable[DiscoveredResource]: ...


@dataclass(frozen=True)
class FilesystemCollectionProvider:
    """Built-in Git tree layout adapter; inventory supplies a materialized plane root."""

    layout: CollectionLayout
    media_typed: bool = False

    def _directories(self, context: CollectionReadContext) -> tuple[Path, ...]:
        if self.layout is CollectionLayout.PROJECT:
            return ()
        if self.layout is CollectionLayout.ENVIRONMENTS:
            base = context.root.joinpath(*context.project.environments_path.parts)
            if context.environment is not None:
                return (base / context.environment,)
            return (
                tuple(
                    sorted(
                        path
                        for path in base.iterdir()
                        if path.is_dir() and (context.names is None or path.name in context.names)
                    )
                )
                if base.is_dir()
                else ()
            )
        if self.layout is CollectionLayout.SOURCE_STACKTEMPLATES:
            return (context.root.joinpath(*context.project.stack_templates_path.parts),)
        source_collection = {
            CollectionLayout.SOURCE_UNITS: "units",
            CollectionLayout.SOURCE_STACKS: "stacks",
        }.get(self.layout)
        if source_collection is not None:
            if context.environment is None:
                raise ResourceModelError(f"collection {self.layout} requires an environment")
            environment_root = context.root.joinpath(*context.project.environments_path.parts, context.environment)
            return (environment_root / source_collection,)
        relative = {
            CollectionLayout.DESIRED_UNITS: "units",
            CollectionLayout.DESIRED_STACKS: "stacks",
            CollectionLayout.DESIRED_STACKTEMPLATES: "stack-templates",
            CollectionLayout.OBSERVED_RECEIPTS: "units",
            CollectionLayout.OBSERVED_ARTIFACTS: "artifacts",
        }.get(self.layout)
        return (context.root / relative,) if relative is not None else ()

    @staticmethod
    def _document_files(directory: Path, *, recursive: bool = False) -> tuple[Path, ...]:
        if not directory.is_dir():
            return ()
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        return tuple(
            sorted(path for path in iterator if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"})
        )

    def _paths(self, context: CollectionReadContext) -> tuple[Path, ...]:
        if self.layout is CollectionLayout.PROJECT:
            return tuple(context.root / name for name in PROJECT_CONFIG_NAMES if (context.root / name).is_file())
        if self.layout is CollectionLayout.ENVIRONMENTS:
            paths: list[Path] = []
            for directory in self._directories(context):
                candidates = self._stem_candidates(directory, "environment")
                if not candidates:
                    raise ResourceModelError(
                        f"authored environment directory {directory} has no environment.yaml, environment.yml, "
                        "or environment.json"
                    )
                paths.extend(candidates)
            return tuple(paths)
        if self.layout is CollectionLayout.DESIRED_PROMOTION:
            return self._stem_candidates(context.root, "promotion")
        recursive = self.layout is CollectionLayout.OBSERVED_ARTIFACTS
        paths = tuple(
            path
            for directory in self._directories(context)
            for path in self._document_files(directory, recursive=recursive)
        )
        if self.layout is CollectionLayout.OBSERVED_ARTIFACTS and context.producer_names is not None:
            paths = tuple(
                path
                for path in paths
                if len(path.relative_to(context.root).parts) == 3
                and path.relative_to(context.root).parts[1] in context.producer_names
            )
        elif (
            self.layout
            not in {
                CollectionLayout.PROJECT,
                CollectionLayout.ENVIRONMENTS,
                CollectionLayout.DESIRED_PROMOTION,
            }
            and context.names is not None
        ):
            paths = tuple(path for path in paths if path.stem in context.names)
        return paths

    @staticmethod
    def _stem_candidates(directory: Path, stem: str) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (directory / f"{stem}.yaml", directory / f"{stem}.yml", directory / f"{stem}.json")
            if path.is_file()
        )

    def discover(self, context: CollectionReadContext) -> Iterable[DiscoveredResource]:
        for path in self._paths(context):
            try:
                loaded = load_document(path)
            except Exception as exc:
                raise ResourceModelError(f"could not load {context.placement.plane} resource {path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ResourceModelError(f"resource {path} must be a document object")
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
            relative = PurePosixPath(path.relative_to(context.root).as_posix())
            identity_qualifier = self._validate_canonical_identity(context, path, relative, document, name)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ResourceModelError(f"could not read {context.placement.plane} resource {path}: {exc}") from exc
            media_type = None
            api_kind = context.api_kinds.get(gvk)
            if self.media_typed:
                if api_kind is None:
                    raise ResourceModelError(f"resource {path} has no authoritative API registration")
                suffix = "json" if path.suffix.lower() == ".json" else "yaml"
                base_media_type = getattr(api_kind.spec, "media_type", None)
                if not isinstance(base_media_type, str) or not base_media_type:
                    raise ResourceModelError(f"Artifact API {gvk} does not declare a media type")
                media_type = f"{base_media_type}+{suffix}"
            yield DiscoveredResource(
                relative,
                document,
                gvk,
                name,
                parsed,
                context.blob_ids.get(relative),
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                media_type,
                identity_qualifier,
            )

    def _validate_canonical_identity(
        self,
        context: CollectionReadContext,
        path: Path,
        relative: PurePosixPath,
        document: JsonObject,
        name: str,
    ) -> tuple[str, ...]:
        """Validate the layout-owned logical identity and return its qualifier."""

        if self.layout is CollectionLayout.ENVIRONMENTS:
            directory_name = path.parent.name
            if name != directory_name:
                raise ResourceModelError(
                    f"Environment metadata.name {name!r} in {path} must match directory {directory_name!r}"
                )
            return ()

        filename_owned = {
            CollectionLayout.SOURCE_UNITS,
            CollectionLayout.SOURCE_STACKS,
            CollectionLayout.SOURCE_STACKTEMPLATES,
            CollectionLayout.DESIRED_UNITS,
            CollectionLayout.DESIRED_STACKS,
            CollectionLayout.DESIRED_STACKTEMPLATES,
            CollectionLayout.OBSERVED_RECEIPTS,
        }
        if self.layout in filename_owned:
            if name != path.stem:
                raise ResourceModelError(
                    f"resource metadata.name {name!r} in {path} must match filename stem {path.stem!r}"
                )
            return ()

        if self.layout is CollectionLayout.OBSERVED_ARTIFACTS:
            if len(relative.parts) != 3 or relative.parts[0] != "artifacts":
                raise ResourceModelError(
                    f"Artifact {path} must use canonical path artifacts/PRODUCER/NAME.yaml|yml|json"
                )
            producer_name = relative.parts[1]
            if name != path.stem:
                raise ResourceModelError(
                    f"Artifact metadata.name {name!r} in {path} must match filename stem {path.stem!r}"
                )
            producer = document.get("producer")
            if not isinstance(producer, dict):
                raise ResourceModelError(f"Artifact {path} requires a producer identity")
            values = producer.get("apiVersion"), producer.get("kind"), producer.get("name")
            if not all(isinstance(value, str) and value for value in values):
                raise ResourceModelError(f"Artifact {path} has an invalid producer identity")
            try:
                GVK(cast(str, values[0]), cast(str, values[1]))
            except ValueError as exc:
                raise ResourceModelError(f"Artifact {path} has an invalid producer API kind: {exc}") from exc
            if values[2] != producer_name:
                raise ResourceModelError(
                    f"Artifact producer name {values[2]!r} in {path} must match directory {producer_name!r}"
                )
            return cast(tuple[str, str, str], values)

        # Project and Promotion have fixed collection-owned filenames; their
        # semantic identities are validated by their executable contracts.
        return ()


@dataclass(frozen=True)
class ResourceCollection:
    """Logical collection bound to an executable plane repository adapter."""

    name: str
    plane: ResourcePlane
    scope: ResourceScope
    contract_profiles: frozenset[str]
    provider: ResourceCollectionProvider


@dataclass(frozen=True)
class ResourceIdentity:
    api_version: str
    kind: str
    name: str

    @property
    def gvk(self) -> GVK:
        return GVK(self.api_version, self.kind)


@dataclass(frozen=True)
class RelationshipResource:
    """Plane-independent facts supplied to executable relationship bindings."""

    identity: ResourceIdentity
    document: JsonObject
    parsed: object
    path: PurePosixPath
    blob_id: str | None = None
    content_digest: str | None = None
    media_type: str | None = None


@runtime_checkable
class ResourceGraphBinding(Protocol):
    """Executable identity fence for a desired resource relationship."""

    def validate(self, source: object, target: object) -> None: ...

    def documentation(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class StackTemplateSelectionBinding:
    """Fence a Stack's selected StackTemplate by name, UID, and content digest."""

    def validate(self, source: object, target: object) -> None:
        from gitopsctr.contracts import DesiredStackSpec, DesiredStackTemplateSpec
        from gitopsctr.resources import StackResource

        if not isinstance(source, StackResource) or not isinstance(target, StackResource):
            raise ResourceModelError("StackTemplate selection binding requires Stack resources")
        if target.gvk != GVK("gitopsctr.io/v1", "StackTemplate") or source.gvk != GVK("gitopsctr.io/v1", "Stack"):
            raise ResourceModelError("StackTemplate selection binding has the wrong resource kinds")
        if not isinstance(source.spec, DesiredStackSpec):
            raise ResourceModelError(f"Stack {source.name!r} has an invalid specification")
        reference = source.spec.templateRef
        if reference.name != target.name:
            raise ResourceModelError(f"Stack {source.name!r} references a different StackTemplate name")
        if target.metadata.uid != reference.uid:
            raise ResourceModelError(f"Stack {source.name!r} StackTemplate reference has a different UID")
        if not isinstance(target.spec, DesiredStackTemplateSpec):
            raise ResourceModelError(f"StackTemplate {target.name!r} is not a desired StackTemplate")
        actual_digest = target.spec.contentDigest
        if actual_digest != reference.contentDigest:
            raise ResourceModelError(f"Stack {source.name!r} StackTemplate reference has a different content digest")
        identity = source.spec.structuralProjection.identity
        if source.metadata.uid != identity.stackUid:
            raise ResourceModelError(f"Stack {source.name!r} projection is fenced to a different Stack UID")
        if target.metadata.uid != identity.templateUid:
            raise ResourceModelError(f"Stack {source.name!r} projection is fenced to a different StackTemplate UID")
        if actual_digest != identity.templateContentDigest:
            raise ResourceModelError(
                f"Stack {source.name!r} projection is fenced to a different StackTemplate content digest"
            )

    def documentation(self) -> tuple[str, ...]:
        return (
            "Stack.apiVersion",
            "Stack.kind",
            "Stack.metadata.name",
            "Stack.metadata.uid",
            "Stack.spec.templateRef.name",
            "Stack.spec.templateRef.uid",
            "Stack.spec.templateRef.contentDigest",
            "Stack.spec.structuralProjection.identity.stackUid",
            "Stack.spec.structuralProjection.identity.templateUid",
            "Stack.spec.structuralProjection.identity.templateContentDigest",
            "StackTemplate.apiVersion",
            "StackTemplate.kind",
            "StackTemplate.metadata.name",
            "StackTemplate.metadata.uid",
            "StackTemplate.spec.contentDigest",
        )


@dataclass(frozen=True)
class StackOwnedUnitBinding:
    """Validate that a desired Unit is owned by the exact desired Stack UID."""

    def validate(self, source: object, target: object) -> None:
        from gitopsctr.resources import StackResource, UnitResource

        if not isinstance(source, StackResource) or source.gvk != GVK("gitopsctr.io/v1", "Stack"):
            raise ResourceModelError("Stack ownership binding requires a Stack source")
        if not isinstance(target, UnitResource):
            raise ResourceModelError("Stack ownership binding requires a Unit target")
        references = target.metadata.ownerReferences
        if references is None or len(references) != 1:
            raise ResourceModelError(f"Unit {target.name!r} is not owned by a Stack")
        owner = references[0]
        if source.metadata.uid is None:
            raise ResourceModelError(f"Stack {source.name!r} has no UID for ownership binding")
        expected = (source.gvk.api_version, source.gvk.kind, source.name, source.metadata.uid)
        if (owner.apiVersion, owner.kind, owner.name, owner.uid) != expected:
            raise ResourceModelError(f"Unit {target.name!r} is owned by a different Stack UID")

    def documentation(self) -> tuple[str, ...]:
        return (
            "Stack.apiVersion",
            "Stack.kind",
            "Stack.metadata.name",
            "Stack.metadata.uid",
            "Unit.apiVersion",
            "Unit.kind",
            "Unit.metadata.name",
            "Unit.metadata.ownerReferences[0].apiVersion",
            "Unit.metadata.ownerReferences[0].kind",
            "Unit.metadata.ownerReferences[0].name",
            "Unit.metadata.ownerReferences[0].uid",
        )


@dataclass(frozen=True)
class ResourceGraphRelationship:
    """A registry-owned relationship between independently stored resources."""

    name: str
    source_family: str
    source_plane: ResourcePlane
    target_family: str
    target_plane: ResourcePlane
    source_gvk: GVK | None
    target_gvk: GVK | None
    binding: ResourceGraphBinding


@dataclass(frozen=True)
class JsonFieldPath:
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.parts or any(not part for part in self.parts):
            raise ResourceModelError("JSON field path must contain non-empty components")

    def get(self, document: JsonObject) -> JsonValue:
        value: JsonValue = document
        for part in self.parts:
            if not isinstance(value, dict) or part not in value:
                raise ResourceModelError(f"missing relationship field {self}")
            value = value[part]
        return value

    def get_optional(self, document: JsonObject) -> JsonValue | None:
        value: JsonValue = document
        for part in self.parts:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    def __str__(self) -> str:
        return "/" + "/".join(self.parts)


@runtime_checkable
class ObservationBinding(Protocol):
    def subject_identity(self, observer: RelationshipResource) -> ResourceIdentity: ...

    def evaluate(self, observer: RelationshipResource, subject: RelationshipResource) -> ObservationState: ...

    def documentation(self) -> ObservationBindingDocumentation: ...


@dataclass(frozen=True)
class ObservationBindingDocumentation:
    identity_fields: tuple[JsonFieldPath, ...]
    freshness_field: JsonFieldPath
    freshness_target: str


@dataclass(frozen=True)
class ReceiptObservationBinding:
    """Executable Receipt subject identity and desired-blob freshness invariant."""

    subject_api_version: JsonFieldPath
    subject_kind: JsonFieldPath
    subject_name: JsonFieldPath
    desired_blob: JsonFieldPath

    def subject_identity(self, observer: RelationshipResource) -> ResourceIdentity:
        values = tuple(
            field.get(observer.document) for field in (self.subject_api_version, self.subject_kind, self.subject_name)
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ResourceModelError(f"Receipt {observer.identity.name!r} has an invalid subject identity")
        identity = ResourceIdentity(cast(str, values[0]), cast(str, values[1]), cast(str, values[2]))
        if observer.identity.name != identity.name:
            raise ResourceModelError(
                f"Receipt {observer.identity.name!r} subject name must match its resource name {identity.name!r}"
            )
        return identity

    def evaluate(self, observer: RelationshipResource, subject: RelationshipResource) -> ObservationState:
        if self.subject_identity(observer) != subject.identity:
            raise ResourceModelError(f"Receipt {observer.identity.name!r} observes a different Unit")
        expected = self.desired_blob.get(observer.document)
        if not isinstance(expected, str) or not expected:
            raise ResourceModelError(f"Receipt {observer.identity.name!r} has an invalid desired Unit blob")
        if subject.blob_id is None:
            raise ResourceModelError(f"desired Unit {subject.identity.name!r} has no Git blob identity")
        return ObservationState.CURRENT if expected == subject.blob_id else ObservationState.STALE

    def documentation(self) -> ObservationBindingDocumentation:
        return ObservationBindingDocumentation(
            (self.subject_api_version, self.subject_kind, self.subject_name),
            self.desired_blob,
            "subject Git blob",
        )


@dataclass(frozen=True)
class ObservationDefinition:
    """An independently stored observer/subject relationship."""

    name: str
    observer_family: str
    observer_plane: ResourcePlane
    subject_family: str
    subject_plane: ResourcePlane
    cardinality: ObservationCardinality
    binding: ObservationBinding


@dataclass(frozen=True)
class ArtifactLink:
    name: str
    artifact: RelationshipResource


@runtime_checkable
class ArtifactDescriptionBinding(Protocol):
    def descriptor_count(self, describer: RelationshipResource) -> int: ...

    def resolve(
        self,
        describer: RelationshipResource,
        context: ArtifactResolutionContext,
    ) -> tuple[ArtifactLink, ...]: ...

    def documentation(self) -> ArtifactBindingDocumentation: ...


@dataclass(frozen=True)
class ArtifactBindingDocumentation:
    descriptor_field: JsonFieldPath
    producer_field: JsonFieldPath
    verified_bindings: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactResolutionContext:
    producer: RelationshipResource
    artifacts_by_path: Mapping[PurePosixPath, RelationshipResource]
    declared_artifacts: Mapping[str, GVK]
    producer_observation: ObservationState | None


@dataclass(frozen=True)
class ReceiptArtifactDescriptionBinding:
    """Executable Receipt descriptor, Artifact identity, and producer invariants."""

    descriptors: JsonFieldPath
    artifact_producer: JsonFieldPath

    def descriptor_count(self, describer: RelationshipResource) -> int:
        value = self.descriptors.get_optional(describer.document)
        if value is None:
            return 0
        if not isinstance(value, dict):
            raise ResourceModelError(f"Receipt {describer.identity.name!r} artifact descriptors must be an object")
        return len(value)

    @staticmethod
    def _identity(value: JsonValue, description: str) -> ResourceIdentity:
        if not isinstance(value, dict):
            raise ResourceModelError(f"{description} must be an object")
        fields = value.get("apiVersion"), value.get("kind"), value.get("name")
        if not all(isinstance(field, str) and field for field in fields):
            raise ResourceModelError(f"{description} has an invalid resource identity")
        return ResourceIdentity(cast(str, fields[0]), cast(str, fields[1]), cast(str, fields[2]))

    def resolve(
        self,
        describer: RelationshipResource,
        context: ArtifactResolutionContext,
    ) -> tuple[ArtifactLink, ...]:
        value = self.descriptors.get_optional(describer.document)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ResourceModelError(f"Receipt {describer.identity.name!r} artifact descriptors must be an object")
        expected_names = set(context.declared_artifacts)
        if set(value) != expected_names:
            raise ResourceModelError(
                f"Receipt {describer.identity.name!r} describes artifacts {sorted(value)}; "
                f"expected {sorted(expected_names)}"
            )
        links: list[ArtifactLink] = []
        for name, descriptor_value in sorted(value.items()):
            if not isinstance(descriptor_value, dict):
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} must be an object")
            api_version = descriptor_value.get("apiVersion")
            kind = descriptor_value.get("kind")
            path_value = descriptor_value.get("path")
            digest = descriptor_value.get("digest")
            media_type = descriptor_value.get("mediaType")
            if not all(isinstance(item, str) and item for item in (api_version, kind, path_value, digest, media_type)):
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has invalid bindings")
            path = PurePosixPath(cast(str, path_value))
            if path.is_absolute() or ".." in path.parts:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has an unsafe path")
            if cast(str, media_type).endswith("+json"):
                suffix = "json"
            elif cast(str, media_type).endswith("+yaml"):
                suffix = "yaml"
            else:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has an unsupported media type")
            expected_path = PurePosixPath("artifacts", context.producer.identity.name, f"{name}.{suffix}")
            if path != expected_path:
                raise ResourceModelError(
                    f"Receipt artifact descriptor {name!r} must use canonical path {expected_path}"
                )
            artifact = context.artifacts_by_path.get(path)
            if artifact is None:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} targets missing path {path}")
            declared_gvk = context.declared_artifacts[name]
            expected = ResourceIdentity(declared_gvk.api_version, declared_gvk.kind, name)
            if (api_version, kind) != (declared_gvk.api_version, declared_gvk.kind):
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has the wrong declared API kind")
            if artifact.identity != expected:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has the wrong artifact identity")
            if artifact.content_digest != digest:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has the wrong digest")
            if artifact.media_type != media_type:
                raise ResourceModelError(f"Receipt artifact descriptor {name!r} has the wrong media type")
            producer_identity = self._identity(
                self.artifact_producer.get(artifact.document), f"Artifact {name!r} producer"
            )
            if producer_identity != context.producer.identity:
                raise ResourceModelError(f"Artifact {name!r} has the wrong producer identity")
            if context.producer_observation is ObservationState.CURRENT:
                self._validate_producer_pin(name, artifact, context.producer)
            links.append(ArtifactLink(name, artifact))
        producer_root = PurePosixPath("artifacts", context.producer.identity.name)
        actual_paths = {path for path in context.artifacts_by_path if path.parent == producer_root}
        expected_paths = {link.artifact.path for link in links}
        if actual_paths != expected_paths:
            raise ResourceModelError(
                f"Artifact files for producer {context.producer.identity.name!r} do not match its declared outputs"
            )
        return tuple(links)

    def _validate_producer_pin(
        self,
        name: str,
        artifact: RelationshipResource,
        producer: RelationshipResource,
    ) -> None:
        """Bind an Artifact to the exact current desired Unit input."""

        from gitopsctr.resources import UnitResource

        if not isinstance(producer.parsed, UnitResource):
            # Kept defensive for custom callers. Inventory only requests this
            # check after authenticating a current desired Unit observation.
            return
        details = self.artifact_producer.get(artifact.document)
        if not isinstance(details, dict):
            raise ResourceModelError(f"Artifact {name!r} producer must be an object")
        specification = producer.document.get("spec")
        source = specification.get("source") if isinstance(specification, dict) else None
        if not isinstance(source, dict):
            raise ResourceModelError(f"Artifact {name!r} producer desired Unit has no persisted source pin")
        expected = {
            "driverVersion": source.get("driverVersion"),
            "sourceRevision": source.get("revision"),
            "inputHashVersion": 1,
            "inputHash": source.get("inputHash"),
        }
        if not isinstance(expected["driverVersion"], int):
            raise ResourceModelError(f"Artifact {name!r} producer desired Unit has no persisted driver version")
        if any(details.get(field) != value for field, value in expected.items()):
            raise ResourceModelError(f"Artifact {name!r} has a stale producer source pin")

    def documentation(self) -> ArtifactBindingDocumentation:
        return ArtifactBindingDocumentation(
            self.descriptors,
            self.artifact_producer,
            ("identity", "producer", "source pin", "digest", "media type"),
        )


@dataclass(frozen=True)
class ArtifactDescriptionDefinition:
    """Receipt descriptor relationship to immutable artifact resources."""

    name: str
    describer_family: str
    describer_plane: ResourcePlane
    artifact_family: str
    artifact_plane: ResourcePlane
    producer_family: str
    producer_plane: ResourcePlane
    binding: ArtifactDescriptionBinding


@dataclass(frozen=True)
class ResourceRegistry:
    """Validated API family, placement, collection, and relationship definitions."""

    api_kinds: Mapping[GVK, ApiKind[object]]
    collections: tuple[ResourceCollection, ...]
    families: tuple[ResourceFamilyDefinition, ...]
    observations: tuple[ObservationDefinition, ...] = ()
    artifact_descriptions: tuple[ArtifactDescriptionDefinition, ...] = ()
    graph_relationships: tuple[ResourceGraphRelationship, ...] = ()
    _collections_by_name: Mapping[str, ResourceCollection] = field(init=False, repr=False)
    _families_by_name: Mapping[str, ResourceFamilyDefinition] = field(init=False, repr=False)
    _families_by_selector: Mapping[str, ResourceFamilyDefinition] = field(init=False, repr=False)
    _family_by_gvk: Mapping[GVK, ResourceFamilyDefinition] = field(init=False, repr=False)
    _namespace_family: ResourceFamilyDefinition = field(init=False, repr=False)
    _contracts: Mapping[tuple[GVK, str], TypedDocumentContract[Any]] = field(init=False, repr=False)
    _artifact_outputs: Mapping[GVK, Mapping[str, GVK]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        collections = self._unique(self.collections, "collection")
        families = self._unique(self.families, "family")
        selectors: dict[str, ResourceFamilyDefinition] = {}
        for collection in self.collections:
            self._validate_collection(collection)
        for family in self.families:
            self._validate_name(family.name, "family")
            for selector in family.selectors:
                self._validate_name(selector, "resource selector")
                previous = selectors.get(selector)
                if previous is not None:
                    raise ResourceModelError(
                        f"duplicate resource selector {selector!r}: {previous.name!r} and {family.name!r}"
                    )
                selectors[selector] = family
            self._validate_family(family, collections)

        namespace_families = tuple(family for family in self.families if family.namespace_boundary)
        if len(namespace_families) != 1:
            raise ResourceModelError("resource registry must define exactly one environment namespace family")
        namespace_family = namespace_families[0]
        if any(placement.scope is not ResourceScope.PROJECT for placement in namespace_family.placements):
            raise ResourceModelError("environment namespace family must be project-scoped")

        family_by_gvk, contracts = self._resolve_api_membership(families)
        relationship_names: set[str] = set()
        for observation in self.observations:
            self._claim_relationship_name(observation.name, relationship_names)
            self._validate_observation(observation, families)
        for description in self.artifact_descriptions:
            self._claim_relationship_name(description.name, relationship_names)
            self._validate_artifact_description(description, families)
        for relationship in self.graph_relationships:
            self._claim_relationship_name(relationship.name, relationship_names)
            self._validate_graph_relationship(relationship, families)
        self._validate_inspection_relationships(families)
        artifact_outputs = self._validate_driver_artifact_outputs(family_by_gvk)

        object.__setattr__(self, "api_kinds", MappingProxyType(dict(self.api_kinds)))
        object.__setattr__(self, "_collections_by_name", MappingProxyType(collections))
        object.__setattr__(self, "_families_by_name", MappingProxyType(families))
        object.__setattr__(self, "_families_by_selector", MappingProxyType(selectors))
        object.__setattr__(self, "_family_by_gvk", MappingProxyType(family_by_gvk))
        object.__setattr__(self, "_namespace_family", namespace_family)
        object.__setattr__(self, "_contracts", MappingProxyType(contracts))
        object.__setattr__(self, "_artifact_outputs", MappingProxyType(artifact_outputs))

    @staticmethod
    def _validate_name(value: str, description: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise ResourceModelError(f"invalid {description}: {value!r}")

    @staticmethod
    def _unique[T](values: tuple[T, ...], description: str) -> dict[str, T]:
        result: dict[str, T] = {}
        for value in values:
            name = getattr(value, "name", None)
            if not isinstance(name, str):
                raise ResourceModelError(f"{description} has no string name")
            if name in result:
                raise ResourceModelError(f"duplicate {description}: {name!r}")
            result[name] = value
        return result

    @staticmethod
    def _claim_relationship_name(name: str, claimed: set[str]) -> None:
        ResourceRegistry._validate_name(name, "relationship")
        if name in claimed:
            raise ResourceModelError(f"duplicate relationship: {name!r}")
        claimed.add(name)

    @staticmethod
    def _validate_collection(collection: ResourceCollection) -> None:
        ResourceRegistry._validate_name(collection.name, "collection")
        if not collection.contract_profiles:
            raise ResourceModelError(f"resource collection {collection.name!r} has no contract profiles")
        if not isinstance(collection.provider, ResourceCollectionProvider):
            raise ResourceModelError(f"resource collection {collection.name!r} has no executable provider")

    def _validate_family(
        self,
        family: ResourceFamilyDefinition,
        collections: Mapping[str, ResourceCollection],
    ) -> None:
        if not family.placements:
            raise ResourceModelError(f"resource family {family.name!r} has no placements")
        if not family.membership_rules or any(
            not isinstance(rule, ApiKindMembership) for rule in family.membership_rules
        ):
            raise ResourceModelError(f"resource family {family.name!r} has no executable API membership rule")
        placement_keys: set[tuple[ResourcePlane, ResourceScope, str]] = set()
        defaults = 0
        for placement in family.placements:
            key = (placement.plane, placement.scope, placement.contract_profile)
            if key in placement_keys:
                raise ResourceModelError(f"resource family {family.name!r} has a duplicate placement {key!r}")
            placement_keys.add(key)
            collection = collections.get(placement.collection)
            if collection is None:
                raise ResourceModelError(
                    f"resource family {family.name!r} references unknown collection {placement.collection!r}"
                )
            if collection.plane != placement.plane or collection.scope != placement.scope:
                raise ResourceModelError(
                    f"resource family {family.name!r} placement is incompatible with collection {collection.name!r}"
                )
            if placement.contract_profile not in collection.contract_profiles:
                raise ResourceModelError(
                    f"resource family {family.name!r} profile {placement.contract_profile!r} is not supported by "
                    f"collection {collection.name!r}"
                )
            defaults += placement.default_for_inspection
        if family.inspection is not None:
            if defaults != 1:
                raise ResourceModelError(
                    f"inspectable resource family {family.name!r} must have exactly one default placement"
                )
            if not family.inspection.columns or len(set(family.inspection.columns)) != len(family.inspection.columns):
                raise ResourceModelError(f"resource family {family.name!r} has invalid inspection columns")
            if any(not column or column.upper() != column for column in family.inspection.columns):
                raise ResourceModelError(f"resource family {family.name!r} inspection columns must be headings")
            if not isinstance(family.inspection.presenter, InspectionPresenter):
                raise ResourceModelError(f"resource family {family.name!r} inspection has no executable presenter")
            default = next(placement for placement in family.placements if placement.default_for_inspection)
            if family.inspection.default_plane is not default.plane:
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection default plane does not match its default placement"
                )

    def _resolve_api_membership(
        self, families: Mapping[str, ResourceFamilyDefinition]
    ) -> tuple[dict[GVK, ResourceFamilyDefinition], dict[tuple[GVK, str], TypedDocumentContract[Any]]]:
        family_by_gvk: dict[GVK, ResourceFamilyDefinition] = {}
        contracts: dict[tuple[GVK, str], TypedDocumentContract[Any]] = {}
        members_by_family = {name: 0 for name in families}
        for gvk, api_kind in self.api_kinds.items():
            if api_kind.gvk != gvk:
                raise ResourceModelError(f"API kind mapping key {gvk} does not match registration {api_kind.gvk}")
            matches = [
                (family, rule)
                for family in families.values()
                for rule in family.membership_rules
                if rule.matches(api_kind)
            ]
            if len(matches) != 1:
                names = sorted({family.name for family, _ in matches})
                raise ResourceModelError(f"API kind {gvk} matches {len(matches)} family membership rules: {names}")
            family, rule = matches[0]
            family_by_gvk[gvk] = family
            members_by_family[family.name] += 1
            for profile in {placement.contract_profile for placement in family.placements}:
                contract = rule.contract(api_kind, profile)
                if contract is None:
                    raise ResourceModelError(f"API kind {gvk} has no executable {profile!r} contract")
                contracts[(gvk, profile)] = contract
        for family, count in members_by_family.items():
            if count == 0:
                raise ResourceModelError(f"resource family {family!r} has no installed API kinds")
        return family_by_gvk, contracts

    @staticmethod
    def _has_placement(family: ResourceFamilyDefinition, plane: ResourcePlane) -> bool:
        return any(placement.plane == plane for placement in family.placements)

    def _validate_observation(
        self, definition: ObservationDefinition, families: Mapping[str, ResourceFamilyDefinition]
    ) -> None:
        observer = families.get(definition.observer_family)
        subject = families.get(definition.subject_family)
        if observer is None or subject is None:
            raise ResourceModelError(f"observation {definition.name!r} references an unknown family")
        if not self._has_placement(observer, definition.observer_plane):
            raise ResourceModelError(f"observation {definition.name!r} observer plane is not placed")
        if not self._has_placement(subject, definition.subject_plane):
            raise ResourceModelError(f"observation {definition.name!r} subject plane is not placed")
        if definition.cardinality is not ObservationCardinality.ZERO_OR_ONE:
            raise ResourceModelError(f"observation {definition.name!r} has unsupported cardinality")
        if not isinstance(definition.binding, ObservationBinding):
            raise ResourceModelError(f"observation {definition.name!r} has no executable binding")

    def _validate_artifact_description(
        self, definition: ArtifactDescriptionDefinition, families: Mapping[str, ResourceFamilyDefinition]
    ) -> None:
        describer = families.get(definition.describer_family)
        artifact = families.get(definition.artifact_family)
        producer = families.get(definition.producer_family)
        if describer is None or artifact is None or producer is None:
            raise ResourceModelError(f"artifact description {definition.name!r} references an unknown family")
        if not self._has_placement(describer, definition.describer_plane):
            raise ResourceModelError(f"artifact description {definition.name!r} describer plane is not placed")
        if not self._has_placement(artifact, definition.artifact_plane):
            raise ResourceModelError(f"artifact description {definition.name!r} artifact plane is not placed")
        if not self._has_placement(producer, definition.producer_plane):
            raise ResourceModelError(f"artifact description {definition.name!r} producer plane is not placed")
        if not isinstance(definition.binding, ArtifactDescriptionBinding):
            raise ResourceModelError(f"artifact description {definition.name!r} has no executable binding")

    def _validate_graph_relationship(
        self, definition: ResourceGraphRelationship, families: Mapping[str, ResourceFamilyDefinition]
    ) -> None:
        source = families.get(definition.source_family)
        target = families.get(definition.target_family)
        if source is None or target is None:
            raise ResourceModelError(f"graph relationship {definition.name!r} references an unknown family")
        if not self._has_placement(source, definition.source_plane):
            raise ResourceModelError(f"graph relationship {definition.name!r} source plane is not placed")
        if not self._has_placement(target, definition.target_plane):
            raise ResourceModelError(f"graph relationship {definition.name!r} target plane is not placed")
        source_kind = self.api_kinds.get(definition.source_gvk) if definition.source_gvk is not None else None
        target_kind = self.api_kinds.get(definition.target_gvk) if definition.target_gvk is not None else None
        if definition.source_gvk is not None and source_kind is None:
            raise ResourceModelError(f"graph relationship {definition.name!r} references an unknown source GVK")
        if definition.target_gvk is not None and target_kind is None:
            raise ResourceModelError(f"graph relationship {definition.name!r} references an unknown target GVK")
        family_by_gvk, _ = self._resolve_api_membership(families)
        if definition.source_gvk is not None and family_by_gvk.get(definition.source_gvk) is not source:
            raise ResourceModelError(f"graph relationship {definition.name!r} source GVK is outside its family")
        if definition.target_gvk is not None and family_by_gvk.get(definition.target_gvk) is not target:
            raise ResourceModelError(f"graph relationship {definition.name!r} target GVK is outside its family")
        if not isinstance(definition.binding, ResourceGraphBinding):
            raise ResourceModelError(f"graph relationship {definition.name!r} has no executable binding")

    def _validate_inspection_relationships(self, families: Mapping[str, ResourceFamilyDefinition]) -> None:
        observations = {definition.name: definition for definition in self.observations}
        descriptions = {definition.name: definition for definition in self.artifact_descriptions}
        for family in families.values():
            view = family.inspection
            if view is None:
                continue
            observation = observations.get(view.observation) if view.observation is not None else None
            description = descriptions.get(view.artifact_description) if view.artifact_description is not None else None
            if view.observation is not None and observation is None:
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection references unknown observation {view.observation!r}"
                )
            if view.artifact_description is not None and description is None:
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection references unknown artifact description "
                    f"{view.artifact_description!r}"
                )
            if observation is not None and (family.name, view.default_plane) not in {
                (observation.observer_family, observation.observer_plane),
                (observation.subject_family, observation.subject_plane),
            }:
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection observation {observation.name!r} "
                    "does not include its default representation"
                )
            if description is not None and (family.name, view.default_plane) not in {
                (description.describer_family, description.describer_plane),
                (description.artifact_family, description.artifact_plane),
                (description.producer_family, description.producer_plane),
            }:
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection artifact description {description.name!r} "
                    "does not include its default representation"
                )
            if (
                observation is not None
                and description is not None
                and (
                    observation.observer_family,
                    observation.observer_plane,
                    observation.subject_family,
                    observation.subject_plane,
                )
                != (
                    description.describer_family,
                    description.describer_plane,
                    description.producer_family,
                    description.producer_plane,
                )
            ):
                raise ResourceModelError(
                    f"resource family {family.name!r} inspection relationships do not share observer and subject "
                    "topology"
                )

    def _validate_driver_artifact_outputs(
        self, family_by_gvk: Mapping[GVK, ResourceFamilyDefinition]
    ) -> dict[GVK, Mapping[str, GVK]]:
        result: dict[GVK, Mapping[str, GVK]] = {}
        for api_kind in self.api_kinds.values():
            if family_by_gvk[api_kind.gvk].name != "unit":
                continue
            outputs = getattr(api_kind.spec, "artifact_outputs", None)
            if outputs is None:
                outputs = {}
            if not isinstance(outputs, Mapping):
                raise ResourceModelError(f"unit API kind {api_kind.gvk} has invalid artifact outputs")
            for name, artifact_kind in outputs.items():
                if not isinstance(name, str) or not name:
                    raise ResourceModelError(f"unit API kind {api_kind.gvk} has an invalid artifact output name")
                if not isinstance(artifact_kind, ApiKind) or self.api_kinds.get(artifact_kind.gvk) is not artifact_kind:
                    raise ResourceModelError(
                        f"unit API kind {api_kind.gvk} artifact {name!r} is not an authoritative registration"
                    )
                if family_by_gvk[artifact_kind.gvk].name != "artifact":
                    raise ResourceModelError(
                        f"unit API kind {api_kind.gvk} artifact {name!r} is not in the artifact family"
                    )
            result[api_kind.gvk] = MappingProxyType({name: kind.gvk for name, kind in outputs.items()})
        return result

    def family(self, name_or_selector: str) -> ResourceFamilyDefinition:
        try:
            return self._families_by_name[name_or_selector]
        except KeyError:
            try:
                return self._families_by_selector[name_or_selector]
            except KeyError as exc:
                raise KeyError(f"unknown resource family or selector: {name_or_selector!r}") from exc

    def family_for_api_kind(self, gvk: GVK) -> ResourceFamilyDefinition:
        try:
            return self._family_by_gvk[gvk]
        except KeyError as exc:
            raise KeyError(f"unregistered API kind: {gvk}") from exc

    @property
    def namespace_family(self) -> ResourceFamilyDefinition:
        return self._namespace_family

    def contract(self, gvk: GVK, profile: str) -> TypedDocumentContract[Any]:
        try:
            return self._contracts[(gvk, profile)]
        except KeyError as exc:
            raise KeyError(f"API kind {gvk} has no {profile!r} representation") from exc

    def contracts_for(self, family: str, profile: str) -> Mapping[GVK, TypedDocumentContract[Any]]:
        definition = self.family(family)
        return MappingProxyType(
            {
                gvk: contract
                for (gvk, candidate_profile), contract in self._contracts.items()
                if candidate_profile == profile and self._family_by_gvk[gvk] is definition
            }
        )

    def artifact_outputs_for(self, producer: GVK) -> Mapping[str, GVK]:
        try:
            return self._artifact_outputs[producer]
        except KeyError as exc:
            raise KeyError(f"API kind {producer} is not an artifact-producing Unit API") from exc

    def collection(self, name: str) -> ResourceCollection:
        try:
            return self._collections_by_name[name]
        except KeyError as exc:
            raise KeyError(f"unknown resource collection: {name!r}") from exc

    def observation(self, name: str) -> ObservationDefinition:
        try:
            return next(definition for definition in self.observations if definition.name == name)
        except StopIteration as exc:
            raise KeyError(f"unknown observation relationship: {name!r}") from exc

    def artifact_description(self, name: str) -> ArtifactDescriptionDefinition:
        try:
            return next(definition for definition in self.artifact_descriptions if definition.name == name)
        except StopIteration as exc:
            raise KeyError(f"unknown artifact-description relationship: {name!r}") from exc

    def graph_relationship(self, name: str) -> ResourceGraphRelationship:
        try:
            return next(definition for definition in self.graph_relationships if definition.name == name)
        except StopIteration as exc:
            raise KeyError(f"unknown graph relationship: {name!r}") from exc

    def api_kinds_for_family(self, family: str) -> tuple[ApiKind[object], ...]:
        definition = self.family(family)
        return tuple(
            sorted(
                (kind for kind in self.api_kinds.values() if self._family_by_gvk[kind.gvk] is definition),
                key=lambda item: str(item.gvk),
            )
        )


def _placement(
    plane: ResourcePlane,
    scope: ResourceScope,
    collection: str,
    profile: str,
    *,
    default: bool = False,
) -> ResourcePlacement:
    return ResourcePlacement(plane, scope, collection, profile, default)


def build_resource_registry(api_kinds: Mapping[GVK, ApiKind[object]]) -> ResourceRegistry:
    """Build the built-in model around all installed core and plugin API kinds."""

    from gitopsctr.artifacts import ArtifactApi
    from gitopsctr.core_api import CORE_API_VERSION, CoreResourceApi
    from gitopsctr.driver import UnitDriver

    source = ResourcePlane.SOURCE
    desired = ResourcePlane.DESIRED
    observed = ResourcePlane.OBSERVED
    project = ResourceScope.PROJECT
    environment = ResourceScope.ENVIRONMENT

    def collection(
        name: str,
        plane: ResourcePlane,
        scope: ResourceScope,
        profiles: tuple[str, ...],
        layout: CollectionLayout,
    ) -> ResourceCollection:
        return ResourceCollection(
            name,
            plane,
            scope,
            frozenset(profiles),
            FilesystemCollectionProvider(layout, media_typed=layout is CollectionLayout.OBSERVED_ARTIFACTS),
        )

    collections = (
        collection("source-project", source, project, ("authored",), CollectionLayout.PROJECT),
        collection("source-environments", source, project, ("authored",), CollectionLayout.ENVIRONMENTS),
        collection("source-units", source, environment, ("authored",), CollectionLayout.SOURCE_UNITS),
        collection("source-stacks", source, environment, ("authored",), CollectionLayout.SOURCE_STACKS),
        collection("source-stacktemplates", source, project, ("authored",), CollectionLayout.SOURCE_STACKTEMPLATES),
        collection("desired-units", desired, environment, ("desired",), CollectionLayout.DESIRED_UNITS),
        collection("desired-stacks", desired, environment, ("desired",), CollectionLayout.DESIRED_STACKS),
        collection(
            "desired-stacktemplates", desired, environment, ("desired",), CollectionLayout.DESIRED_STACKTEMPLATES
        ),
        collection("desired-promotions", desired, environment, ("desired",), CollectionLayout.DESIRED_PROMOTION),
        collection("observed-receipts", observed, environment, ("observed",), CollectionLayout.OBSERVED_RECEIPTS),
        collection("observed-artifacts", observed, environment, ("observed",), CollectionLayout.OBSERVED_ARTIFACTS),
    )

    def core(kind: str) -> tuple[ApiKindMembership, ...]:
        return (ProfiledApiMembership(frozenset((GVK(CORE_API_VERSION, kind),)), CoreResourceApi),)

    families = (
        ResourceFamilyDefinition(
            "project",
            "project",
            "projects",
            (_placement(source, project, "source-project", "authored"),),
            core("Project"),
        ),
        ResourceFamilyDefinition(
            "environment",
            "environment",
            "environments",
            (_placement(source, project, "source-environments", "authored", default=True),),
            core("Environment"),
            inspection=InspectionViewDefinition(
                source,
                ("NAME", "DESIRED", "OBSERVED", "RECONCILIATION"),
                EnvironmentInspectionPresenter(),
            ),
            namespace_boundary=True,
        ),
        ResourceFamilyDefinition(
            "unit",
            "unit",
            "units",
            (
                _placement(source, environment, "source-units", "authored"),
                _placement(desired, environment, "desired-units", "desired", default=True),
            ),
            (UnitApiMembership(UnitDriver),),
            inspection=InspectionViewDefinition(
                desired,
                ("NAME", "KIND", "DESIRED", "OBSERVATION", "RECONCILIATION", "REASON"),
                UnitInspectionPresenter(),
                observation="receipt-observes-unit",
                artifact_description="receipt-describes-artifacts",
            ),
        ),
        ResourceFamilyDefinition(
            "stack",
            "stack",
            "stacks",
            (
                _placement(source, environment, "source-stacks", "authored"),
                _placement(desired, environment, "desired-stacks", "desired", default=True),
            ),
            core("Stack"),
            inspection=InspectionViewDefinition(
                desired,
                (
                    "NAME",
                    "UID",
                    "TEMPLATE",
                    "TEMPLATE-UID",
                    "TEMPLATE-DIGEST",
                    "PARTITION",
                    "STRUCTURAL",
                    "ACTIVE",
                    "TOPOLOGY",
                    "OBSERVATION",
                    "STATE",
                ),
                StackInspectionPresenter(),
            ),
        ),
        ResourceFamilyDefinition(
            "stacktemplate",
            "stacktemplate",
            "stacktemplates",
            (
                _placement(source, project, "source-stacktemplates", "authored"),
                _placement(desired, environment, "desired-stacktemplates", "desired", default=True),
            ),
            core("StackTemplate"),
            inspection=InspectionViewDefinition(
                desired,
                (
                    "NAME",
                    "UID",
                    "CONTENT-DIGEST",
                    "ACQUISITION",
                    "SOURCE",
                    "PARAMETERS",
                    "UNITS",
                    "PARTITION",
                    "REFERENCES",
                    "STATE",
                ),
                StackTemplateInspectionPresenter(),
            ),
        ),
        ResourceFamilyDefinition(
            "promotion",
            "promotion",
            "promotions",
            (_placement(desired, environment, "desired-promotions", "desired", default=True),),
            core("Promotion"),
            inspection=InspectionViewDefinition(
                desired,
                ("NAME", "SOURCE", "DESIRED-REVISION", "OBSERVED-REVISION", "SPECIFICATION-REVISION"),
                PromotionInspectionPresenter(),
            ),
        ),
        ResourceFamilyDefinition(
            "receipt",
            "receipt",
            "receipts",
            (_placement(observed, environment, "observed-receipts", "observed", default=True),),
            (ReceiptApiMembership(GVK(CORE_API_VERSION, "Receipt"), CoreResourceApi, api_kinds),),
            inspection=InspectionViewDefinition(
                observed,
                ("NAME", "KIND", "OBSERVATION", "ARTIFACTS"),
                ReceiptInspectionPresenter(),
                observation="receipt-observes-unit",
                artifact_description="receipt-describes-artifacts",
            ),
        ),
        ResourceFamilyDefinition(
            "artifact",
            "artifact",
            "artifacts",
            (_placement(observed, environment, "observed-artifacts", "observed"),),
            (ArtifactApiMembership(ArtifactApi),),
        ),
    )
    observations = (
        ObservationDefinition(
            "receipt-observes-unit",
            observer_family="receipt",
            observer_plane=observed,
            subject_family="unit",
            subject_plane=desired,
            cardinality=ObservationCardinality.ZERO_OR_ONE,
            binding=ReceiptObservationBinding(
                JsonFieldPath(("spec", "subject", "apiVersion")),
                JsonFieldPath(("spec", "subject", "kind")),
                JsonFieldPath(("spec", "subject", "name")),
                JsonFieldPath(("spec", "desired", "unitBlob")),
            ),
        ),
    )
    artifact_descriptions = (
        ArtifactDescriptionDefinition(
            "receipt-describes-artifacts",
            describer_family="receipt",
            describer_plane=observed,
            artifact_family="artifact",
            artifact_plane=observed,
            producer_family="unit",
            producer_plane=desired,
            binding=ReceiptArtifactDescriptionBinding(
                JsonFieldPath(("status", "artifacts")), JsonFieldPath(("producer",))
            ),
        ),
    )
    graph_relationships = (
        ResourceGraphRelationship(
            "stack-selects-stacktemplate",
            source_family="stack",
            source_plane=desired,
            target_family="stacktemplate",
            target_plane=desired,
            source_gvk=GVK(CORE_API_VERSION, "Stack"),
            target_gvk=GVK(CORE_API_VERSION, "StackTemplate"),
            binding=StackTemplateSelectionBinding(),
        ),
        ResourceGraphRelationship(
            "stack-owns-unit",
            source_family="stack",
            source_plane=desired,
            target_family="unit",
            target_plane=desired,
            source_gvk=GVK(CORE_API_VERSION, "Stack"),
            target_gvk=None,
            binding=StackOwnedUnitBinding(),
        ),
    )
    return ResourceRegistry(api_kinds, collections, families, observations, artifact_descriptions, graph_relationships)
