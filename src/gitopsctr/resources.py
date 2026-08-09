"""Resource envelope parsing, serialization, and typed document loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from gitopsctr.api import GVK
from gitopsctr.artifacts import ArtifactApi
from gitopsctr.contracts import ArtifactDescriptor, ReceiptDesired, ResolvedInputs, StrictModel
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

    def with_spec[NextT: StrictModel](self, spec: NextT) -> UnitResource[NextT]:
        return UnitResource(self.gvk, self.metadata, self.driver, spec)


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
        contract = {
            "authored": driver.unit_contract,
            "resolved": driver.resolved_unit_contract,
            "desired": driver.desired_unit_contract,
        }[profile]
        model = self.parse_contract(contract, specification, f"{profile} {driver.driver_name} unit {name}")
        return cast(UnitResource[ModelT], UnitResource(gvk, ResourceMetadata(name=name), driver, model))

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
        return {
            "$schema": resource_schema_url(api_version, kind, "authored" if profile == "authored" else "desired"),
            "apiVersion": api_version,
            "kind": kind,
            "metadata": unit.metadata.to_dict(),
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
            "metadata": receipt.metadata.to_dict(),
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
