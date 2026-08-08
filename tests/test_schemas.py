from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from gitopsctr import schemas
from gitopsctr.contracts import CORE_CONTRACTS, ContractError
from gitopsctr.driver import UNIT_PLUGINS

ROOT = Path(__file__).parents[1]
REVISION = "a" * 40
DIGEST = "sha256:" + "1" * 64


def authored_examples() -> list[dict]:
    paths = sorted((ROOT / "tests/fixtures").rglob("units/*.json")) + sorted((ROOT / "demo").rglob("units/*.json"))
    return [json.loads(path.read_text()) for path in paths]


def desired_example(unit: dict) -> dict:
    desired = deepcopy(unit)
    driver = desired["driver"]
    desired["$schema"] = schemas.driver_schema(driver, "desired-unit")["$id"]
    desired["source"] |= {
        "revision": REVISION,
        "inputHash": DIGEST,
        "driverVersion": UNIT_PLUGINS[driver].version,
    }
    if driver == "kubernetes-manifests":
        desired["materialization"] = {
            "path": f"manifests/{desired['name']}",
            "digest": DIGEST,
            "mediaType": "application/vnd.gitopsctr.kubernetes-manifests.v1",
            "metadata": {
                "renderer": "helm",
                "version": "v3.17.1",
                "releaseName": "web",
                "namespace": "default",
                "inventory": [{"apiVersion": "v1", "kind": "ConfigMap", "namespace": "default", "name": "web"}],
            },
        }
    return desired


@pytest.mark.parametrize("unit", authored_examples(), ids=lambda unit: f"{unit['driver']}-{unit['name']}")
def test_builtin_contracts_validate_authored_and_desired_examples(unit):
    plugin = UNIT_PLUGINS[unit["driver"]]

    plugin.unit_contract.validate(unit)
    plugin.desired_unit_contract.validate(desired_example(unit))

    invalid = {**unit, "unexpected": True}
    with pytest.raises(ContractError, match="Additional properties"):
        plugin.unit_contract.validate(invalid)


def test_schema_hint_is_ignored_for_runtime_validation():
    unit = authored_examples()[0]
    contract = UNIT_PLUGINS[unit["driver"]].unit_contract

    for hint in (None, "https://example.invalid/schema.json", 42):
        candidate = {key: value for key, value in unit.items() if key != "$schema"}
        if hint is not None:
            candidate["$schema"] = hint
        assert contract.validate(candidate) == candidate


RESULTS = {
    "terraform": {"applied": {"sourceRevision": REVISION, "path": "infra"}, "outputs": {}},
    "oci-images": {
        "artifacts": {
            "containers.json": {
                "schema": 1,
                "unit": {
                    "name": "images",
                    "driver": "oci-images",
                    "inputHashVersion": 1,
                    "inputHash": DIGEST,
                    "sourceRevision": REVISION,
                },
                "artifacts": {"app": {"type": "oci-image", "uri": f"registry.example/app@{DIGEST}"}},
            }
        }
    },
    "vite-oci-bundle": {
        "artifacts": {
            "frontend.json": {
                "schema": 1,
                "unit": {
                    "name": "web",
                    "driver": "vite-oci-bundle",
                    "inputHashVersion": 1,
                    "inputHash": DIGEST,
                    "sourceRevision": REVISION,
                },
                "artifacts": {
                    "bundle": {
                        "type": "oci-artifact",
                        "artifactType": "application/vnd.gitopsctr.frontend.v1",
                        "uri": f"registry.example/web@{DIGEST}",
                    }
                },
            }
        }
    },
    "frontend-s3-cloudfront": {
        "published": {
            "sourceRevision": REVISION,
            "path": "web",
            "bundle": f"registry.example/web@{DIGEST}",
            "artifactDigest": DIGEST,
            "runtimeConfigHash": DIGEST,
            "url": "https://example.invalid",
        }
    },
    "kubernetes-manifests": {"applied": {"manifestDigest": DIGEST, "inventory": []}},
}


@pytest.mark.parametrize("driver", sorted(RESULTS))
def test_result_and_composed_receipt_schemas(driver):
    plugin = UNIT_PLUGINS[driver]
    result = RESULTS[driver]
    plugin.result_contract.validate(result)
    receipt = {
        "$schema": schemas.driver_schema(driver, "receipt")["$id"],
        "schema": 1,
        "unit": "example",
        "driver": driver,
        "desired": {"revision": REVISION, "unitBlob": "f" * 40},
        "resolvedInputs": {},
        "controller": {"version": "0.1.0", "revision": REVISION, "observed_at": "2026-08-08T00:00:00Z"},
        **result,
    }

    receipt_schema = schemas.driver_schema(driver, "receipt")
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator(receipt_schema).validate(receipt)
    assert len(receipt_schema["allOf"]) == 2


def test_latest_aliases_resolve_from_the_local_schema_catalog():
    documents = schemas.schema_documents()
    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document)) for document in documents.values() if "$id" in document
    )
    terraform_unit = next(unit for unit in authored_examples() if unit["driver"] == "terraform")

    Draft202012Validator(
        documents[Path("drivers/terraform/latest/unit.schema.json")],
        registry=registry,
    ).validate(terraform_unit)


def test_generated_schemas_and_examples_validate_from_the_local_catalog():
    documents = schemas.schema_documents()
    published = [document for document in documents.values() if "$id" in document]
    registry = Registry().with_resources((document["$id"], Resource.from_contents(document)) for document in published)
    by_id = {document["$id"]: document for document in published}

    for document in published:
        Draft202012Validator.check_schema(document)
    for authored in authored_examples():
        Draft202012Validator(by_id[authored["$schema"]], registry=registry).validate(authored)
        desired = desired_example(authored)
        Draft202012Validator(by_id[desired["$schema"]], registry=registry).validate(desired)
    for path in sorted((ROOT / "tests/fixtures").rglob("environment.json")):
        environment = json.loads(path.read_text())
        Draft202012Validator(by_id[environment["$schema"]], registry=registry).validate(environment)


def test_schema_catalog_is_deterministic_checkable_and_preserves_history(tmp_path):
    assert schemas.export_schemas(tmp_path)
    first = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    assert schemas.export_schemas(tmp_path, check=True) == []
    assert first == {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    historical = tmp_path / "drivers/terraform/v1/unit.schema.json"
    historical.parent.mkdir(parents=True, exist_ok=True)
    historical.write_text("{}\n")
    current = tmp_path / "drivers/terraform/v2/unit.schema.json"
    current.write_text("{}\n")
    assert Path("drivers/terraform/v2/unit.schema.json") in schemas.export_schemas(tmp_path, check=True)
    schemas.export_schemas(tmp_path)
    assert historical.read_text() == "{}\n"

    index = json.loads((tmp_path / "index.json").read_text())
    assert index["drivers"]["terraform"]["version"] == 2
    latest = json.loads((tmp_path / "drivers/terraform/latest/unit.schema.json").read_text())
    assert latest["$ref"].endswith("/drivers/terraform/v2/unit.schema.json")


def test_core_schemas_are_draft_2020_12_and_environment_examples_validate():
    for contract in CORE_CONTRACTS.values():
        Draft202012Validator.check_schema(contract.json_schema())
    for path in sorted((ROOT / "tests/fixtures").rglob("environment.json")):
        CORE_CONTRACTS["environment"].validate(json.loads(path.read_text()))


def test_schema_cli_show_export_and_check_work_outside_a_git_repository(tmp_path):
    command = [sys.executable, "-m", "gitopsctr.cli"]
    shown = subprocess.run(
        [*command, "schemas", "show", "terraform", "unit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(shown.stdout)["$id"].endswith("/drivers/terraform/v2/unit.schema.json")

    destination = tmp_path / "schemas"
    subprocess.run([*command, "schemas", "export", str(destination)], cwd=tmp_path, check=True)
    subprocess.run([*command, "schemas", "export", str(destination), "--check"], cwd=tmp_path, check=True)
    (destination / "core/v1/environment.schema.json").write_text("{}\n")
    stale = subprocess.run(
        [*command, "schemas", "export", str(destination), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1
    assert "generated schemas are stale" in stale.stderr
