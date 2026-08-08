from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from gitopsctr import cli, schemas
from gitopsctr.contracts import CORE_CONTRACTS, ContractError
from gitopsctr.registry import UNIT_DRIVERS

ROOT = Path(__file__).parents[1]
REVISION = "a" * 40
DIGEST = "sha256:" + "1" * 64


def authored_examples() -> list[dict]:
    paths = sorted((ROOT / "tests/fixtures").rglob("units/*.json")) + sorted((ROOT / "demo").rglob("units/*.json"))
    return [cli.normalize_unit_document(json.loads(path.read_text()), path.stem) for path in paths]


def desired_example(unit: dict) -> dict:
    desired = deepcopy(unit)
    desired.pop("schema", None)
    driver = desired["driver"]
    desired["$schema"] = schemas.resource_schema_url(
        UNIT_DRIVERS[driver].api_version,
        UNIT_DRIVERS[driver].kind,
        "desired",
    )
    desired["source"] |= {
        "revision": REVISION,
        "inputHash": DIGEST,
        "driverVersion": UNIT_DRIVERS[driver].version,
    }
    if driver == "kubernetes-manifests":
        desired["materialization"] = {
            "path": f"materialized/{desired['name']}",
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
    plugin = UNIT_DRIVERS[unit["driver"]]

    plugin.unit_contract.validate(unit)
    plugin.desired_unit_contract.validate(desired_example(unit))

    invalid = {**unit, "unexpected": True}
    with pytest.raises(ContractError, match="Additional properties"):
        plugin.unit_contract.validate(invalid)


def test_schema_hint_is_ignored_for_runtime_validation():
    unit = authored_examples()[0]
    contract = UNIT_DRIVERS[unit["driver"]].unit_contract

    for hint in (None, "https://example.invalid/schema.json", 42):
        candidate = {key: value for key, value in unit.items() if key != "$schema"}
        if hint is not None:
            candidate["$schema"] = hint
        assert contract.validate(candidate) == candidate


def test_source_schema_describes_repository_relative_path_semantics():
    schema = UNIT_DRIVERS["terraform"].unit_contract.json_schema()
    source = schema["properties"]["source"]
    assert "root of the selected source revision" in source["properties"]["path"]["description"]
    assert "relative to source.path" in source["properties"]["inputs"]["description"]


def test_resource_contracts_use_api_version_instead_of_envelope_schema_field():
    for driver in UNIT_DRIVERS.values():
        for contract in (driver.unit_contract, driver.desired_unit_contract):
            assert "schema" not in contract.json_schema().get("properties", {})
    for contract in CORE_CONTRACTS.values():
        assert "schema" not in contract.json_schema().get("properties", {})


RESULTS = {
    "terraform": {"applied": {"sourceRevision": REVISION, "path": "infra"}, "outputs": {}},
    "oci-images": {},
    "vite-oci-bundle": {},
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
def test_result_contracts_and_receipt_resource_schemas(driver):
    plugin = UNIT_DRIVERS[driver]
    result = RESULTS[driver]
    plugin.result_contract.validate(result)
    receipt = {
        "$schema": schemas.resource_schema_url(plugin.api_version, plugin.kind, "receipt"),
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Receipt",
        "metadata": {"name": "example"},
        "spec": {
            "subject": {"apiVersion": plugin.api_version, "kind": plugin.kind, "name": "example"},
            "desired": {"revision": REVISION, "unitBlob": "f" * 40},
            "resolvedInputs": {},
        },
        "status": {
            "controller": {"version": "0.1.0", "revision": REVISION, "observed_at": "2026-08-08T00:00:00Z"},
            "result": result,
        },
    }
    if plugin.artifact_outputs:
        receipt["status"]["artifacts"] = {
            name: {
                "apiVersion": artifact_kind.gvk.api_version,
                "kind": artifact_kind.gvk.kind,
                "path": f"artifacts/example/{name}.yaml",
                "digest": DIGEST,
                "mediaType": f"{artifact_kind.spec.media_type}+yaml",
            }
            for name, artifact_kind in plugin.artifact_outputs.items()
        }

    receipt_schema = schemas.receipt_resource_schema(driver)
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator(receipt_schema).validate(receipt)


def test_generated_schemas_and_examples_validate_from_the_local_catalog():
    documents = schemas.schema_documents()
    published = [document for document in documents.values() if "$id" in document]
    registry = Registry().with_resources((document["$id"], Resource.from_contents(document)) for document in published)
    by_id = {document["$id"]: document for document in published}

    for document in published:
        Draft202012Validator.check_schema(document)
    for authored in authored_examples():
        authored_resource = cli.serialize_unit_document(authored, profile="authored")
        Draft202012Validator(by_id[authored_resource["$schema"]], registry=registry).validate(authored_resource)
        desired = desired_example(authored)
        desired_resource = cli.serialize_unit_document(desired, profile="desired")
        Draft202012Validator(by_id[desired_resource["$schema"]], registry=registry).validate(desired_resource)
    for path in sorted((ROOT / "tests/fixtures").rglob("environment.json")):
        environment = {key: value for key, value in json.loads(path.read_text()).items() if key != "schema"}
        Draft202012Validator(by_id[environment["$schema"]], registry=registry).validate(environment)


def test_schema_catalog_is_deterministic_checkable_and_prunes_obsolete_schemas(tmp_path):
    assert schemas.export_schemas(tmp_path)
    first = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    assert schemas.export_schemas(tmp_path, check=True) == []
    assert first == {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    obsolete = tmp_path / "drivers/terraform/v1/unit.schema.json"
    obsolete.parent.mkdir(parents=True, exist_ok=True)
    obsolete.write_text("{}\n")
    current = tmp_path / "apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json"
    current.write_text("{}\n")
    changed = schemas.export_schemas(tmp_path, check=True)
    assert Path("drivers/terraform/v1/unit.schema.json") in changed
    assert Path("apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json") in changed
    schemas.export_schemas(tmp_path)
    assert not obsolete.exists()
    assert json.loads(current.read_text())["properties"]["kind"]["const"] == "Terraform"

    index = json.loads((tmp_path / "index.json").read_text())
    assert set(index) == {"schema", "apis"}
    assert index["apis"]["artifact.gitopsctr.io/v1/ContainerImages"] == (
        "apis/artifact.gitopsctr.io/v1/ContainerImages.schema.json"
    )


def test_core_schemas_are_draft_2020_12_and_environment_examples_validate():
    for contract in CORE_CONTRACTS.values():
        Draft202012Validator.check_schema(contract.json_schema())
    for path in sorted((ROOT / "tests/fixtures").rglob("environment.json")):
        CORE_CONTRACTS["environment"].validate(
            cli.normalize_environment_document(json.loads(path.read_text()), path.parent.name)
        )


def test_schema_cli_show_export_and_check_work_outside_a_git_repository(tmp_path):
    command = [sys.executable, "-m", "gitopsctr.cli"]
    shown = subprocess.run(
        [*command, "schemas", "show", "unit.gitopsctr.io/v1/Terraform", "authored"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(shown.stdout)["$id"].endswith("/apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json")

    destination = tmp_path / "schemas"
    subprocess.run([*command, "schemas", "export", str(destination)], cwd=tmp_path, check=True)
    subprocess.run([*command, "schemas", "export", str(destination), "--check"], cwd=tmp_path, check=True)
    (destination / "apis/gitopsctr.io/v1/Environment.schema.json").write_text("{}\n")
    stale = subprocess.run(
        [*command, "schemas", "export", str(destination), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1
    assert "generated schemas are stale" in stale.stderr


def test_schema_cli_can_show_resource_envelopes():
    environment = schemas.show_schema("gitopsctr.io/v1", "Environment")
    project = schemas.show_schema("gitopsctr.io/v1", "Project")
    artifact = schemas.show_schema("artifact.gitopsctr.io/v1", "ContainerImages")
    unit = schemas.show_schema("unit.gitopsctr.io/v1/Terraform", "authored")
    receipt = schemas.show_schema("unit.gitopsctr.io/v1", "Terraform/receipt")
    assert environment["properties"]["kind"]["const"] == "Environment"
    assert project["$id"].endswith("/apis/gitopsctr.io/v1/Project.schema.json")
    assert project["properties"]["kind"]["const"] == "Project"
    assert project["properties"]["spec"]["properties"]["writeFormat"]["enum"] == ["yaml", "json"]
    assert artifact["properties"]["kind"]["const"] == "ContainerImages"
    assert "images" in artifact["required"]
    assert unit["properties"]["kind"]["const"] == "Terraform"
    assert receipt["properties"]["kind"]["const"] == "Receipt"
