from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from gitopsctr import controller, schemas
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    INSPECTION_RESOURCE_LIST_CONTRACT,
    AuthoredSource,
    ContractError,
    DesiredSource,
    MaterializationDocument,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.driver import DriverError, MaterializationCapability, UnitResolutionContext
from gitopsctr.errors import ReferenceUnavailable
from gitopsctr.registry import UNIT_DRIVERS
from gitopsctr.resolution import FingerprintedValue, ResolutionContext, resolve_template
from gitopsctr.resources import ResourceMetadata, UnitResource

ROOT = Path(__file__).parents[1]
REVISION = "a" * 40
DIGEST = "sha256:" + "1" * 64


def authored_examples() -> list[UnitResource]:
    paths = sorted((ROOT / "tests/fixtures").rglob("units/*.json")) + sorted((ROOT / "demo").rglob("units/*.json"))
    return [controller.parse_authored_unit_document(json.loads(path.read_text()), path.stem) for path in paths]


def desired_example(unit: UnitResource) -> UnitResource:
    authored_source = unit.spec.source
    source = DesiredSource(
        path=authored_source.path,
        inputs=authored_source.inputs,
        revision=REVISION,
        inputHash=DIGEST,
        driverVersion=unit.driver.version,
    )
    resolved = unit.driver.resolve_unit(
        unit.spec,
        UnitResolutionContext(
            source=source,
            resolve_template=lambda value, pointer: resolve_template(
                value,
                ResolutionContext(
                    receipt=lambda _target: FingerprintedValue("resolved", DIGEST),
                    artifact=lambda _target: FingerprintedValue("resolved", DIGEST),
                    promotion=lambda _target: FingerprintedValue("resolved", DIGEST),
                    unit=unit.name,
                ),
                pointer,
            ),
        ),
    )
    unit.driver.resolved_unit_contract.validate(unit.driver.resolved_unit_contract.dump(resolved.unit))
    desired = resolved.unit
    if isinstance(unit.driver, MaterializationCapability):
        renderer = desired.materialize.type
        metadata = {"renderer": renderer, "inventory": []}
        if renderer == "helm":
            metadata |= {
                "version": "v3.17.1",
                "releaseName": desired.materialize.releaseName,
                "namespace": desired.materialize.namespace,
            }
        desired = unit.driver.finalize_materialization(
            desired,
            MaterializationDocument(
                path=f"materialized/{unit.name}",
                digest=DIGEST,
                mediaType="application/vnd.gitopsctr.kubernetes-manifests.v1",
                metadata=JsonObjectValue(metadata),
            ),
        )
    return unit.with_spec(desired).with_metadata(ResourceMetadata.new_root(unit.name, partition="application"))


def has_incomplete_frontend_inputs(unit: UnitResource) -> bool:
    return unit.driver_name == "frontend-s3-cloudfront" and (
        unit.spec.inputs is None or any(value is None for value in unit.spec.inputs.to_dict().values())
    )


@pytest.mark.parametrize("unit", authored_examples(), ids=lambda unit: f"{unit.driver_name}-{unit.name}")
def test_builtin_contracts_validate_the_full_typed_unit_pipeline(unit):
    plugin = unit.driver
    authored = plugin.unit_contract.dump(unit.spec)

    plugin.unit_contract.validate(authored)
    if has_incomplete_frontend_inputs(unit):
        with pytest.raises((DriverError, ReferenceUnavailable)):
            desired_example(unit)
        return
    desired = desired_example(unit)
    plugin.desired_unit_contract.validate(plugin.desired_unit_contract.dump(desired.spec))

    invalid = {**authored, "unexpected": True}
    with pytest.raises(ContractError, match="Additional properties"):
        plugin.unit_contract.validate(invalid)


def test_schema_hint_is_ignored_for_runtime_validation():
    unit = authored_examples()[0]
    contract = unit.driver.unit_contract
    authored = contract.dump(unit.spec)

    for hint in (None, "https://example.invalid/schema.json", 42):
        candidate = dict(authored)
        if hint is not None:
            candidate["$schema"] = hint
        assert contract.validate(candidate) == candidate


def test_source_schema_describes_repository_relative_path_semantics():
    schema = UNIT_DRIVERS["terraform"].unit_contract.json_schema()
    source = schema["properties"]["source"]
    assert "root of the selected source revision" in source["properties"]["path"]["description"]
    assert "relative to source.path" in source["properties"]["inputs"]["description"]


def test_exact_source_revision_is_supported_by_runtime_and_json_schema():
    contract = UNIT_DRIVERS["terraform"].unit_contract
    valid = {"source": {"path": ".", "revision": REVISION}}
    contract.validate(valid)
    Draft202012Validator(contract.json_schema()).validate({**valid, "terraform": {}})

    invalid = {"source": {"path": ".", "revision": "not-a-commit"}}
    with pytest.raises(ContractError):
        contract.validate(invalid)
    with pytest.raises(ValidationError):
        Draft202012Validator(contract.json_schema()).validate({**invalid, "terraform": {}})

    with pytest.raises(ValueError, match="exact lowercase 40-hex"):
        AuthoredSource(path=".", revision="main")
    with pytest.raises(ValueError, match="exact lowercase 40-hex"):
        DesiredSource(path=".", revision="main")
    with pytest.raises(TypeError):
        DesiredSource(path=".")  # type: ignore[call-arg]


def test_template_schema_accepts_integer_values():
    document = {
        "source": {"path": "."},
        "terraform": {"variables": {"replicas": 2}},
    }
    UNIT_DRIVERS["terraform"].unit_contract.validate(document)


def test_template_schema_accepts_implicit_promotion_and_recursive_dry_fallback():
    document = {
        "source": {"path": "."},
        "terraform": {
            "variables": {
                "image": {"fromPromotion": {"dryFallback": {"fromReceipt": {"unit": "images", "pointer": "/image"}}}}
            }
        },
    }

    UNIT_DRIVERS["terraform"].unit_contract.validate(document)


@pytest.mark.parametrize("target", [{"unit": None}, {"pointer": None}])
def test_template_schema_rejects_null_promotion_selectors(target):
    document = {
        "source": {"path": "."},
        "terraform": {"variables": {"image": {"fromPromotion": target}}},
    }

    with pytest.raises(ContractError):
        UNIT_DRIVERS["terraform"].unit_contract.validate(document)


def test_resource_contracts_use_api_version_instead_of_envelope_schema_field():
    for driver in UNIT_DRIVERS.values():
        for contract in (driver.unit_contract, driver.desired_unit_contract):
            assert "schema" not in contract.json_schema().get("properties", {})
    for contract in CORE_CONTRACTS.values():
        assert "schema" not in contract.json_schema().get("properties", {})


def test_inspection_resource_list_has_a_strict_generated_contract():
    document = {
        "apiVersion": "inspection.gitopsctr.io/v1",
        "kind": "ResourceList",
        "metadata": {},
        "items": [
            {
                "provenance": {
                    "environment": "dev",
                    "plane": "observed",
                    "ref": "gitopsctr/observed/dev",
                    "revision": REVISION,
                    "path": "artifacts/images/containers.yaml",
                },
                "address": {
                    "family": "artifact",
                    "scope": "environment",
                    "namespace": "dev",
                    "qualifiedName": "images/containers",
                },
                "document": {"apiVersion": "artifact.gitopsctr.io/v1", "kind": "ContainerImages"},
                "inspection": {"authentication": "CURRENT"},
            }
        ],
    }
    assert INSPECTION_RESOURCE_LIST_CONTRACT.validate(document) == document
    with pytest.raises(ContractError):
        INSPECTION_RESOURCE_LIST_CONTRACT.validate(
            {
                **document,
                "items": [{**document["items"][0], "inspection": {"authentication": "UNKNOWN"}}],
            }
        )


RESULTS = {
    "terraform": {"applied": {"sourceRevision": REVISION, "path": "infra"}, "outputs": {}},
    "oci-images": {},
    "vite-oci-bundle": {},
    "frontend-s3-cloudfront": {
        "published": {
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
    invalid_receipt = {
        **receipt,
        "spec": {**receipt["spec"], "resolvedInputs": {"nonsense": [1, 2]}},
    }
    with pytest.raises(ValidationError):
        Draft202012Validator(receipt_schema).validate(invalid_receipt)


def test_generated_schemas_and_examples_validate_from_the_local_catalog():
    documents = schemas.schema_documents()
    published = [document for document in documents.values() if "$id" in document]
    registry = Registry().with_resources((document["$id"], Resource.from_contents(document)) for document in published)
    by_id = {document["$id"]: document for document in published}

    for document in published:
        Draft202012Validator.check_schema(document)
    for authored in authored_examples():
        authored_resource = controller.serialize_unit_document(authored, profile="authored")
        Draft202012Validator(by_id[authored_resource["$schema"]], registry=registry).validate(authored_resource)
        if has_incomplete_frontend_inputs(authored):
            continue
        desired = desired_example(authored)
        desired_resource = controller.serialize_unit_document(desired, profile="desired")
        validator = Draft202012Validator(by_id[desired_resource["$schema"]], registry=registry)
        validator.validate(desired_resource)
    for path in sorted((ROOT / "tests/fixtures").rglob("environment.json")):
        environment = {key: value for key, value in json.loads(path.read_text()).items() if key != "schema"}
        Draft202012Validator(by_id[environment["$schema"]], registry=registry).validate(environment)


@pytest.mark.parametrize(
    "metadata",
    [
        {"uid": None},
        {"labels": None},
        {"labels": {"gitopsctr.io/partition": "Not Valid"}},
    ],
)
def test_generated_desired_schema_rejects_invalid_metadata(metadata):
    authored = authored_examples()[0]
    desired = desired_example(authored)
    document = controller.serialize_unit_document(desired, profile="desired")
    document["metadata"].update(metadata)
    schema = next(schema for schema in schemas.schema_documents().values() if schema.get("$id") == document["$schema"])

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(document)


def test_desired_stack_template_schema_exposes_direct_input_contract():
    schema = schemas.core_resource_schema("StackTemplate", "desired")
    spec = schema["properties"]["spec"]

    assert "contentDigest" in spec["properties"]
    assert "acquisition" in spec["properties"]
    assert "sourceContext" in spec["properties"]
    assert "requestedSource" not in spec["properties"]
    assert "resolvedSource" not in spec["properties"]
    assert "unitTemplates" in spec["properties"]
    assert "resources" not in spec["properties"]
    assert set(spec["required"]) >= {"parameters", "unitTemplates", "contentDigest", "acquisition"}
    acquisition = spec["properties"]["acquisition"]
    assert set(acquisition["required"]) == {"documentDigest", "requestedSource", "resolvedSource"}
    assert "fromInput" not in acquisition["properties"]


@pytest.mark.parametrize(
    ("kind", "source", "parameters", "acquisition", "requires_context"),
    [
        ("Terraform", None, [], None, False),
        ("FrontendS3Cloudfront", None, [], None, False),
        ("Terraform", {"path": "."}, [], None, True),
        (
            "Terraform",
            {"fromParameter": {"name": "source"}},
            [{"name": "source", "type": "object"}],
            None,
            True,
        ),
        (
            "Terraform",
            {"path": {"fromParameter": {"name": "source"}}},
            [{"name": "source", "type": "string"}],
            None,
            True,
        ),
        (
            "Terraform",
            None,
            [],
            {
                "documentDigest": DIGEST,
                "requestedSource": {
                    "fromGit": {
                        "repository": "https://example.invalid/templates.git",
                        "revision": "main",
                        "path": "templates/application.yaml",
                    }
                },
                "resolvedSource": {
                    "fromGit": {
                        "repository": "https://example.invalid/templates.git",
                        "revision": "a" * 40,
                        "path": "templates/application.yaml",
                    }
                },
            },
            True,
        ),
    ],
)
def test_desired_stack_template_schema_source_context_matrix(
    kind: str,
    source: object,
    parameters: list[dict[str, str]],
    acquisition: dict[str, object] | None,
    requires_context: bool,
):
    schema = schemas.core_resource_schema("StackTemplate", "desired")
    document = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "application", "uid": "template-uid"},
        "spec": {
            "parameters": parameters,
            "unitTemplates": {
                "app": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": kind,
                    "spec": {"source": source},
                }
            },
            "contentDigest": DIGEST,
            "acquisition": acquisition
            or {
                "documentDigest": DIGEST,
                "requestedSource": {"fromInput": {}},
                "resolvedSource": {"fromInput": {}},
            },
        },
    }

    validator = Draft202012Validator(schema)
    if requires_context:
        with pytest.raises(ValidationError):
            validator.validate(document)
        document["spec"]["sourceContext"] = {"repository": ".", "revision": "a" * 40}
    Draft202012Validator(schema).validate(document)


def test_authored_stack_template_schema_exposes_only_canonical_unit_templates():
    spec = schemas.core_resource_schema("StackTemplate", "authored")["properties"]["spec"]
    inline = next(variant for variant in spec["anyOf"] if "unitTemplates" in variant.get("properties", {}))

    assert "unitTemplates" in inline["properties"]
    assert "resources" not in inline["properties"]


@pytest.mark.parametrize(
    ("schema_kind", "profile", "mutation"),
    [
        ("StackTemplate", "desired", lambda spec: spec.update(requestedSource={"fromGit": {"path": "."}})),
        ("Stack", "authored", lambda spec: spec.update(template={"fromResource": {"name": "application"}})),
        ("Stack", "authored", lambda spec: spec.update(template={"fromGit": {"path": "."}})),
        ("StackTemplate", "authored", lambda spec: spec.update(fromPromotion={"name": "application"})),
        (
            "StackTemplate",
            "desired",
            lambda spec: spec["acquisition"].update(
                resolvedSource={
                    "fromGit": {
                        "repository": "https://example.invalid/templates.git",
                        "revision": "a" * 40,
                        "path": "templates/web.yaml",
                    }
                }
            ),
        ),
    ],
)
def test_stack_schemas_reject_unsupported_acquisition_shapes(schema_kind, profile, mutation):
    if schema_kind == "StackTemplate":
        spec = {
            "parameters": [],
            "unitTemplates": {
                "app": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {},
                }
            },
        }
        if profile == "desired":
            spec.update(
                contentDigest=DIGEST,
                acquisition={
                    "documentDigest": DIGEST,
                    "requestedSource": {"fromInput": {}},
                    "resolvedSource": {"fromInput": {}},
                },
            )
        document = {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "StackTemplate",
            "metadata": {"name": "application", **({"uid": "template-uid"} if profile == "desired" else {})},
            "spec": spec,
        }
    else:
        document = {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {"template": "application"},
        }
    mutation(document["spec"])
    schema = schemas.core_resource_schema(schema_kind, profile)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(document)


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
            controller.normalize_environment_document(json.loads(path.read_text()), path.parent.name)
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
    project_ref_defaults = project["properties"]["spec"]["properties"]["environmentDefaults"]["properties"]["refs"]
    assert project_ref_defaults["minProperties"] == 1
    assert "\\{environment\\}" in project_ref_defaults["properties"]["desired"]["pattern"]
    assert artifact["properties"]["kind"]["const"] == "ContainerImages"
    assert "images" in artifact["required"]
    assert unit["properties"]["kind"]["const"] == "Terraform"
    assert receipt["properties"]["kind"]["const"] == "Receipt"
