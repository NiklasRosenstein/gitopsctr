from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from gitopsctr import controller, schemas
from gitopsctr.formats import DocumentFormat, DocumentFormatError, load_project_config, write_document


def project_document(*, name: str = "test-project", spec: str = "{}") -> str:
    specification = yaml.safe_load(spec) or {}
    if isinstance(specification, dict):
        specification.setdefault("effectLease", None)
    return yaml.safe_dump(
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": name},
            "spec": specification,
        },
        sort_keys=False,
    )


def test_yaml_is_the_default_and_project_config_can_select_json(tmp_path: Path):
    value = {"apiVersion": "gitopsctr.io/v1", "kind": "Environment", "metadata": {"name": "dev"}}
    write_document(tmp_path / "default.yaml", value)
    assert (tmp_path / "default.yaml").is_file()
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec="{writeFormat: json}"))
    config = load_project_config(tmp_path)
    assert config.name == "test-project"
    assert config.write_format is DocumentFormat.JSON
    assert config.environments_path.as_posix() == "deployment/environments"
    assert config.environment_defaults.refs.desired == "gitopsctr/desired/{environment}"
    assert config.environment_defaults.refs.observed == "gitopsctr/observed/{environment}"
    assert config.environment_defaults.refs.candidate == "gitopsctr/candidates/{environment}/{id}"
    write_document(tmp_path / "configured.json", value, format=DocumentFormat.JSON)
    assert controller.load_json(tmp_path / "configured.json") == value


def test_yaml_uses_language_server_schema_directive_while_json_keeps_schema_property(tmp_path: Path):
    schema = "https://example.invalid/resource.schema.json"
    value = {"$schema": schema, "name": "example"}

    yaml_path = write_document(tmp_path / "resource.yaml", value)
    json_path = write_document(tmp_path / "resource.json", value)

    assert yaml_path.read_text() == (f"# yaml-language-server: $schema={schema}\nname: example\n")
    assert yaml.safe_load(yaml_path.read_text()) == {"name": "example"}
    assert json.loads(json_path.read_text()) == value
    assert value == {"$schema": schema, "name": "example"}


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("writeFormat: yaml\n", "apiVersion"),
        (project_document(spec="{writeFormat: toml}"), "toml"),
        (project_document(spec="{unknown: true}"), "Additional properties"),
        (project_document(name="Not_A_DNS_Name"), "does not match"),
        (project_document(name="invalid..name"), "does not match"),
        (project_document().replace("kind: Project", "kind: Configuration"), "Project"),
    ],
)
def test_project_config_rejects_values_outside_its_published_schema(tmp_path: Path, contents: str, message: str):
    (tmp_path / "gitopsctr.yaml").write_text(contents)
    with pytest.raises(DocumentFormatError, match=message):
        load_project_config(tmp_path)


@pytest.mark.parametrize(
    "template",
    [
        "deploy/main",
        "deploy/{env}",
        "deploy/{environment",
        "deploy/{environment}/{other}",
    ],
)
def test_project_environment_ref_templates_require_only_the_environment_placeholder(tmp_path: Path, template: str):
    specification = {
        "environmentDefaults": {"refs": {"desired": template}},
    }
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec=json.dumps(specification)))

    with pytest.raises(DocumentFormatError, match="environmentDefaults.refs.desired"):
        load_project_config(tmp_path)


def test_project_environment_ref_templates_can_be_configured_independently(tmp_path: Path):
    specification = {
        "environmentDefaults": {"refs": {"desired": "deployments/{environment}/{environment}"}},
    }
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec=json.dumps(specification)))

    config = load_project_config(tmp_path)

    assert config.environment_defaults.refs.desired == "deployments/{environment}/{environment}"
    assert config.environment_defaults.refs.observed == "gitopsctr/observed/{environment}"
    assert config.environment_defaults.refs.candidate == "gitopsctr/candidates/{environment}/{id}"


@pytest.mark.parametrize(
    "template",
    [
        "gitopsctr/candidates/static",
        "gitopsctr/candidates/{id}",
        "gitopsctr/candidates/{environment}/{unknown}",
        "gitopsctr/candidates/{environment",
    ],
)
def test_project_candidate_ref_template_requires_environment_and_known_placeholders(tmp_path: Path, template: str):
    specification = {"environmentDefaults": {"refs": {"candidate": template}}}
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec=json.dumps(specification)))

    with pytest.raises(DocumentFormatError, match="environmentDefaults.refs.candidate"):
        load_project_config(tmp_path)


@pytest.mark.parametrize(
    "template",
    [
        "gitopsctr/candidates/{environment}",
        "gitopsctr/candidates/{environment}/{id}",
        "gitopsctr/candidates/{environment}/{operation}",
        "gitopsctr/candidates/{environment}/{operation}/{id}",
    ],
)
def test_project_candidate_ref_template_accepts_supported_forms(tmp_path: Path, template: str):
    specification = {"environmentDefaults": {"refs": {"candidate": template}}}
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec=json.dumps(specification)))

    assert load_project_config(tmp_path).environment_defaults.refs.candidate == template


def test_project_resource_schema_is_published_and_deterministic():
    schema = schemas.project_resource_schema()
    Draft202012Validator.check_schema(schema)
    project = yaml.safe_load(project_document(spec="{writeFormat: yaml, environmentsPath: config/environments}"))
    project["$schema"] = "https://example.invalid/project.json"
    Draft202012Validator(schema).validate(project)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({**project, "other": True})


def test_project_configuration_is_required_and_unambiguous(tmp_path: Path):
    with pytest.raises(DocumentFormatError, match="no Project configuration"):
        load_project_config(tmp_path)
    (tmp_path / "gitopsctr.yaml").write_text(project_document())
    (tmp_path / ".gitopsctr.yml").write_text(project_document())
    with pytest.raises(DocumentFormatError, match="multiple Project configuration"):
        load_project_config(tmp_path)


@pytest.mark.parametrize("filename", ["gitopsctr.yaml", "gitopsctr.yml", ".gitopsctr.yaml", ".gitopsctr.yml"])
def test_project_configuration_accepts_each_supported_filename(tmp_path: Path, filename: str):
    (tmp_path / filename).write_text(project_document())
    assert load_project_config(tmp_path).name == "test-project"


@pytest.mark.parametrize("environments_path", ["", "/environments", "../environments", "config/../environments"])
def test_project_rejects_unsafe_environment_paths(tmp_path: Path, environments_path: str):
    (tmp_path / "gitopsctr.yaml").write_text(
        project_document(spec=f"{{environmentsPath: {json.dumps(environments_path)}}}")
    )
    with pytest.raises(DocumentFormatError, match="environmentsPath"):
        load_project_config(tmp_path)


def test_project_configures_the_authored_environment_root(tmp_path: Path):
    environment_root = tmp_path / "config/environments/dev"
    units_root = environment_root / "units"
    units_root.mkdir(parents=True)
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec="{environmentsPath: config/environments}"))
    (environment_root / "environment.yaml").write_text(
        "apiVersion: gitopsctr.io/v1\nkind: Environment\nmetadata: {name: dev}\nspec: {}\n"
    )
    (units_root / "infrastructure.yaml").write_text(
        """apiVersion: unit.gitopsctr.io/v1
kind: Terraform
metadata: {name: infrastructure}
spec:
  source: {path: infrastructure}
  terraform:
    backend: {key: example/dev.tfstate}
    variables: {environment: dev}
    observeOutputs: []
    checks: []
"""
    )
    assert controller.load_environment(tmp_path, "dev")["name"] == "dev"
    assert list(controller.load_environment_specifications(tmp_path, "dev")) == ["infrastructure"]


def test_new_yaml_resource_envelopes_are_loaded_as_typed_resources(tmp_path: Path):
    environment_root = tmp_path / "deployment/environments/dev"
    units_root = environment_root / "units"
    units_root.mkdir(parents=True)
    (tmp_path / "gitopsctr.yaml").write_text(project_document(spec="{writeFormat: yaml}"))
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

    environment = controller.load_environment(tmp_path, "dev")
    specifications = controller.load_environment_specifications(tmp_path, "dev")

    assert environment["name"] == "dev"
    assert specifications["infrastructure"].driver_name == "terraform"
    assert specifications["infrastructure"].name == "infrastructure"
    assert specifications["infrastructure"].spec.source.path == "infrastructure"


def test_yaml_demo_documents_validate_against_published_resource_schemas():
    root = Path(__file__).parents[1]
    documents = schemas.schema_documents()
    by_id = {document["$id"]: document for document in documents.values() if "$id" in document}
    paths = [
        root / "gitopsctr.yaml",
        root / "demo/docker/repository/gitopsctr.yaml",
        root / "demo/kubernetes/repository/gitopsctr.yaml",
        root / "demo/docker/repository/deployment/environments/dev/environment.yaml",
        root / "demo/docker/repository/deployment/environments/dev/units/demo-image.yaml",
        root / "demo/docker/repository/deployment/environments/dev/units/demo-service.yaml",
        root / "demo/kubernetes/repository/deployment/environments/dev/environment.yaml",
        root / "demo/kubernetes/repository/deployment/environments/dev/units/demo-image.yaml",
        root / "demo/kubernetes/repository/deployment/environments/dev/units/web.yaml",
    ]
    for path in paths:
        text = path.read_text()
        match = re.match(r"# yaml-language-server: \$schema=(\S+)\n", text)
        assert match, path
        document = yaml.safe_load(text)
        Draft202012Validator(by_id[match.group(1)]).validate(document)


def test_receipt_result_cannot_override_envelope_identity():
    with pytest.raises(controller.OperationError, match="Additional properties"):
        controller.RESOURCE_CATALOG.parse_receipt(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Receipt",
                "metadata": {"name": "terraform"},
                "spec": {
                    "subject": {
                        "apiVersion": "unit.gitopsctr.io/v1",
                        "kind": "Terraform",
                        "name": "terraform",
                    },
                    "desired": {"unitBlob": "f" * 40},
                },
                "status": {"controller": {}, "result": {"driver": "oci-images"}},
            }
        )


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
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=tmp_path, check=True, capture_output=True)

    script = Path(__file__).parents[1] / "tools/migrate_documents.py"
    subprocess.run(
        [sys.executable, str(script), "--project-name", "test-project", "--apply"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    def show(path: str) -> str:
        return subprocess.run(
            ["git", "show", f"HEAD:{path}"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    project = yaml.safe_load(show("gitopsctr.yaml"))
    assert project["apiVersion"] == "gitopsctr.io/v1"
    assert project["kind"] == "Project"
    assert project["metadata"]["name"] == "test-project"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "show", "HEAD:deployment/environments/dev/environment.json"], cwd=tmp_path, check=True)
    assert "apiVersion: gitopsctr.io/v1" in show("deployment/environments/dev/environment.yaml")
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["git", "show", "HEAD:deployment/environments/dev/units/infrastructure.json"], cwd=tmp_path, check=True
        )
    migrated = yaml.safe_load(show("deployment/environments/dev/units/infrastructure.yaml"))
    assert migrated["apiVersion"] == "unit.gitopsctr.io/v1"
    assert migrated["kind"] == "Terraform"
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout
        == ""
    )
    assert not (environment_root / "environment.json").exists()
    assert (environment_root / "environment.yaml").exists()
    assert not (units_root / "infrastructure.json").exists()
    assert (units_root / "infrastructure.yaml").exists()


def test_migration_script_canonicalizes_legacy_desired_units_and_uses_configured_refs(tmp_path: Path):
    project = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "test-project"},
        "spec": {
            "writeFormat": "yaml",
            "environmentsPath": "config/environments",
            "environmentDefaults": {
                "refs": {
                    "desired": "release/{environment}",
                    "observed": "state/{environment}",
                    "candidate": "changes/{environment}/{id}",
                }
            },
            "effectLease": None,
        },
    }
    environment_root = tmp_path / "config/environments/dev"
    units_root = environment_root / "units"
    units_root.mkdir(parents=True)
    (tmp_path / "gitopsctr.yaml").write_text(yaml.safe_dump(project, sort_keys=False))
    (environment_root / "environment.json").write_text(json.dumps({"schema": 1, "name": "dev"}))
    (units_root / "application.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "name": "application",
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
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "legacy source"], cwd=tmp_path, check=True, capture_output=True)

    subprocess.run(["git", "branch", "release/dev"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "release/dev"], cwd=tmp_path, check=True, capture_output=True)
    desired_units = tmp_path / "units"
    desired_units.mkdir()
    desired_units.joinpath("application.json").write_text(
        json.dumps(
            {
                "name": "application",
                "driver": "terraform",
                "source": {
                    "path": "infrastructure",
                    "revision": "a" * 40,
                    "inputHash": "sha256:inputs",
                    "driverVersion": controller.DRIVER_VERSIONS["terraform"],
                },
                "terraform": {
                    "backend": {"key": "example/dev.tfstate"},
                    "variables": {"environment": "dev"},
                    "observeOutputs": [],
                    "checks": [],
                },
            }
        )
    )
    subprocess.run(["git", "add", "units/application.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "legacy desired"], cwd=tmp_path, check=True, capture_output=True)
    old_desired = subprocess.run(
        ["git", "rev-parse", "release/dev"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    script = Path(__file__).parents[1] / "tools/migrate_documents.py"
    subprocess.run(
        [sys.executable, str(script), "--project-name", "test-project", "--apply"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    new_desired = subprocess.run(
        ["git", "rev-parse", "release/dev"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    source_revision = subprocess.run(
        ["git", "rev-parse", "main"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert new_desired != old_desired
    migrated = yaml.safe_load(
        subprocess.run(
            ["git", "show", "release/dev:units/application.yaml"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert migrated["apiVersion"] == "unit.gitopsctr.io/v1"
    assert migrated["kind"] == "Terraform"
    assert migrated["metadata"]["uid"].startswith("d1-")
    assert migrated["metadata"]["lifecycle"] == {"management": {"mode": "sourceTracked"}}
    assert migrated["spec"]["source"]["revision"] == source_revision
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "show", "release/dev:units/application.json"], cwd=tmp_path, check=True)


def test_migration_script_rejects_stale_local_refs_before_applying(tmp_path: Path):
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repository, check=True)
    (repository / "deployment/environments/dev/units").mkdir(parents=True)
    (repository / "deployment/environments/dev/environment.json").write_text(json.dumps({"schema": 1, "name": "dev"}))
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "branch", "deploy/dev"], cwd=repository, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
    subprocess.run(
        ["git", "push", "-u", "origin", "main", "deploy/dev"], cwd=repository, check=True, capture_output=True
    )

    deploy_revision = subprocess.run(
        ["git", "rev-parse", "deploy/dev"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", f"{deploy_revision}^{{tree}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_revision = subprocess.run(
        ["git", "commit-tree", tree, "-p", deploy_revision, "-m", "remote deployment"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "origin", f"{remote_revision}:refs/heads/deploy/dev"],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    script = Path(__file__).parents[1] / "tools/migrate_documents.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-name", "test-project", "--apply", "--push"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "deploy/dev (remote-only commits: 1, local-only commits: 0)" in result.stderr
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        == original_head
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout
        == ""
    )
