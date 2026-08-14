import json
from pathlib import Path

import pytest

from gitopsctr import controller as controller_module

FIXTURE_REPOSITORY = Path(__file__).parent / "fixtures/repository"


def write_test_document(path: Path, value: object) -> None:
    """Write concise test data, enveloping authored source resources when needed."""

    if not isinstance(value, dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return
    parts = path.parts
    deployment_index = next(
        (index for index in range(len(parts) - 1) if parts[index : index + 2] == ("deployment", "environments")),
        None,
    )
    if deployment_index is not None:
        project_root = Path(*parts[:deployment_index])
        project_path = project_root / "gitopsctr.yaml"
        if not any((project_root / name).is_file() for name in controller_module.PROJECT_CONFIG_NAMES):
            project_root.mkdir(parents=True, exist_ok=True)
            project_path.write_text(
                json.dumps(
                    {
                        "apiVersion": "gitopsctr.io/v1",
                        "kind": "Project",
                        "metadata": {"name": "test-project"},
                        "spec": {"effectLease": None},
                    }
                )
            )
        if path.stem == "environment":
            if value.get("apiVersion") is None:
                specification = {key: item for key, item in value.items() if key not in {"$schema", "schema", "name"}}
                value = {
                    "apiVersion": "gitopsctr.io/v1",
                    "kind": "Environment",
                    "metadata": {"name": value.get("name", path.parent.name)},
                    "spec": specification,
                }
        elif path.parent.name == "units" and value.get("apiVersion") is None:
            driver_name = value.get("driver")
            plugin = controller_module.UNIT_DRIVERS.get(driver_name) if isinstance(driver_name, str) else None
            if plugin is not None:
                value = {
                    "apiVersion": plugin.api_version,
                    "kind": plugin.kind,
                    "metadata": {"name": value.get("name", path.stem)},
                    "spec": {
                        key: item for key, item in value.items() if key not in {"$schema", "schema", "name", "driver"}
                    },
                }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def receipt_document(
    driver: str,
    unit: str,
    desired: dict[str, object],
    result: dict[str, object] | None = None,
    *,
    resolved_inputs: dict[str, object] | None = None,
    controller: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
) -> dict[str, object]:
    plugin = controller_module.UNIT_DRIVERS[driver]
    if result is None and driver == "terraform":
        result = {"applied": {"sourceRevision": "0" * 40}, "outputs": {}}
    typed_result = plugin.result_contract.parse(result or {})
    document: dict[str, object] = {
        "$schema": controller_module.resource_schema_url(plugin.api_version, plugin.kind, "receipt"),
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Receipt",
        "metadata": {"name": unit},
        "spec": {
            "subject": {"apiVersion": plugin.api_version, "kind": plugin.kind, "name": unit},
            "desired": desired,
        },
        "status": {
            "controller": controller or {},
            "result": plugin.result_contract.dump(typed_result),
        },
    }
    if resolved_inputs is not None:
        document["spec"]["resolvedInputs"] = resolved_inputs  # type: ignore[index]
    if artifacts is not None:
        document["status"]["artifacts"] = artifacts  # type: ignore[index]
    return document


def receipt_resource(
    driver: str,
    unit: str,
    desired: dict[str, object],
    result: dict[str, object] | None = None,
    *,
    resolved_inputs: dict[str, object] | None = None,
    controller: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
):
    if artifacts is None:
        plugin = controller_module.UNIT_DRIVERS[driver]
        if plugin.artifact_outputs:
            artifacts = {
                name: {
                    "apiVersion": artifact_kind.gvk.api_version,
                    "kind": artifact_kind.gvk.kind,
                    "path": f"artifacts/{unit}/{name}.json",
                    "digest": "sha256:" + "0" * 64,
                    "mediaType": f"{controller_module.require_artifact_api(artifact_kind).media_type}+json",
                }
                for name, artifact_kind in plugin.artifact_outputs.items()
            }
    return controller_module.RESOURCE_CATALOG.parse_receipt(
        receipt_document(
            driver,
            unit,
            desired,
            result,
            resolved_inputs=resolved_inputs,
            controller=controller,
            artifacts=artifacts,
        ),
        unit,
    )


@pytest.fixture(autouse=True)
def repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep controller tests independent from the gitopsctr source checkout."""
    monkeypatch.setattr(controller_module, "REPOSITORY_ROOT", FIXTURE_REPOSITORY)
