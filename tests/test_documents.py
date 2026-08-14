from __future__ import annotations

import json
import re
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
        root / "demo/k8s/repository/gitopsctr.yaml",
        root / "demo/docker/repository/deployment/environments/dev/environment.yaml",
        root / "demo/docker/repository/deployment/environments/dev/stacks/application.yaml",
        root / "demo/docker/repository/deployment/stack-templates/application.yaml",
        root / "demo/k8s/repository/deployment/environments/dev/environment.yaml",
        root / "demo/k8s/repository/deployment/environments/dev/stacks/application.yaml",
        root / "demo/k8s/repository/deployment/environments/staging/environment.yaml",
        root / "demo/k8s/repository/deployment/environments/staging/stacks/application.yaml",
        root / "demo/k8s/repository/deployment/environments/preview/environment.yaml",
        root / "demo/k8s/repository/deployment/stack-templates/application.yaml",
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


def test_core_resource_normalizers_reject_flat_documents():
    with pytest.raises(controller.OperationError, match="apiVersion gitopsctr.io/v1 and kind Environment"):
        controller.normalize_environment_document({"name": "dev"}, "dev")
    with pytest.raises(controller.OperationError, match="apiVersion gitopsctr.io/v1 and kind Promotion"):
        controller.normalize_promotion_document(
            {
                "source": {
                    "environment": "dev",
                    "desiredRef": "desired/dev",
                    "desiredRevision": "a" * 40,
                    "observedRef": "observed/dev",
                    "observedRevision": None,
                },
                "specificationRevision": "b" * 40,
            }
        )
    with pytest.raises(controller.OperationError, match="apiVersion gitopsctr.io/v1 and kind Receipt"):
        controller.RESOURCE_CATALOG.parse_receipt({"name": "application"})
