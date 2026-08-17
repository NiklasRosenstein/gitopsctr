"""Resource envelope parsing, serialization, and typed document loading."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import uuid4

from gitopsctr.api import GVK
from gitopsctr.artifacts import ArtifactApi
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    PARTITION_LABEL,
    ArtifactDescriptor,
    AuthoredResourceMetadata,
    DeletionMetadata,
    DesiredOwnerReference,
    DesiredResourceMetadata,
    DesiredStackDocument,
    DesiredStackSpec,
    DesiredStackTemplateDocument,
    DesiredStackTemplateSpec,
    QualifiedResourceName,
    ReceiptDesired,
    ResolvedInputs,
    StackDocument,
    StackSpec,
    StackTemplateDocument,
    StackTemplateDocumentSpec,
    StackTemplateResource,
    StrictModel,
    stack_generated_unit_name,
)
from gitopsctr.document import ContractError, JsonObject, JsonObjectValue, TypedDocumentContract
from gitopsctr.driver import InstalledUnitDriver
from gitopsctr.errors import OperationError
from gitopsctr.formats import (
    PROJECT_CONFIG_NAMES,
    DocumentFormatError,
    document_candidates,
    load_document,
    load_project_config,
    write_document,
)
from gitopsctr.schemas import receipt_resource_schema, resource_schema_url
from gitopsctr.templates import TemplateValue, dump_template_value

CORE_API_VERSION = "gitopsctr.io/v1"
UNIT_API_VERSION = "unit.gitopsctr.io/v1"


@dataclass(frozen=True, kw_only=True)
class ResourceMetadata(StrictModel):
    name: str
    uid: str | None = None
    labels: dict[str, str] | None = None
    ownerReferences: list[DesiredOwnerReference] | None = None
    deletion: DeletionMetadata | None = None

    def validate_desired(self) -> None:
        if self.uid is None:
            raise ValueError("desired metadata requires uid")
        DesiredResourceMetadata(
            name=self.name,
            uid=self.uid,
            labels=self.labels,
            ownerReferences=self.ownerReferences,
            deletion=self.deletion,
        )

    def as_desired(self) -> DesiredResourceMetadata:
        self.validate_desired()
        assert self.uid is not None
        return DesiredResourceMetadata(
            name=self.name,
            uid=self.uid,
            labels=self.labels,
            ownerReferences=self.ownerReferences,
            deletion=self.deletion,
        )

    @classmethod
    def new_root(cls, name: str, *, partition: str | None = None) -> ResourceMetadata:
        """Create a new desired root identity in an optional management partition."""

        metadata = cls(name=name, uid=uuid4().hex)
        return metadata.with_partition(partition)

    @classmethod
    def root_from_provenance(cls, name: str, provenance: str, *, partition: str | None = None) -> ResourceMetadata:
        """Create a deterministic desired root identity for one proposal."""

        digest = hashlib.sha256(f"gitopsctr/desired-uid/v1\0{provenance}".encode()).hexdigest()[:32]
        metadata = cls(name=name, uid=f"d1-{digest}")
        return metadata.with_partition(partition)

    @property
    def is_root(self) -> bool:
        return self.ownerReferences is None

    @property
    def partition(self) -> str | None:
        """Return this desired root's management partition, if any."""

        if not self.is_root:
            raise ValueError("owned desired resources do not have a management partition")
        return self.labels.get(PARTITION_LABEL) if self.labels is not None else None

    @property
    def is_unpartitioned_root(self) -> bool:
        return self.is_root and self.partition is None

    def with_partition(self, partition: str | None, *, preserve_existing: bool = False) -> ResourceMetadata:
        """Return a root stamped with a partition, optionally retaining an existing one."""

        if not self.is_root:
            raise ValueError("cannot stamp an owned desired resource with a management partition")
        existing = self.partition
        if preserve_existing and existing is not None:
            partition = existing
        labels = dict(self.labels or {})
        if partition is None:
            labels.pop(PARTITION_LABEL, None)
        else:
            labels[PARTITION_LABEL] = partition
        updated = replace(self, labels=labels or None)
        if updated.uid is not None:
            updated.validate_desired()
        return updated

    def document(self, *, profile: Literal["authored", "desired"]) -> JsonObject:
        if profile == "authored":
            if (
                self.uid is not None
                or self.labels is not None
                or self.ownerReferences is not None
                or self.deletion is not None
            ):
                raise ValueError("authored metadata may contain only name")
            return {"name": self.name}
        return {key: value for key, value in self.as_desired().to_dict().items() if value is not None}


@dataclass(frozen=True)
class UnitResource[ModelT: StrictModel]:
    """A typed unit specification associated with its authoritative GVK registration."""

    gvk: GVK
    metadata: ResourceMetadata
    driver: InstalledUnitDriver
    spec: ModelT

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def driver_name(self) -> str:
        return self.driver.driver_name

    def with_metadata(self, metadata: ResourceMetadata) -> UnitResource[ModelT]:
        return UnitResource(self.gvk, metadata, self.driver, self.spec)

    def with_spec[NextT: StrictModel](self, spec: NextT) -> UnitResource[NextT]:
        return UnitResource(self.gvk, self.metadata, self.driver, spec)


@dataclass(frozen=True)
class StackResource:
    """A typed Stack or StackTemplate resource in the desired graph."""

    gvk: GVK
    metadata: ResourceMetadata
    spec: StackSpec | DesiredStackSpec | StackTemplateDocumentSpec | DesiredStackTemplateSpec

    @property
    def name(self) -> str:
        return self.metadata.name

    def with_metadata(self, metadata: ResourceMetadata) -> StackResource:
        return StackResource(self.gvk, metadata, self.spec)


DesiredGraphResource = UnitResource[Any] | StackResource


def _stack_template_name(spec: StackSpec | DesiredStackSpec) -> str:
    """Return the logical template name from authored or desired Stack syntax."""

    return spec.template if isinstance(spec, StackSpec) else spec.templateRef.name


def _validate_stack_template_reference(stack: StackResource, template: StackResource) -> None:
    """Validate the optional desired StackTemplate identity fence."""

    from gitopsctr.registry import RESOURCE_REGISTRY

    try:
        RESOURCE_REGISTRY.graph_relationship("stack-selects-stacktemplate").binding.validate(stack, template)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


@dataclass(frozen=True)
class _ProjectedStackUnit:
    """The graph-relevant portion of one Stack projection entry."""

    logical_name: str
    api_version: str
    kind: str
    name: str
    spec: object
    dependencies: tuple[str, ...]


def _resolved_stack_projection(stack: StackResource) -> tuple[_ProjectedStackUnit, ...]:
    """Parse the required structural Unit topology recorded by a desired Stack."""

    if not isinstance(stack.spec, DesiredStackSpec):
        raise ValueError(f"Stack {stack.name!r} must use a desired Stack specification")
    projection = stack.spec.structuralProjection
    units = projection.units
    projected: list[_ProjectedStackUnit] = []
    for logical_name, value in units.items():
        api_version = value.apiVersion
        kind = value.kind
        dependencies = value.dependsOn
        if logical_name in dependencies:
            raise ValueError(f"Stack {stack.name!r} structuralProjection Unit {logical_name!r} cannot depend on itself")
        projected.append(
            _ProjectedStackUnit(
                logical_name=logical_name,
                api_version=api_version,
                kind=kind,
                name=logical_name,
                spec=dump_template_value(cast(TemplateValue, value.spec)),
                dependencies=tuple(dependencies),
            )
        )
    return tuple(projected)


def _resource_template_projection(
    stack: StackResource,
    template: StackResource,
) -> tuple[_ProjectedStackUnit, ...]:
    """Expand a sibling StackTemplate and validate any persisted projection against it."""

    if not isinstance(stack.spec, DesiredStackSpec) or not isinstance(template.spec, DesiredStackTemplateSpec):
        raise ValueError(f"Stack {stack.name!r} and StackTemplate {template.name!r} must both be desired resources")
    expanded = template.spec.expand(stack.spec.parameters)
    projected = _resolved_stack_projection(stack)
    selected_names = {resource.logical_name for resource in projected}
    expected_names = set(stack.spec.units or (resource.name for resource in expanded))
    if selected_names != expected_names:
        raise ValueError(f"Stack {stack.name!r} structuralProjection does not match selected Unit templates")
    expanded = tuple(resource for resource in expanded if resource.name in selected_names)
    template_spec = template.spec
    assert isinstance(template_spec, DesiredStackTemplateSpec)
    recorded_by_name = {resource.logical_name: resource for resource in projected}

    def normalized_spec(resource: StackTemplateResource) -> object:
        raw = dump_template_value(cast(TemplateValue, resource.spec))
        if not isinstance(raw, dict):
            return raw
        source = raw.get("source")
        recorded_source = recorded_by_name[resource.name].spec
        recorded_source = recorded_source.get("source") if isinstance(recorded_source, dict) else None
        if (
            isinstance(source, dict)
            and isinstance(source.get("path"), str)
            and source.get("revision") is None
            and isinstance(recorded_source, dict)
            and recorded_source.get("revision") is not None
            and template_spec.sourceContext is not None
        ):
            source = dict(source)
            source["revision"] = template_spec.sourceContext.revision
            raw = dict(raw)
            raw["source"] = source
        return raw

    expected = tuple(
        _ProjectedStackUnit(
            logical_name=resource.name,
            api_version=resource.apiVersion,
            kind=resource.kind,
            name=resource.name,
            spec=normalized_spec(resource),
            dependencies=tuple(resource.dependsOn),
        )
        for resource in expanded
    )
    if {resource.logical_name: resource for resource in projected} != {
        resource.logical_name: resource for resource in expected
    }:
        raise ValueError(f"Stack {stack.name!r} structuralProjection does not match StackTemplate expansion")
    return expected


def _validate_stack_projection(
    stack: StackResource,
    projected: tuple[_ProjectedStackUnit, ...],
    identities: Mapping[tuple[str, str, str], DesiredGraphResource],
) -> None:
    """Validate a projected topology against the concrete desired Unit graph."""

    by_name = {resource.name: resource for resource in projected}
    if len(by_name) != len(projected):
        raise ValueError(f"Stack {stack.name!r} structuralProjection has duplicate generated Unit names")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"Stack {stack.name!r} structuralProjection dependencies must be acyclic")
        if name in visited:
            return
        visiting.add(name)
        for dependency in by_name[name].dependencies:
            if dependency not in by_name:
                raise ValueError(f"Stack {stack.name!r} Unit {name!r} depends on missing generated Unit {dependency!r}")
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit(name)

    expected_owner = (stack.gvk.api_version, stack.gvk.kind, stack.name, stack.metadata.uid)
    actual_owned: dict[tuple[str, str, str], UnitResource[Any]] = {}
    for key, resource in identities.items():
        if not isinstance(resource, UnitResource) or resource.metadata.ownerReferences is None:
            continue
        owner = resource.metadata.ownerReferences[0]
        if (owner.apiVersion, owner.kind, owner.name, owner.uid) == expected_owner:
            actual_owned[key] = resource

    if not isinstance(stack.spec, DesiredStackSpec):
        raise ValueError(f"Stack {stack.name!r} must use a desired Stack specification")
    active = stack.spec.activeProjection
    active_units = active.units if active is not None else {}
    if active is not None:
        if (
            active.sourceProjectionDigest == stack.spec.structuralProjection.identity.projectionDigest
            and active.projectionContextDigest != stack.spec.structuralProjection.identity.projectionContextDigest
        ):
            raise ValueError(
                f"Stack {stack.name!r} active projection context does not match structural projection context"
            )
        concrete_names = [binding.name for binding in active_units.values()]
        if len(set(concrete_names)) != len(concrete_names):
            raise ValueError(f"Stack {stack.name!r} active projection has duplicate concrete Unit names")
        active_names = set(concrete_names)
        active_by_name = {binding.name: binding for binding in active_units.values()}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit_active(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Stack {stack.name!r} active projection dependencies must be acyclic")
            if name in visited:
                return
            visiting.add(name)
            for dependency in active_by_name[name].dependsOn:
                if dependency not in active_names:
                    raise ValueError(
                        f"Stack {stack.name!r} active projection Unit {name!r} depends on non-active Unit "
                        f"{dependency!r}"
                    )
                visit_active(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in active_names:
            visit_active(name)
    if active is None and any(resource.metadata.deletion is None for resource in actual_owned.values()):
        raise ValueError(f"Stack {stack.name!r} has concrete Units but no active projection binding")
    if (
        active is not None
        and active.sourceProjectionDigest != stack.spec.structuralProjection.identity.projectionDigest
    ):
        # A blocked structural transition keeps the previous active Unit set
        # atomically.  Those bindings can legitimately refer to a logical Unit
        # that is absent from the new structural projection until the complete
        # transition resolves.
        for binding in active_units.values():
            generated_resource = identities.get(
                (binding.apiVersion, binding.kind, stack_generated_unit_name(stack.name, binding.name))
            )
            if not isinstance(generated_resource, UnitResource):
                raise ValueError(f"Stack {stack.name!r} stale active projection is missing Unit {binding.name!r}")
            owner_references = generated_resource.metadata.ownerReferences
            owner = owner_references[0] if owner_references is not None else None
            if (
                (generated_resource.metadata.deletion is not None and stack.metadata.deletion is None)
                or generated_resource.metadata.uid != binding.uid
                or owner is None
                or (owner.apiVersion, owner.kind, owner.name, owner.uid)
                != (stack.gvk.api_version, stack.gvk.kind, stack.name, stack.metadata.uid)
                or desired_unit_binding_digest(generated_resource) != binding.desiredDigest
            ):
                raise ValueError(
                    f"Stack {stack.name!r} stale active projection does not authenticate Unit {binding.name!r}"
                )
            missing_dependencies = sorted(set(binding.dependsOn) - {item.name for item in active_units.values()})
            if missing_dependencies:
                raise ValueError(
                    f"Stack {stack.name!r} stale active projection Unit {binding.name!r} depends on "
                    f"non-active Unit(s): {', '.join(missing_dependencies)}"
                )
            active_keys = {
                (binding.apiVersion, binding.kind, stack_generated_unit_name(stack.name, binding.name))
                for binding in active_units.values()
            }
        unexpected = [
            resource
            for key, resource in actual_owned.items()
            if key not in active_keys and resource.metadata.deletion is None
        ]
        if unexpected:
            names = ", ".join(sorted(resource.name for resource in unexpected))
            raise ValueError(f"Stack {stack.name!r} stale active projection has unexpected generated Units: {names}")
        return
    unknown_active = sorted(set(active_units) - {resource.logical_name for resource in projected})
    if unknown_active:
        raise ValueError(
            f"Stack {stack.name!r} active projection has unknown Unit templates: {', '.join(unknown_active)}"
        )

    for generated in projected:
        binding = active_units.get(generated.logical_name)
        if binding is None:
            # A structurally valid Unit may be absent while its dynamic inputs
            # wait, or may still be represented by a deleting old child.
            stale = actual_owned.get(
                (generated.api_version, generated.kind, stack_generated_unit_name(stack.name, generated.name))
            )
            if stale is not None and stale.metadata.deletion is None:
                raise ValueError(
                    f"Stack {stack.name!r} generated Unit {generated.name!r} is concrete but absent from active projection"
                )
            continue
        generated_key = (
            generated.api_version,
            generated.kind,
            stack_generated_unit_name(stack.name, generated.name),
        )
        generated_resource = identities.get(generated_key)
        if generated_resource is None:
            same_name = [resource for resource in actual_owned.values() if resource.name == generated.name]
            if same_name:
                actual = same_name[0]
                raise ValueError(
                    f"Stack {stack.name!r} generated Unit {generated.name!r} has GVK {actual.gvk}, "
                    f"expected {generated.api_version}/{generated.kind}"
                )
            raise ValueError(f"Stack {stack.name!r} expansion is missing generated Unit {generated.name!r}")
        if not isinstance(generated_resource, UnitResource):
            raise ValueError(f"Stack {stack.name!r} expansion {generated.name!r} is not a Unit")
        generated_owner_references = generated_resource.metadata.ownerReferences
        actual_owner = (
            (
                generated_owner_references[0].apiVersion,
                generated_owner_references[0].kind,
                generated_owner_references[0].name,
                generated_owner_references[0].uid,
            )
            if generated_owner_references is not None
            else None
        )
        if actual_owner != expected_owner:
            raise ValueError(f"Stack {stack.name!r} generated Unit {generated.name!r} has an invalid owner reference")
        if (
            binding.apiVersion != generated_resource.gvk.api_version
            or binding.kind != generated_resource.gvk.kind
            or binding.name != generated_resource.name
            or binding.uid != generated_resource.metadata.uid
        ):
            raise ValueError(f"Stack {stack.name!r} active projection binding does not match Unit {generated.name!r}")
        actual_digest = desired_unit_binding_digest(generated_resource)
        if binding.desiredDigest != actual_digest:
            raise ValueError(
                f"Stack {stack.name!r} active projection binding does not authenticate Unit {generated.name!r}"
            )
        if (
            binding.sourceProjectionDigest == stack.spec.structuralProjection.identity.projectionDigest
            and binding.projectionContextDigest != stack.spec.structuralProjection.identity.projectionContextDigest
        ):
            raise ValueError(
                f"Stack {stack.name!r} active projection binding context does not match Unit {generated.name!r}"
            )
        from gitopsctr.registry import RESOURCE_REGISTRY

        try:
            RESOURCE_REGISTRY.graph_relationship("stack-owns-unit").binding.validate(stack, generated_resource)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        for dependency in generated.dependencies:
            dependency_resource = by_name[dependency]
            dependency_key = (
                dependency_resource.api_version,
                dependency_resource.kind,
                stack_generated_unit_name(stack.name, dependency_resource.name),
            )
            if dependency_key not in identities:
                raise ValueError(f"Stack {stack.name!r} dependency {dependency!r} is absent from this ref")
        expected_dependencies = tuple(generated.dependencies)
        if tuple(sorted(binding.dependsOn)) != tuple(sorted(expected_dependencies)):
            if (
                active is not None
                and active.sourceProjectionDigest == stack.spec.structuralProjection.identity.projectionDigest
            ):
                raise ValueError(
                    f"Stack {stack.name!r} active projection dependencies do not match structural topology for "
                    f"{generated.logical_name!r}"
                )

    expected_keys = {
        (resource.api_version, resource.kind, stack_generated_unit_name(stack.name, resource.name))
        for resource in projected
        if resource.logical_name in active_units
    }
    unexpected = [
        resource
        for key, resource in actual_owned.items()
        if key not in expected_keys and resource.metadata.deletion is None
    ]
    if unexpected:
        names = ", ".join(sorted(resource.name for resource in unexpected))
        raise ValueError(f"Stack {stack.name!r} has unexpected generated Units: {names}")


def desired_unit_binding_digest(unit: UnitResource[Any]) -> str:
    """Hash the effect-bearing desired Unit identity and typed specification."""

    payload = {
        "apiVersion": unit.gvk.api_version,
        "kind": unit.gvk.kind,
        "name": unit.name,
        "spec": unit.driver.desired_unit_contract.dump(unit.spec),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_desired_resource_graph(resources: Mapping[tuple[str, str, str], DesiredGraphResource]) -> None:
    """Validate UID fencing and acyclicity for resources from one desired ref.

    The mapping is deliberately scoped to one desired ref: the current document
    loader has no ref identifier in an individual resource envelope, so callers
    must not combine resources from different refs here.
    """

    identities: dict[tuple[str, str, str], DesiredGraphResource] = {}
    for key, unit in resources.items():
        owner_references = unit.metadata.ownerReferences
        qualified_name = (
            stack_generated_unit_name(owner_references[0].name, unit.name)
            if isinstance(unit, UnitResource) and owner_references is not None and owner_references[0].kind == "Stack"
            else unit.name
        )
        expected_key = (unit.gvk.api_version, unit.gvk.kind, qualified_name)
        if expected_key in identities:
            raise ValueError(f"duplicate desired resource identity: {expected_key!r}")
        if key != expected_key:
            raise ValueError(f"desired resource mapping key {key!r} does not match resource identity {expected_key!r}")
        identities[key] = unit
    edges: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for key, unit in identities.items():
        unit.metadata.validate_desired()
        owner_references = unit.metadata.ownerReferences
        if owner_references is None:
            continue
        owner = owner_references[0]
        owner_key = (owner.apiVersion, owner.kind, owner.name)
        owner_resource = identities.get(owner_key)
        if owner_resource is None:
            raise ValueError(f"desired owner reference for {key[2]!r} does not identify a resource in this ref")
        if owner_resource.metadata.uid != owner.uid:
            raise ValueError(f"desired owner reference for {key[2]!r} is fenced by a different UID")
        if owner_resource.metadata.deletion is not None and unit.metadata.deletion is None:
            raise ValueError(f"desired resource {key[2]!r} must be deleting with its owner")
        edges[key] = owner_key

    visiting: set[tuple[str, str, str]] = set()
    visited: set[tuple[str, str, str]] = set()

    def visit(key: tuple[str, str, str]) -> None:
        if key in visiting:
            raise ValueError("desired resource ownership must be acyclic")
        if key in visited:
            return
        visiting.add(key)
        owner = edges.get(key)
        if owner is not None:
            visit(owner)
        visiting.remove(key)
        visited.add(key)

    for key in identities:
        visit(key)

    # Resolve each Stack's authoritative Unit topology from the same-environment
    # desired StackTemplate and validate the identity fences before children.
    templates = {
        resource.name: resource
        for resource in identities.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate"
    }
    for template in templates.values():
        if not isinstance(template.spec, DesiredStackTemplateSpec):
            raise ValueError(f"StackTemplate {template.name!r} has an invalid desired specification")
        if not template.metadata.is_root:
            raise ValueError(f"StackTemplate {template.name!r} must be a root resource")
    for stack in (
        resource
        for resource in identities.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack"
    ):
        if not isinstance(stack.spec, DesiredStackSpec):
            raise ValueError(f"Stack {stack.name!r} has an invalid desired Stack spec")
        if not stack.metadata.is_root:
            raise ValueError(f"Stack {stack.name!r} must be a root resource")
        template_name = _stack_template_name(stack.spec)
        template = templates.get(template_name)
        if template is None:
            raise ValueError(f"Stack {stack.name!r} references missing StackTemplate {template_name!r} in this ref")
        _validate_stack_template_reference(stack, template)
        projected = (
            _resolved_stack_projection(stack)
            if stack.metadata.deletion is not None
            else _resource_template_projection(stack, template)
        )
        _validate_stack_projection(stack, projected, identities)


@dataclass(frozen=True, kw_only=True)
class ReceiptSubject(StrictModel):
    apiVersion: str
    kind: str
    name: str
    qualifiedName: QualifiedResourceName

    @property
    def gvk(self) -> GVK:
        return GVK(self.apiVersion, self.kind)


@dataclass(frozen=True, kw_only=True)
class ReceiptSpec(StrictModel):
    subject: ReceiptSubject
    desired: ReceiptDesired
    resolvedInputs: ResolvedInputs | None = None


@dataclass(frozen=True, kw_only=True)
class ReceiptStatus[ResultT: StrictModel]:
    controller: JsonObjectValue
    result: ResultT
    artifacts: dict[str, ArtifactDescriptor] | None = None


@dataclass(frozen=True)
class ReceiptResource[ResultT: StrictModel]:
    """A typed persisted receipt associated with its registered unit driver."""

    gvk: GVK
    metadata: ResourceMetadata
    driver: InstalledUnitDriver
    spec: ReceiptSpec
    status: ReceiptStatus[ResultT]

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def driver_name(self) -> str:
        return self.driver.driver_name


class ResourceCatalog:
    """The dynamic GVK boundary used by resource envelope I/O."""

    def __init__(
        self,
        drivers: dict[str, InstalledUnitDriver],
        driver_names_by_gvk: dict[str, str],
        driver_gvks: dict[str, str],
    ) -> None:
        self.drivers = drivers
        self.driver_names_by_gvk = driver_names_by_gvk
        self.driver_gvks = driver_gvks

    def load_document(self, path: Path) -> JsonObject:
        if not path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
            alternatives = document_candidates(path.parent, path.stem)
            if alternatives:
                path = alternatives[0]
        try:
            return load_document(path)
        except (OSError, DocumentFormatError) as exc:
            raise OperationError(f"could not read {path}: {exc}") from exc

    def normalize_environment(self, document: JsonObject, expected_name: str | None = None) -> JsonObject:
        if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Environment":
            raise OperationError("environment must use apiVersion gitopsctr.io/v1 and kind Environment")
        metadata, specification = document.get("metadata"), document.get("spec")
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("name"), str)
            or not isinstance(specification, dict)
        ):
            raise OperationError("environment envelope requires metadata.name and a spec mapping")
        name = metadata["name"]
        if expected_name is not None and name != expected_name:
            raise OperationError(f"environment metadata.name must be {expected_name!r}")
        return {"name": name, **specification}

    def normalize_promotion(self, document: JsonObject) -> JsonObject:
        if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Promotion":
            raise OperationError("promotion must use apiVersion gitopsctr.io/v1 and kind Promotion")
        metadata, specification = document.get("metadata"), document.get("spec")
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("name"), str)
            or not metadata["name"]
            or not isinstance(specification, dict)
        ):
            raise OperationError("promotion envelope requires metadata.name and a spec mapping")
        return dict(specification)

    def parse_unit[ModelT: StrictModel](
        self,
        document: JsonObject,
        *,
        profile: Literal["authored", "resolved", "desired"],
        expected_name: str | None = None,
    ) -> UnitResource[ModelT]:
        """Parse a persisted envelope directly into its registered typed specification."""

        api_version, kind = document.get("apiVersion"), document.get("kind")
        metadata, specification = document.get("metadata"), document.get("spec")
        if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(metadata, dict):
            raise OperationError("unit envelope requires apiVersion, kind, and metadata")
        if api_version != UNIT_API_VERSION:
            raise OperationError(f"unsupported unit API version: {api_version!r}")
        driver_name = self.driver_names_by_gvk.get(f"{api_version}/{kind}")
        driver = self.drivers.get(driver_name) if driver_name is not None else None
        if driver is None:
            raise OperationError(f"no installed unit driver handles {api_version}/{kind}")
        name = metadata.get("name")
        if not isinstance(specification, dict):
            raise OperationError(f"unit {name} requires a spec mapping")
        gvk = GVK(api_version, kind)
        if not isinstance(name, str) or not name or (expected_name is not None and name != expected_name):
            raise OperationError(f"unit metadata.name must be {expected_name or 'a non-empty name'!r}")
        if profile == "authored":
            if set(metadata) != {"name"}:
                raise OperationError("authored unit metadata may contain only name")
            metadata_model = ResourceMetadata(name=name)
        elif profile == "desired":
            try:
                nullable_fields = {"uid", "labels", "ownerReferences", "deletion"}
                if any(field in metadata and metadata[field] is None for field in nullable_fields):
                    raise ValueError("desired metadata fields cannot use null values")
                metadata_model = ResourceMetadata.from_dict(metadata)
                metadata_model.validate_desired()
            except (TypeError, ValueError, KeyError) as exc:
                raise OperationError(f"desired unit {name} has invalid metadata: {exc}") from exc
        else:
            metadata_model = ResourceMetadata(name=name)
        contract = {
            "authored": driver.unit_contract,
            "resolved": driver.resolved_unit_contract,
            "desired": driver.desired_unit_contract,
        }[profile]
        model = self.parse_contract(contract, specification, f"{profile} {driver.driver_name} unit {name}")
        return cast(UnitResource[ModelT], UnitResource(gvk, metadata_model, driver, model))

    @staticmethod
    def _stack_metadata(document: AuthoredResourceMetadata | DesiredResourceMetadata) -> ResourceMetadata:
        if isinstance(document, AuthoredResourceMetadata):
            return ResourceMetadata(name=document.name)
        return ResourceMetadata(
            name=document.name,
            uid=document.uid,
            labels=document.labels,
            ownerReferences=document.ownerReferences,
            deletion=document.deletion,
        )

    def parse_stack_template(
        self,
        document: JsonObject,
        *,
        profile: Literal["authored", "desired"],
        expected_name: str | None = None,
    ) -> StackResource:
        contract = cast(TypedDocumentContract[Any], CORE_CONTRACTS[f"stack-template-{profile}"])
        parsed = self.parse_contract(contract, document, f"{profile} StackTemplate")
        metadata = self._stack_metadata(parsed.metadata)  # type: ignore[union-attr]
        if expected_name is not None and metadata.name != expected_name:
            raise OperationError(f"StackTemplate metadata.name must be {expected_name!r}")
        return StackResource(GVK(CORE_API_VERSION, "StackTemplate"), metadata, parsed.spec)  # type: ignore[union-attr]

    def parse_stack(
        self,
        document: JsonObject,
        *,
        profile: Literal["authored", "desired"],
        expected_name: str | None = None,
    ) -> StackResource:
        contract = cast(TypedDocumentContract[Any], CORE_CONTRACTS[f"stack-{profile}"])
        parsed = self.parse_contract(contract, document, f"{profile} Stack")
        metadata = self._stack_metadata(parsed.metadata)  # type: ignore[union-attr]
        if expected_name is not None and metadata.name != expected_name:
            raise OperationError(f"Stack metadata.name must be {expected_name!r}")
        return StackResource(GVK(CORE_API_VERSION, "Stack"), metadata, parsed.spec)  # type: ignore[union-attr]

    def serialize_stack_resource(
        self,
        resource: StackResource,
        *,
        profile: Literal["authored", "desired"],
    ) -> JsonObject:
        if resource.gvk.kind == "StackTemplate":
            if not isinstance(resource.spec, StackTemplateDocumentSpec):
                raise OperationError("StackTemplate resource has an invalid spec")
            if profile == "authored":
                document = StackTemplateDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="StackTemplate",
                    metadata=AuthoredResourceMetadata(name=resource.name),
                    spec=resource.spec,
                )
            else:
                if not isinstance(resource.spec, DesiredStackTemplateSpec):
                    raise OperationError(
                        "cannot serialize an authored StackTemplate as desired without acquisition and content digest"
                    )
                desired_template_spec = resource.spec
                document = DesiredStackTemplateDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="StackTemplate",
                    metadata=resource.metadata.as_desired(),
                    spec=desired_template_spec,
                )
            contract = cast(TypedDocumentContract[Any], CORE_CONTRACTS[f"stack-template-{profile}"])
        elif resource.gvk.kind == "Stack":
            if not isinstance(resource.spec, (StackSpec, DesiredStackSpec)):
                raise OperationError("Stack resource has an invalid spec")
            if profile == "authored":
                if not isinstance(resource.spec, StackSpec):
                    raise OperationError("authored Stack has an invalid specification")
                document = StackDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="Stack",
                    metadata=AuthoredResourceMetadata(name=resource.name),
                    spec=resource.spec,
                )
            else:
                if not isinstance(resource.spec, DesiredStackSpec):
                    raise OperationError("cannot serialize an authored Stack as a desired Stack without projection")
                desired_spec = resource.spec
                document = DesiredStackDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="Stack",
                    metadata=resource.metadata.as_desired(),
                    spec=desired_spec,
                )
            contract = cast(TypedDocumentContract[Any], CORE_CONTRACTS[f"stack-{profile}"])
        else:
            raise OperationError(f"unsupported Stack resource kind: {resource.gvk.kind!r}")
        value = contract.dump(document)
        value["metadata"] = resource.metadata.document(profile=profile)
        value["$schema"] = resource_schema_url(CORE_API_VERSION, resource.gvk.kind, profile)
        return value

    def serialize_environment(self, environment: JsonObject) -> JsonObject:
        name = environment.get("name")
        if not isinstance(name, str):
            raise OperationError("environment is missing its name")
        specification = {key: value for key, value in environment.items() if key not in {"name", "$schema"}}
        return {
            "$schema": resource_schema_url(CORE_API_VERSION, "Environment"),
            "apiVersion": CORE_API_VERSION,
            "kind": "Environment",
            "metadata": {"name": name},
            "spec": specification,
        }

    def serialize_promotion(self, promotion: JsonObject) -> JsonObject:
        specification = {key: value for key, value in promotion.items() if key != "$schema"}
        source = specification.get("source", {})
        source_name = source.get("environment", "promotion") if isinstance(source, dict) else "promotion"
        return {
            "$schema": resource_schema_url(CORE_API_VERSION, "Promotion"),
            "apiVersion": CORE_API_VERSION,
            "kind": "Promotion",
            "metadata": {"name": str(source_name)},
            "spec": specification,
        }

    def serialize_unit(
        self, unit: UnitResource[Any], *, profile: Literal["authored", "desired"] = "desired"
    ) -> JsonObject:
        driver = unit.driver
        contract = driver.unit_contract if profile == "authored" else driver.desired_unit_contract
        try:
            specification = contract.dump(unit.spec)
        except (TypeError, ValueError) as exc:
            raise OperationError(f"invalid typed {profile} {unit.driver_name} unit {unit.name}: {exc}") from exc
        api_version, kind = unit.gvk.api_version, unit.gvk.kind
        try:
            metadata_document = unit.metadata.document(profile=profile)
        except ValueError as exc:
            raise OperationError(f"invalid {profile} metadata for {unit.name}: {exc}") from exc
        return {
            "$schema": resource_schema_url(api_version, kind, "authored" if profile == "authored" else "desired"),
            "apiVersion": api_version,
            "kind": kind,
            "metadata": metadata_document,
            "spec": specification,
        }

    @staticmethod
    def _compact(value: JsonObject) -> JsonObject:
        return {key: item for key, item in value.items() if item is not None}

    def parse_receipt(self, document: JsonObject, expected_unit: str | None = None) -> ReceiptResource[Any]:
        if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Receipt":
            raise OperationError("receipt must use apiVersion gitopsctr.io/v1 and kind Receipt")
        metadata, specification, status = document.get("metadata"), document.get("spec"), document.get("status")
        if not isinstance(metadata, dict) or not isinstance(specification, dict) or not isinstance(status, dict):
            raise OperationError("receipt envelope requires metadata, spec, and status mappings")
        name = metadata.get("name")
        if not isinstance(name, str) or not name or (expected_unit is not None and name != expected_unit):
            raise OperationError(f"receipt metadata.name must be {expected_unit or 'a unit name'}")
        subject_document = specification.get("subject")
        if not isinstance(subject_document, dict):
            raise OperationError("receipt spec.subject is required")
        try:
            subject = ReceiptSubject.from_dict(subject_document)
            subject_gvk = subject.gvk
        except (TypeError, ValueError) as exc:
            raise OperationError(f"receipt subject is invalid: {exc}") from exc
        driver_name = self.driver_names_by_gvk.get(str(subject_gvk))
        driver = self.drivers.get(driver_name) if driver_name is not None else None
        if driver is None:
            raise OperationError(f"receipt subject does not identify an installed unit driver: {subject_gvk}")
        assert driver_name is not None
        if subject.name != name:
            raise OperationError("receipt subject name must match metadata.name")
        if expected_unit is not None and subject.name != expected_unit:
            raise OperationError(f"receipt subject.name must be {expected_unit!r}")
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError

        try:
            Draft202012Validator(receipt_resource_schema(driver_name)).validate(
                {key: value for key, value in document.items() if key != "$schema"}
            )
        except ValidationError as exc:
            raise OperationError(f"invalid {driver_name} receipt: {exc.message}") from exc
        try:
            desired_document = specification.get("desired")
            if not isinstance(desired_document, dict):
                raise ValueError("receipt spec.desired must be an object")
            desired = ReceiptDesired.from_dict(desired_document)
            resolved_inputs_document = specification.get("resolvedInputs")
            if resolved_inputs_document is not None and not isinstance(resolved_inputs_document, dict):
                raise ValueError("receipt spec.resolvedInputs must be an object")
            resolved_inputs = (
                ResolvedInputs.from_dict(resolved_inputs_document) if resolved_inputs_document is not None else None
            )
            controller = JsonObjectValue._deserialize(status["controller"])
            result = driver.result_contract.parse(status["result"])
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid {driver_name} receipt: {exc}") from exc
        artifacts_document = status.get("artifacts")
        if artifacts_document is not None and not isinstance(artifacts_document, dict):
            raise OperationError(f"invalid {driver_name} receipt: status.artifacts must be an object")
        expected_artifacts = set(driver.artifact_outputs)
        actual_artifacts = set(artifacts_document) if artifacts_document is not None else set()
        if actual_artifacts != expected_artifacts:
            raise OperationError(
                f"persisted {driver_name} receipt describes artifacts {sorted(actual_artifacts)}; "
                f"expected {sorted(expected_artifacts)}"
            )
        artifacts = None
        if artifacts_document is not None:
            try:
                artifacts = {}
                for artifact_name, descriptor in artifacts_document.items():
                    if not isinstance(descriptor, dict):
                        raise ValueError(f"artifact {artifact_name!r} must be an object")
                    artifacts[artifact_name] = ArtifactDescriptor.from_dict(descriptor)
            except (TypeError, ValueError) as exc:
                raise OperationError(f"invalid {driver_name} receipt artifacts: {exc}") from exc
        return ReceiptResource(
            gvk=subject_gvk,
            metadata=ResourceMetadata(name=name),
            driver=driver,
            spec=ReceiptSpec(subject=subject, desired=desired, resolvedInputs=resolved_inputs),
            status=ReceiptStatus(controller=controller, result=result, artifacts=artifacts),
        )

    def serialize_receipt(self, receipt: ReceiptResource[Any]) -> JsonObject:
        result = receipt.driver.result_contract.dump(receipt.status.result)
        specification: JsonObject = {
            "subject": receipt.spec.subject.to_dict(),
            "desired": self._compact(receipt.spec.desired.to_dict()),
        }
        if receipt.spec.resolvedInputs is not None:
            specification["resolvedInputs"] = self._compact(receipt.spec.resolvedInputs.to_dict())
        status: JsonObject = {
            "controller": dict(receipt.status.controller),
            "result": result,
        }
        if receipt.status.artifacts is not None:
            status["artifacts"] = {
                name: self._compact(descriptor.to_dict()) for name, descriptor in receipt.status.artifacts.items()
            }
        return {
            "$schema": resource_schema_url(receipt.gvk.api_version, receipt.gvk.kind, "receipt"),
            "apiVersion": CORE_API_VERSION,
            "kind": "Receipt",
            "metadata": {"name": receipt.metadata.name},
            "spec": specification,
            "status": status,
        }

    def load_receipt(self, path: Path, expected_unit: str | None = None) -> ReceiptResource[Any]:
        document = self.load_document(path)
        selected_name = expected_unit or path.stem
        return self.parse_receipt(document, PurePosixPath(selected_name).parts[-1])

    @staticmethod
    def resource_documents_enabled(root: Path) -> bool:
        return any((root / name).is_file() for name in PROJECT_CONFIG_NAMES)

    def unit_document_path(self, root: Path, unit_name: str, project_root: Path | None = None) -> Path:
        posix = PurePosixPath(unit_name)
        if (
            not posix.parts
            or unit_name != posix.as_posix()
            or any(part in {".", ".."} for part in posix.parts)
            or posix.is_absolute()
        ):
            raise OperationError(f"invalid Unit qualified name {unit_name!r}")
        parts = posix.parts
        directory = root / "units" / Path(*parts[:-1])
        local_name = parts[-1]
        candidates = document_candidates(directory, local_name)
        if len(candidates) > 1:
            raise OperationError(
                f"multiple document formats exist for unit {unit_name}: {', '.join(map(str, candidates))}"
            )
        if candidates:
            return candidates[0]
        if project_root is not None and self.resource_documents_enabled(project_root):
            from gitopsctr.registry import RESOURCE_REGISTRY
            from gitopsctr.resource_model import ResourcePlane

            project = load_project_config(project_root)
            return RESOURCE_REGISTRY.document_path(
                family="unit",
                plane=ResourcePlane.DESIRED,
                root=root,
                repository_root=project_root,
                project=project,
                environment=None,
                qualified_name=unit_name,
                suffix=project.write_format.suffix,
            )
        return directory / f"{local_name}.json"

    def load_unit[ModelT: StrictModel](
        self,
        path: Path,
        expected_name: str | None = None,
        *,
        profile: Literal["authored", "resolved", "desired"],
    ) -> UnitResource[ModelT]:
        document = self.load_document(path)
        selected_name = expected_name or path.stem
        return self.parse_unit(
            document,
            profile=profile,
            expected_name=PurePosixPath(selected_name).parts[-1],
        )

    def reference_document_path(self, root: Path, reference: str) -> Path:
        exact = root / reference
        if exact.is_file():
            return exact
        path = PurePosixPath(reference)
        return self.unit_document_path(root, path.stem) if len(path.parts) == 2 and path.parts[0] == "units" else exact

    def write_unit(self, path: Path, unit: UnitResource[Any], project_root: Path) -> Path:
        try:
            selected = load_project_config(project_root).write_format
            return write_document(path.with_suffix(selected.suffix), self.serialize_unit(unit), format=selected)
        except DocumentFormatError as exc:
            raise OperationError(str(exc)) from exc

    @staticmethod
    def parse_contract[ModelT: StrictModel](
        contract: TypedDocumentContract[ModelT], document: object, description: str
    ) -> ModelT:
        try:
            return contract.parse(document)
        except ContractError as exc:
            raise OperationError(f"invalid {description}: {exc}") from exc

    @staticmethod
    def parse_artifact[ResourceT](
        artifact_api: ArtifactApi[ResourceT], document: object, description: str
    ) -> ResourceT:
        try:
            return artifact_api.parse(document)
        except ContractError as exc:
            raise OperationError(f"invalid {description}: {exc}") from exc

    def validate_receipt(self, document: object, description: str) -> ReceiptResource[Any]:
        if not isinstance(document, dict):
            raise OperationError(f"invalid {description}: expected a JSON object")
        try:
            return self.parse_receipt(cast(JsonObject, document))
        except OperationError as exc:
            raise OperationError(f"invalid {description}: {exc}") from exc

    def write_preferred(self, path: Path, value: JsonObject | ReceiptResource[Any], project_root: Path) -> Path:
        try:
            selected = load_project_config(project_root).write_format
            if isinstance(value, ReceiptResource):
                value = self.serialize_receipt(value)
            elif value.get("source") is not None and value.get("specificationRevision") is not None:
                value = self.serialize_promotion(value)
            return write_document(path.with_suffix(selected.suffix), value, format=selected)
        except DocumentFormatError as exc:
            raise OperationError(str(exc)) from exc
