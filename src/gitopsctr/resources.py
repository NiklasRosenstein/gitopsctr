"""Resource envelope parsing, serialization, and typed document loading."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import uuid4

from gitopsctr.api import GVK
from gitopsctr.artifacts import ArtifactApi
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    ArtifactDescriptor,
    AuthoredResourceMetadata,
    DesiredLifecycle,
    DesiredResourceMetadata,
    DesiredStackDocument,
    DesiredStackSpec,
    DesiredStackTemplateDocument,
    DesiredStackTemplateSpec,
    LifecycleManagement,
    ReceiptDesired,
    ResolvedInputs,
    StackDocument,
    StackSpec,
    StackTemplateDocument,
    StackTemplateFromResource,
    StackTemplateSpec,
    StrictModel,
    scope_stack_template_resources,
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

CORE_API_VERSION = "gitopsctr.io/v1"
UNIT_API_VERSION = "unit.gitopsctr.io/v1"


@dataclass(frozen=True, kw_only=True)
class ResourceMetadata(StrictModel):
    name: str
    uid: str | None = None
    lifecycle: DesiredLifecycle | None = None

    @property
    def is_legacy_compatibility(self) -> bool:
        return self.uid is None and self.lifecycle is None

    def validate_desired(self) -> None:
        if self.is_legacy_compatibility:
            return
        if self.uid is None or self.lifecycle is None:
            raise ValueError("desired metadata requires both uid and lifecycle")
        DesiredResourceMetadata(name=self.name, uid=self.uid, lifecycle=self.lifecycle)

    def as_desired(self) -> DesiredResourceMetadata:
        self.validate_desired()
        if self.is_legacy_compatibility:
            raise ValueError("legacy metadata has no desired identity")
        assert self.uid is not None
        assert self.lifecycle is not None
        return DesiredResourceMetadata(name=self.name, uid=self.uid, lifecycle=self.lifecycle)

    @classmethod
    def new_source_tracked(cls, name: str) -> ResourceMetadata:
        return cls(
            name=name,
            uid=uuid4().hex,
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked")),
        )

    @classmethod
    def source_tracked_from_provenance(cls, name: str, provenance: str) -> ResourceMetadata:
        """Create a source-tracked identity for one desired proposal."""

        digest = hashlib.sha256(f"gitopsctr/desired-uid/v1\0{provenance}".encode()).hexdigest()[:32]
        return cls(
            name=name,
            uid=f"d1-{digest}",
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked")),
        )

    def document(self, *, profile: Literal["authored", "desired"]) -> JsonObject:
        if profile == "authored":
            if self.uid is not None or self.lifecycle is not None:
                raise ValueError("authored metadata may contain only name")
            return {"name": self.name}
        if self.is_legacy_compatibility:
            raise ValueError("desired metadata must be canonical; adopt legacy identity before serialization")
        document = self.as_desired().to_dict()
        lifecycle = document.get("lifecycle")
        if isinstance(lifecycle, dict):
            document["lifecycle"] = {key: value for key, value in lifecycle.items() if value is not None}
        return document


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

    @property
    def is_legacy_compatibility(self) -> bool:
        return self.metadata.is_legacy_compatibility

    def with_metadata(self, metadata: ResourceMetadata) -> UnitResource[ModelT]:
        return UnitResource(self.gvk, metadata, self.driver, self.spec)

    def with_spec[NextT: StrictModel](self, spec: NextT) -> UnitResource[NextT]:
        return UnitResource(self.gvk, self.metadata, self.driver, spec)


@dataclass(frozen=True)
class StackResource:
    """A typed Stack or StackTemplate resource in the desired graph."""

    gvk: GVK
    metadata: ResourceMetadata
    spec: StackSpec | DesiredStackSpec | StackTemplateSpec | DesiredStackTemplateSpec

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def is_legacy_compatibility(self) -> bool:
        return self.metadata.is_legacy_compatibility


DesiredGraphResource = UnitResource[Any] | StackResource


def _stack_template_name(spec: StackSpec | DesiredStackSpec) -> str:
    """Return the logical template name from old or current Stack syntax."""

    template = spec.template
    return template if isinstance(template, str) else template.name


def _stack_uses_resource_template(spec: StackSpec | DesiredStackSpec) -> bool:
    """Return whether the Stack must resolve a sibling desired StackTemplate."""

    template = spec.template
    return isinstance(template, str) or isinstance(template.source, StackTemplateFromResource)


def validate_desired_resource_graph(resources: Mapping[tuple[str, str, str], DesiredGraphResource]) -> None:
    """Validate UID fencing and acyclicity for resources from one desired ref.

    The mapping is deliberately scoped to one desired ref: the current document
    loader has no ref identifier in an individual resource envelope, so callers
    must not combine resources from different refs here.
    """

    identities: dict[tuple[str, str, str], DesiredGraphResource] = {}
    legacy_keys: set[tuple[str, str, str]] = set()
    for key, unit in resources.items():
        expected_key = (unit.gvk.api_version, unit.gvk.kind, unit.name)
        if expected_key in identities:
            raise ValueError(f"duplicate desired resource identity: {expected_key!r}")
        if key != expected_key:
            raise ValueError(f"desired resource mapping key {key!r} does not match resource identity {expected_key!r}")
        if unit.is_legacy_compatibility:
            # Legacy desired documents are compatibility roots. They may gate
            # graph publication while migration is in progress, but cannot
            # participate in UID-fenced ownership until explicitly adopted.
            legacy_keys.add(key)
        identities[key] = unit
    edges: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for key, unit in identities.items():
        if key in legacy_keys:
            continue
        unit.metadata.validate_desired()
        lifecycle = unit.metadata.lifecycle
        assert lifecycle is not None
        owner = lifecycle.owner
        if owner is None:
            continue
        owner_key = (owner.apiVersion, owner.kind, owner.name)
        if owner_key in legacy_keys:
            raise ValueError(f"desired owner reference for {key[2]!r} cannot target a legacy compatibility root")
        owner_resource = identities.get(owner_key)
        if owner_resource is None:
            raise ValueError(f"desired owner reference for {key[2]!r} does not identify a resource in this ref")
        if owner_resource.metadata.uid != owner.uid:
            raise ValueError(f"desired owner reference for {key[2]!r} is fenced by a different UID")
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

    # StackTemplate dependency declarations are retained in the desired
    # StackTemplate document. Re-check them after expansion so a projected
    # graph cannot silently omit a generated Unit or its dependency edge.
    templates = {
        resource.name: resource
        for resource in identities.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate"
    }
    for stack in (
        resource
        for resource in identities.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack"
    ):
        if not isinstance(stack.spec, (StackSpec, DesiredStackSpec)):
            raise ValueError(f"Stack {stack.name!r} has an invalid Stack spec")
        lifecycle = stack.metadata.lifecycle
        if lifecycle is None or lifecycle.management is None:
            raise ValueError(f"Stack {stack.name!r} must be a root resource")
        has_provenance = isinstance(stack.spec, DesiredStackSpec) and stack.spec.provenance is not None
        if lifecycle.management.mode == "direct" and not has_provenance:
            raise ValueError(f"direct Stack {stack.name!r} is missing instantiation provenance")
        if lifecycle.management.mode == "sourceTracked" and has_provenance:
            raise ValueError(f"source-tracked Stack {stack.name!r} must not carry direct instantiation provenance")
        if not _stack_uses_resource_template(stack.spec):
            # Git and promotion sources are self-contained in the desired
            # Stack projection. They do not require a sibling catalog entry.
            continue
        template_name = _stack_template_name(stack.spec)
        template = templates.get(template_name)
        if template is None:
            raise ValueError(f"Stack {stack.name!r} references missing StackTemplate {template_name!r} in this ref")
        assert isinstance(template.spec, StackTemplateSpec)
        expanded = scope_stack_template_resources(stack.name, template.spec.expand(stack.spec.parameters))
        expanded_by_name = {resource.name: resource for resource in expanded}
        for generated in expanded:
            generated_key = (generated.apiVersion, generated.kind, generated.name)
            generated_resource = identities.get(generated_key)
            if generated_resource is None:
                raise ValueError(f"Stack {stack.name!r} expansion is missing generated Unit {generated.name!r}")
            if not isinstance(generated_resource, UnitResource):
                raise ValueError(f"Stack {stack.name!r} expansion {generated.name!r} is not a Unit")
            generated_lifecycle = generated_resource.metadata.lifecycle
            owner = generated_lifecycle.owner if generated_lifecycle is not None else None
            expected_owner = (
                stack.gvk.api_version,
                stack.gvk.kind,
                stack.name,
                stack.metadata.uid,
            )
            actual_owner = (owner.apiVersion, owner.kind, owner.name, owner.uid) if owner is not None else None
            if actual_owner != expected_owner:
                raise ValueError(
                    f"Stack {stack.name!r} generated Unit {generated.name!r} has an invalid owner reference"
                )
            for dependency in generated.dependsOn:
                dependency_resource = expanded_by_name.get(dependency)
                if dependency_resource is None:
                    raise ValueError(
                        f"Stack {stack.name!r} Unit {generated.name!r} depends on missing generated Unit {dependency!r}"
                    )
                dependency_key = (
                    dependency_resource.apiVersion,
                    dependency_resource.kind,
                    dependency_resource.name,
                )
                if dependency_key not in identities:
                    raise ValueError(f"Stack {stack.name!r} dependency {dependency!r} is absent from this ref")


@dataclass(frozen=True, kw_only=True)
class ReceiptSubject(StrictModel):
    apiVersion: str
    kind: str
    name: str

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

    @staticmethod
    def _without_legacy_schema(document: JsonObject) -> JsonObject:
        if document.get("schema") == 1:
            return {key: value for key, value in document.items() if key != "schema"}
        return document

    def normalize_environment(self, document: JsonObject, expected_name: str | None = None) -> JsonObject:
        if document.get("apiVersion") is None:
            return self._without_legacy_schema(document)
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
        if document.get("apiVersion") is None:
            return self._without_legacy_schema(document)
        if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Promotion":
            raise OperationError("promotion must use apiVersion gitopsctr.io/v1 and kind Promotion")
        specification = document.get("spec")
        if not isinstance(specification, dict):
            raise OperationError("promotion envelope requires a spec mapping")
        return dict(specification)

    def parse_unit[ModelT: StrictModel](
        self,
        document: JsonObject,
        *,
        profile: Literal["authored", "resolved", "desired"],
        expected_name: str | None = None,
    ) -> UnitResource[ModelT]:
        """Parse a persisted envelope directly into its registered typed specification."""

        if document.get("apiVersion") is None:
            legacy = self._without_legacy_schema(document)
            name, driver_name = legacy.get("name"), legacy.get("driver")
            if not isinstance(name, str) or not isinstance(driver_name, str):
                raise OperationError("legacy unit requires string name and driver fields")
            driver = self.drivers.get(driver_name)
            if driver is None:
                raise OperationError(f"unit uses an unknown driver: {driver_name!r}")
            specification: object = {
                key: value for key, value in legacy.items() if key not in {"$schema", "schema", "name", "driver"}
            }
            gvk = GVK(driver.api_version, driver.kind)
        else:
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
        if document.get("apiVersion") is None:
            metadata_model = ResourceMetadata(name=name)
        elif profile == "authored":
            if set(metadata) != {"name"}:
                raise OperationError("authored unit metadata may contain only name")
            metadata_model = ResourceMetadata(name=name)
        elif profile == "desired":
            try:
                if set(metadata) == {"name"}:
                    metadata_model = ResourceMetadata(name=name)
                else:
                    metadata_model = ResourceMetadata.from_dict(metadata)
                    if metadata_model.is_legacy_compatibility:
                        raise ValueError("desired metadata with lifecycle fields cannot use null values")
                    metadata_model.validate_desired()
            except (TypeError, ValueError, KeyError) as exc:
                raise OperationError(f"desired unit {name} has invalid lifecycle metadata: {exc}") from exc
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
        return ResourceMetadata(name=document.name, uid=document.uid, lifecycle=document.lifecycle)

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
            if not isinstance(resource.spec, StackTemplateSpec):
                raise OperationError("StackTemplate resource has an invalid spec")
            if profile == "authored":
                document = StackTemplateDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="StackTemplate",
                    metadata=AuthoredResourceMetadata(name=resource.name),
                    spec=resource.spec,
                )
            else:
                desired_template_spec = (
                    resource.spec
                    if isinstance(resource.spec, DesiredStackTemplateSpec)
                    else DesiredStackTemplateSpec(
                        parameters=resource.spec.parameters,
                        unitTemplates=resource.spec.unitTemplates,
                        resources=resource.spec.resources,
                    )
                )
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
                if not isinstance(resource.spec, StackSpec) or getattr(resource.spec, "provenance", None) is not None:
                    raise OperationError("authored Stack metadata may not contain controller provenance")
                document = StackDocument(
                    apiVersion=CORE_API_VERSION,
                    kind="Stack",
                    metadata=AuthoredResourceMetadata(name=resource.name),
                    spec=resource.spec,
                )
            else:
                desired_spec = (
                    resource.spec
                    if isinstance(resource.spec, DesiredStackSpec)
                    else DesiredStackSpec(template=resource.spec.template, parameters=resource.spec.parameters)
                )
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
        value["$schema"] = resource_schema_url(CORE_API_VERSION, resource.gvk.kind, profile)
        return value

    def serialize_environment(self, environment: JsonObject) -> JsonObject:
        name = environment.get("name")
        if not isinstance(name, str):
            raise OperationError("environment is missing its name")
        specification = {key: value for key, value in environment.items() if key not in {"schema", "name", "$schema"}}
        return {
            "$schema": resource_schema_url(CORE_API_VERSION, "Environment"),
            "apiVersion": CORE_API_VERSION,
            "kind": "Environment",
            "metadata": {"name": name},
            "spec": specification,
        }

    def serialize_promotion(self, promotion: JsonObject) -> JsonObject:
        specification = {key: value for key, value in promotion.items() if key not in {"schema", "$schema"}}
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
        if self.strict_resource_documents(path) and document.get("apiVersion") is None:
            raise OperationError(f"legacy receipt document is not valid in a migrated project: {path}")
        return self.parse_receipt(document, expected_unit or path.stem)

    @staticmethod
    def resource_documents_enabled(root: Path) -> bool:
        return any((root / name).is_file() for name in PROJECT_CONFIG_NAMES)

    def unit_document_path(self, root: Path, unit_name: str, project_root: Path | None = None) -> Path:
        directory = root / "units"
        candidates = document_candidates(directory, unit_name)
        if len(candidates) > 1:
            raise OperationError(
                f"multiple document formats exist for unit {unit_name}: {', '.join(map(str, candidates))}"
            )
        if candidates:
            return candidates[0]
        if project_root is not None and self.resource_documents_enabled(project_root):
            return directory / f"{unit_name}{load_project_config(project_root).write_format.suffix}"
        return directory / f"{unit_name}.json"

    @staticmethod
    def strict_resource_documents(path: Path) -> bool:
        return any(
            any((parent / name).is_file() for name in PROJECT_CONFIG_NAMES) for parent in (path.parent, *path.parents)
        )

    def load_unit[ModelT: StrictModel](
        self,
        path: Path,
        expected_name: str | None = None,
        *,
        profile: Literal["authored", "resolved", "desired"],
    ) -> UnitResource[ModelT]:
        document = self.load_document(path)
        if self.strict_resource_documents(path) and document.get("apiVersion") is None:
            raise OperationError(f"legacy unit document is not valid in a migrated project: {path}")
        return self.parse_unit(document, profile=profile, expected_name=expected_name or path.stem)

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
