from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from gitopsctr import cli, schemas
from gitopsctr.formats import DocumentFormat, load_project_config, write_document


def test_yaml_is_the_default_and_project_config_can_select_json(tmp_path: Path):
    value = {"apiVersion": "gitopsctr.io/v1", "kind": "Environment", "metadata": {"name": "dev"}}
    write_document(tmp_path / "default.yaml", value)
    assert (tmp_path / "default.yaml").is_file()
    (tmp_path / "gitopsctr.yaml").write_text("writeFormat: json\n")
    assert load_project_config(tmp_path).write_format is DocumentFormat.JSON
    write_document(tmp_path / "configured.json", value, format=DocumentFormat.JSON)
    assert cli.load_json(tmp_path / "configured.json") == value


def test_new_yaml_resource_envelopes_are_loaded_and_normalized(tmp_path: Path):
    environment_root = tmp_path / "deployment/environments/dev"
    units_root = environment_root / "units"
    units_root.mkdir(parents=True)
    (tmp_path / "gitopsctr.yaml").write_text("writeFormat: yaml\n")
    (environment_root / "environment.yaml").write_text(
        """apiVersion: gitopsctr.io/v1
kind: Environment
metadata:
  name: dev
spec: {}
"""
    )
    (units_root / "infrastructure.yaml").write_text(
        """apiVersion: unit.gitopsctr.io/v1
kind: Terraform
metadata:
  name: infrastructure
spec:
  source:
    path: infrastructure
  terraform:
    backend:
      key: example/dev.tfstate
    variables:
      environment: dev
    observeOutputs: []
    checks: []
"""
    )

    environment = cli.load_environment(tmp_path, "dev")
    specifications = cli.load_environment_specifications(tmp_path, "dev")

    assert environment["name"] == "dev"
    assert specifications["infrastructure"]["driver"] == "terraform"
    assert specifications["infrastructure"]["name"] == "infrastructure"


def test_yaml_demo_documents_validate_against_published_resource_schemas():
    root = Path(__file__).parents[1]
    documents = schemas.schema_documents()
    by_id = {document["$id"]: document for document in documents.values() if "$id" in document}
    paths = [
        root / "demo/repository/deployment/environments/dev/environment.yaml",
        root / "demo/repository/deployment/environments/dev/units/demo-image.yaml",
        root / "demo/repository/deployment/environments/dev/units/demo-service.yaml",
        root / "demo/kubernetes/repository/deployment/environments/dev/environment.yaml",
        root / "demo/kubernetes/repository/deployment/environments/dev/units/web.yaml",
    ]
    for path in paths:
        document = yaml.safe_load(path.read_text())
        Draft202012Validator(by_id[document["$schema"]]).validate(document)


def test_migration_script_converts_a_source_branch_in_one_forward_commit(tmp_path: Path):
    environment_root = tmp_path / "deployment/environments/dev"
    units_root = environment_root / "units"
    units_root.mkdir(parents=True)
    (environment_root / "environment.json").write_text(json.dumps({"schema": 1, "name": "dev"}))
    (units_root / "infrastructure.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "name": "infrastructure",
                "driver": "terraform",
                "source": {"path": "infrastructure"},
                "terraform": {
                    "backend": {"key": "example/dev.tfstate"},
                    "variables": {"environment": "dev"},
                    "observeOutputs": [],
                    "checks": [],
                },
            }
        )
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=tmp_path, check=True, capture_output=True)

    script = Path(__file__).parents[1] / "tools/migrate_documents.py"
    subprocess.run([sys.executable, str(script), "--apply"], cwd=tmp_path, check=True, capture_output=True)

    def show(path: str) -> str:
        return subprocess.run(["git", "show", f"HEAD:{path}"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    assert show("gitopsctr.yaml") == "writeFormat: yaml\n"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "show", "HEAD:deployment/environments/dev/environment.json"], cwd=tmp_path, check=True)
    assert "apiVersion: gitopsctr.io/v1" in show("deployment/environments/dev/environment.yaml")
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "show", "HEAD:deployment/environments/dev/units/infrastructure.json"], cwd=tmp_path, check=True)
    migrated = yaml.safe_load(show("deployment/environments/dev/units/infrastructure.yaml"))
    assert migrated["apiVersion"] == "unit.gitopsctr.io/v1"
    assert migrated["kind"] == "Terraform"
