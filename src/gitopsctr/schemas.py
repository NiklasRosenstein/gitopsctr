"""Deterministic JSON Schema catalog for public resource documents."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from gitopsctr.api import GVK
from gitopsctr.artifacts import ArtifactApi, require_artifact_api
from gitopsctr.contracts import CORE_CONTRACTS, SCHEMA_ROOT, artifact_descriptors_schema, receipt_schema
from gitopsctr.document import DocumentContract, JsonObject
from gitopsctr.driver import UnitDriver
from gitopsctr.formats import PROJECT_RESOURCE_SCHEMA
from gitopsctr.registry import API_KINDS, UNIT_DRIVERS


def _driver_schema_id(driver: str, plugin: UnitDriver, kind: str) -> str:
    if plugin.schema_base_uri:
        return f"{plugin.schema_base_uri.rstrip('/')}/{kind}.schema.json"
    return f"urn:gitopsctr:schema:driver:{driver}:v{plugin.version}:{kind}"


def resource_schema_url(api_version: str, kind: str, profile: str | None = None) -> str:
    group, version = api_version.rsplit("/", 1)
    suffix = f"/{profile}" if profile else ""
    return f"{SCHEMA_ROOT}/apis/{group}/{version}/{kind}{suffix}.schema.json"


def _specification_schema(contract: DocumentContract) -> JsonObject:
    schema = deepcopy(contract.json_schema())
    schema.pop("$schema", None)
    schema.pop("$id", None)
    schema.pop("title", None)
    properties = cast(dict[str, Any], schema.get("properties", {}))
    required = [
        value for value in cast(list[str], schema.get("required", [])) if value not in {"$schema", "name", "driver"}
    ]
    for key in ("$schema", "name", "driver"):
        properties.pop(key, None)
    schema["properties"] = properties
    schema["required"] = cast(Any, required)
    return schema


def _resolved_inputs_schema() -> JsonObject:
    schema = CORE_CONTRACTS["receipt"].json_schema()
    properties = cast(dict[str, JsonObject], schema["properties"])
    return deepcopy(properties["resolvedInputs"])


def _resource_schema(
    *,
    schema_id: str,
    api_version: str,
    kind: str,
    spec: JsonObject,
) -> JsonObject:
    definitions = spec.pop("$defs", None)
    resource: JsonObject = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "title": f"{kind} ({api_version})",
        "type": "object",
        "properties": {
            "$schema": {"type": "string"},
            "apiVersion": {"const": api_version},
            "kind": {"const": kind},
            "metadata": {
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "spec": spec,
        },
        "required": ["apiVersion", "kind", "metadata", "spec"],
        "additionalProperties": False,
    }
    if definitions is not None:
        resource["$defs"] = definitions
    return resource


def unit_resource_schema(driver: str, profile: str = "authored") -> JsonObject:
    try:
        driver_instance = UNIT_DRIVERS[driver]
    except KeyError as exc:
        raise ValueError(f"unknown schema driver: {driver}") from exc
    contract = driver_instance.unit_contract if profile == "authored" else driver_instance.desired_unit_contract
    kind = driver_instance.kind
    return _resource_schema(
        schema_id=resource_schema_url(driver_instance.api_version, kind, profile),
        api_version=driver_instance.api_version,
        kind=kind,
        spec=_specification_schema(contract),
    )


def receipt_resource_schema(driver: str) -> JsonObject:
    try:
        driver_instance = UNIT_DRIVERS[driver]
    except KeyError as exc:
        raise ValueError(f"unknown schema driver: {driver}") from exc
    result = deepcopy(driver_instance.result_contract.json_schema())
    result.pop("$schema", None)
    result.pop("$id", None)
    result.pop("title", None)
    status_properties: dict[str, Any] = {
        "controller": {"type": "object"},
        "result": result,
    }
    status_required = ["controller", "result"]
    if driver_instance.artifact_outputs:
        status_properties["artifacts"] = artifact_descriptors_schema(
            {
                name: (
                    artifact_kind.gvk.api_version,
                    artifact_kind.gvk.kind,
                    require_artifact_api(artifact_kind).media_type,
                )
                for name, artifact_kind in driver_instance.artifact_outputs.items()
            }
        )
        status_required.append("artifacts")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": resource_schema_url(driver_instance.api_version, driver_instance.kind, "receipt"),
        "title": f"Receipt for {driver_instance.kind}",
        "type": "object",
        "properties": {
            "$schema": {"type": "string"},
            "apiVersion": {"const": "gitopsctr.io/v1"},
            "kind": {"const": "Receipt"},
            "metadata": {
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "spec": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "object",
                        "properties": {
                            "apiVersion": {"const": driver_instance.api_version},
                            "kind": {"const": driver_instance.kind},
                            "name": {"type": "string", "minLength": 1},
                        },
                        "required": ["apiVersion", "kind", "name"],
                        "additionalProperties": False,
                    },
                    "desired": {"type": "object"},
                    "resolvedInputs": _resolved_inputs_schema(),
                },
                "required": ["subject", "desired"],
                "additionalProperties": False,
            },
            "status": {
                "type": "object",
                "properties": status_properties,
                "required": status_required,
                "additionalProperties": False,
            },
        },
        "required": ["apiVersion", "kind", "metadata", "spec", "status"],
        "additionalProperties": False,
    }


def core_resource_schema(kind: str) -> JsonObject:
    if kind == "Environment":
        specification = _specification_schema(CORE_CONTRACTS["environment"])
    elif kind == "Promotion":
        specification = _specification_schema(CORE_CONTRACTS["promotion"])
    elif kind == "Receipt":
        specification = {
            "type": "object",
            "properties": {
                "subject": {"type": "object"},
                "desired": {"type": "object"},
                "resolvedInputs": _resolved_inputs_schema(),
            },
            "required": ["subject", "desired"],
            "additionalProperties": False,
        }
    else:
        raise ValueError(f"unknown core resource kind: {kind}")
    return _resource_schema(
        schema_id=resource_schema_url("gitopsctr.io/v1", kind),
        api_version="gitopsctr.io/v1",
        kind=kind,
        spec=cast(JsonObject, specification),
    )


def project_resource_schema() -> JsonObject:
    """Return the schema for the repository-level Project resource."""

    return cast(JsonObject, deepcopy(PROJECT_RESOURCE_SCHEMA))


def driver_schema(driver: str, kind: str) -> JsonObject:
    try:
        plugin = UNIT_DRIVERS[driver]
    except KeyError as exc:
        raise ValueError(f"unknown schema driver: {driver}") from exc
    contracts: dict[str, DocumentContract] = {
        "unit": plugin.unit_contract,
        "desired-unit": plugin.desired_unit_contract,
        "result": plugin.result_contract,
    }
    if kind == "receipt":
        schema = receipt_schema(
            driver,
            plugin.version,
            plugin.result_contract,
            {
                name: (
                    artifact_kind.gvk.api_version,
                    artifact_kind.gvk.kind,
                    require_artifact_api(artifact_kind).media_type,
                )
                for name, artifact_kind in plugin.artifact_outputs.items()
            },
        )
    else:
        try:
            schema = contracts[kind].json_schema()
        except KeyError as exc:
            raise ValueError(f"unknown driver schema kind: {kind}") from exc
    schema = deepcopy(schema)
    schema["$id"] = _driver_schema_id(driver, plugin, kind)
    return schema


def show_schema(scope: str, kind: str) -> JsonObject:
    if scope == "gitopsctr.io/v1":
        if kind == "Project":
            return project_resource_schema()
        return core_resource_schema(kind)
    api_kind = API_KINDS.get(GVK(scope, kind)) if "/" in scope else None
    if api_kind is not None and isinstance(api_kind.spec, ArtifactApi):
        return require_artifact_api(api_kind).json_schema()
    if scope.startswith("unit.gitopsctr.io/v1/"):
        return show_schema("unit.gitopsctr.io/v1", f"{scope.rsplit('/', 1)[1]}/{kind}")
    if scope == "unit.gitopsctr.io/v1":
        if "/" not in kind:
            raise ValueError("unit API schema kind must be <Kind>/<authored|desired|receipt>")
        resource_kind, profile = kind.split("/", 1)
        driver = next(
            (name for name, driver_instance in UNIT_DRIVERS.items() if driver_instance.kind == resource_kind),
            None,
        )
        if driver is None:
            raise ValueError(f"unknown unit API kind: {resource_kind}")
        if profile == "receipt":
            return receipt_resource_schema(driver)
        if profile not in {"authored", "desired"}:
            raise ValueError(f"unknown unit API schema profile: {profile}")
        return unit_resource_schema(driver, profile)
    raise ValueError(f"unknown schema API version: {scope}")


def schema_documents() -> dict[Path, JsonObject]:
    documents: dict[Path, JsonObject] = {}
    index: dict[str, Any] = {"schema": 1, "apis": {}}
    for kind in ("Environment", "Promotion", "Receipt", "Project"):
        path = Path("apis/gitopsctr.io/v1") / f"{kind}.schema.json"
        documents[path] = project_resource_schema() if kind == "Project" else core_resource_schema(kind)
        index["apis"][f"gitopsctr.io/v1/{kind}"] = path.as_posix()
    for gvk, api_kind in sorted(API_KINDS.items()):
        if not isinstance(api_kind.spec, ArtifactApi):
            continue
        group, version = gvk.api_version.rsplit("/", 1)
        path = Path("apis") / group / version / f"{gvk.kind}.schema.json"
        documents[path] = require_artifact_api(api_kind).json_schema()
        index["apis"][str(gvk)] = path.as_posix()
    for driver, driver_instance in sorted(UNIT_DRIVERS.items()):
        root = Path("apis/unit.gitopsctr.io/v1") / driver_instance.kind
        for profile in ("authored", "desired"):
            path = root / f"{profile}.schema.json"
            documents[path] = unit_resource_schema(driver, profile)
            index["apis"][f"unit.gitopsctr.io/v1/{driver_instance.kind}/{profile}"] = path.as_posix()
        path = root / "receipt.schema.json"
        documents[path] = receipt_resource_schema(driver)
        index["apis"][f"unit.gitopsctr.io/v1/{driver_instance.kind}/receipt"] = path.as_posix()
    documents[Path("index.json")] = cast(JsonObject, index)
    return documents


def encoded_schema(document: JsonObject) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def export_schemas(directory: Path, *, check: bool = False) -> list[Path]:
    documents = schema_documents()
    changed: list[Path] = []
    for relative, document in documents.items():
        path = directory / relative
        expected = encoded_schema(document)
        if not path.is_file() or path.read_text() != expected:
            changed.append(relative)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected)

    expected_paths = set(documents)
    existing_paths = (
        {path.relative_to(directory) for path in directory.rglob("*.json") if path.is_file()}
        if directory.is_dir()
        else set()
    )
    obsolete = sorted(existing_paths - expected_paths)
    changed.extend(obsolete)
    if not check:
        for relative in obsolete:
            (directory / relative).unlink()
        for path in sorted(
            (path for path in directory.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                path.rmdir()
            except OSError:
                pass
    return sorted(changed)
