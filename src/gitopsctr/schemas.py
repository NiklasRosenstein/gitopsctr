"""Deterministic JSON Schema catalog for core and plugin documents."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from gitopsctr.contracts import CORE_CONTRACTS, receipt_schema, schema_url
from gitopsctr.document import DocumentContract, JsonObject
from gitopsctr.driver import UNIT_PLUGINS, UnitPlugin

DRIVER_KINDS = ("unit", "desired-unit", "result", "receipt")
CORE_KINDS = tuple(CORE_CONTRACTS)


def _driver_schema_id(driver: str, plugin: UnitPlugin, kind: str) -> str:
    if plugin.schema_base_uri:
        return f"{plugin.schema_base_uri.rstrip('/')}/{kind}.schema.json"
    return f"urn:gitopsctr:schema:driver:{driver}:v{plugin.version}:{kind}"


def driver_schema(driver: str, kind: str) -> JsonObject:
    try:
        plugin = UNIT_PLUGINS[driver]
    except KeyError as exc:
        raise ValueError(f"unknown schema driver: {driver}") from exc
    contracts: dict[str, DocumentContract] = {
        "unit": plugin.unit_contract,
        "desired-unit": plugin.desired_unit_contract,
        "result": plugin.result_contract,
    }
    if kind == "receipt":
        schema = receipt_schema(driver, plugin.version, plugin.result_contract)
    else:
        try:
            schema = contracts[kind].json_schema()
        except KeyError as exc:
            raise ValueError(f"unknown driver schema kind: {kind}") from exc
    schema = deepcopy(schema)
    schema["$id"] = _driver_schema_id(driver, plugin, kind)
    return schema


def show_schema(scope: str, kind: str) -> JsonObject:
    if scope == "core":
        try:
            return CORE_CONTRACTS[kind].json_schema()
        except KeyError as exc:
            raise ValueError(f"unknown core schema kind: {kind}") from exc
    return driver_schema(scope, kind)


def schema_documents() -> dict[Path, JsonObject]:
    documents: dict[Path, JsonObject] = {}
    index: dict[str, Any] = {"schema": 1, "core": {}, "drivers": {}}
    for kind, contract in sorted(CORE_CONTRACTS.items()):
        path = Path("core/v1") / f"{kind}.schema.json"
        documents[path] = contract.json_schema()
        index["core"][kind] = path.as_posix()
    for driver, plugin in sorted(UNIT_PLUGINS.items()):
        version_root = Path("drivers") / driver / f"v{plugin.version}"
        latest_root = Path("drivers") / driver / "latest"
        driver_index = {"version": plugin.version, "schemas": {}}
        for kind in DRIVER_KINDS:
            version_path = version_root / f"{kind}.schema.json"
            latest_path = latest_root / f"{kind}.schema.json"
            documents[version_path] = driver_schema(driver, kind)
            documents[latest_path] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": schema_url(f"drivers/{driver}/latest", plugin.version, kind).replace(
                    f"/v{plugin.version}/", "/"
                ),
                "$ref": documents[version_path]["$id"],
            }
            driver_index["schemas"][kind] = version_path.as_posix()
        index["drivers"][driver] = driver_index
    documents[Path("index.json")] = cast(JsonObject, index)
    return documents


def encoded_schema(document: JsonObject) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def export_schemas(directory: Path, *, check: bool = False) -> list[Path]:
    changed: list[Path] = []
    for relative, document in schema_documents().items():
        path = directory / relative
        expected = encoded_schema(document)
        if not path.is_file() or path.read_text() != expected:
            changed.append(relative)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected)
    return changed
