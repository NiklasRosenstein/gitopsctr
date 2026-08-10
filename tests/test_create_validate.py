from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from gitopsctr import cli
from gitopsctr.driver import UnitDriver
from gitopsctr.registry import UNIT_DRIVERS


def project_document(
    name: str = "example",
    *,
    write_format: str = "yaml",
    environments_path: str = "deployment/environments",
) -> dict:
    return {
        "$schema": "https://example.invalid/Project.schema.json",
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": name},
        "spec": {"writeFormat": write_format, "environmentsPath": environments_path},
    }


def write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def run_command(root: Path, arguments: list[str]) -> None:
    cli.REPOSITORY_ROOT = root
    args = cli.build_parser().parse_args(arguments)
    args.handler(args)


def create_project(root: Path, **overrides: str) -> None:
    arguments = ["create", "project", "--name", overrides.get("name", "example")]
    if "write_format" in overrides:
        arguments += ["--write-format", overrides["write_format"]]
    if "environments_path" in overrides:
        arguments += ["--environments-path", overrides["environments_path"]]
    if "desired_ref_template" in overrides:
        arguments += ["--desired-ref-template", overrides["desired_ref_template"]]
    if "observed_ref_template" in overrides:
        arguments += ["--observed-ref-template", overrides["observed_ref_template"]]
    if "candidate_ref_template" in overrides:
        arguments += ["--candidate-ref-template", overrides["candidate_ref_template"]]
    run_command(root, arguments)


def create_environment(root: Path, name: str = "dev", change_gate: str = "none") -> None:
    run_command(
        root,
        ["create", "environment", "--name", name, "--change-gate", change_gate],
    )


def test_create_project_writes_a_valid_canonical_resource(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    create_project(tmp_path, name="example.team")

    path = tmp_path / "gitopsctr.yaml"
    text = path.read_text()
    document = yaml.safe_load(text)
    assert document == {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "example.team"},
        "spec": {
            "writeFormat": "yaml",
            "environmentsPath": "deployment/environments",
            "environmentDefaults": {
                "refs": {
                    "desired": "gitopsctr/desired/{environment}",
                    "observed": "gitopsctr/observed/{environment}",
                    "candidate": "gitopsctr/candidates/{environment}/{id}",
                }
            },
        },
    }
    assert text.startswith(
        "# yaml-language-server: "
        "$schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Project.schema.json\n"
    )
    assert capsys.readouterr().out == "gitopsctr.yaml\n"


def test_create_project_validates_before_writing_and_requires_force(tmp_path: Path):
    with pytest.raises(cli.OperationError, match="does not match"):
        create_project(tmp_path, name="Not_Valid")
    assert not (tmp_path / "gitopsctr.yaml").exists()

    create_project(tmp_path, name="first")
    with pytest.raises(cli.OperationError, match="already exists"):
        create_project(tmp_path, name="second")
    run_command(tmp_path, ["create", "project", "--name", "second", "--force"])
    assert yaml.safe_load((tmp_path / "gitopsctr.yaml").read_text())["metadata"]["name"] == "second"

    with pytest.raises(cli.OperationError, match="environmentsPath"):
        run_command(
            tmp_path,
            ["create", "project", "--name", "second", "--environments-path", "../outside", "--force"],
        )
    with pytest.raises(cli.OperationError, match="desired"):
        run_command(
            tmp_path,
            ["create", "project", "--name", "second", "--desired-ref-template", "deploy/main", "--force"],
        )
    with pytest.raises(cli.OperationError, match="candidate"):
        run_command(
            tmp_path,
            [
                "create",
                "project",
                "--name",
                "second",
                "--candidate-ref-template",
                "changes/{id}",
                "--force",
            ],
        )


def test_create_project_accepts_environment_ref_templates(tmp_path: Path):
    create_project(
        tmp_path,
        desired_ref_template="deployments/{environment}",
        observed_ref_template="observations/{environment}",
        candidate_ref_template="changes/{environment}/{operation}/{id}",
    )

    specification = yaml.safe_load((tmp_path / "gitopsctr.yaml").read_text())["spec"]
    assert specification["environmentDefaults"]["refs"] == {
        "desired": "deployments/{environment}",
        "observed": "observations/{environment}",
        "candidate": "changes/{environment}/{operation}/{id}",
    }


def test_project_source_revision_policy_defaults_and_parses(tmp_path: Path):
    write_yaml(tmp_path / "gitopsctr.yaml", project_document())
    policy = cli.load_project_config(tmp_path).source_revision_policy
    assert policy.unavailable_when.value == "outside-candidate-history"
    assert policy.when_unavailable_during_advance.value == "refresh"
    assert policy.when_unavailable_during_plan.value == "error"

    document = project_document()
    document["spec"]["sourceRevisionPolicy"] = {
        "unavailableWhen": "missing",
        "whenUnavailableDuringAdvance": "error",
        "whenUnavailableDuringPlan": "refresh",
    }
    write_yaml(tmp_path / "gitopsctr.yaml", document)
    policy = cli.load_project_config(tmp_path).source_revision_policy
    assert policy.unavailable_when.value == "missing"
    assert policy.when_unavailable_during_advance.value == "error"
    assert policy.when_unavailable_during_plan.value == "refresh"

    document["spec"]["sourceRevisionPolicy"] = {"whenUnavailableDuringPlan": "refresh"}
    write_yaml(tmp_path / "gitopsctr.yaml", document)
    policy = cli.load_project_config(tmp_path).source_revision_policy
    assert policy.unavailable_when.value == "outside-candidate-history"
    assert policy.when_unavailable_during_advance.value == "refresh"
    assert policy.when_unavailable_during_plan.value == "refresh"


def test_create_project_rejects_ambiguous_configuration_even_with_force(tmp_path: Path):
    write_yaml(tmp_path / "gitopsctr.yaml", project_document())
    write_yaml(tmp_path / ".gitopsctr.yml", project_document())
    with pytest.raises(cli.OperationError, match="multiple Project configuration"):
        run_command(tmp_path, ["create", "project", "--name", "replacement", "--force"])


def test_create_environment_uses_the_project_path_format_and_gate(tmp_path: Path):
    create_project(tmp_path, write_format="json", environments_path="config/environments")
    create_environment(tmp_path, change_gate="pullRequest")

    path = tmp_path / "config/environments/dev/environment.json"
    document = json.loads(path.read_text())
    assert document["metadata"]["name"] == "dev"
    assert document["spec"] == {"changeGate": "pullRequest"}
    assert not (tmp_path / "deployment/environments").exists()

    run_command(tmp_path, ["validate", "--environment", "dev"])


def test_create_environment_requires_a_project_and_handles_force_and_duplicates(tmp_path: Path):
    with pytest.raises(cli.OperationError, match="no Project configuration"):
        create_environment(tmp_path)
    create_project(tmp_path)
    create_environment(tmp_path)
    with pytest.raises(cli.OperationError, match="already exists"):
        create_environment(tmp_path)
    run_command(
        tmp_path,
        ["create", "environment", "--name", "dev", "--change-gate", "pullRequest", "--force"],
    )
    environment_root = tmp_path / "deployment/environments/dev"
    assert yaml.safe_load((environment_root / "environment.yaml").read_text())["spec"]["changeGate"] == "pullRequest"
    (environment_root / "environment.json").write_text("{}")
    with pytest.raises(cli.OperationError, match="multiple document formats"):
        run_command(tmp_path, ["create", "environment", "--name", "dev", "--force"])


@pytest.mark.parametrize("driver_name", sorted(UNIT_DRIVERS))
def test_create_unit_generates_a_valid_builtin_scaffold(tmp_path: Path, driver_name: str):
    create_project(tmp_path)
    create_environment(tmp_path)
    run_command(
        tmp_path,
        [
            "create",
            "unit",
            "--environment",
            "dev",
            "--name",
            driver_name,
            "--driver",
            driver_name,
            "--source-path",
            f"services/{driver_name}",
        ],
    )

    path = tmp_path / f"deployment/environments/dev/units/{driver_name}.yaml"
    text = path.read_text()
    document = yaml.safe_load(text)
    unit = cli.parse_authored_unit_document(document, driver_name)
    UNIT_DRIVERS[driver_name].unit_contract.validate(unit.driver.unit_contract.dump(unit.spec))
    assert unit.spec.source.path == f"services/{driver_name}"
    assert not hasattr(unit.spec, "artifacts")
    assert text.startswith(
        f"# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/"
        f"unit.gitopsctr.io/v1/{UNIT_DRIVERS[driver_name].kind}/authored.schema.json\n"
    )


def test_create_unit_rejects_unsafe_source_paths_and_unsupported_scaffolding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    create_project(tmp_path)
    create_environment(tmp_path)
    with pytest.raises(cli.OperationError, match="stay inside"):
        run_command(
            tmp_path,
            [
                "create",
                "unit",
                "--environment",
                "dev",
                "--name",
                "bad",
                "--driver",
                "terraform",
                "--source-path",
                "../bad",
            ],
        )

    unsupported = UnitDriver()
    monkeypatch.setitem(cli.UNIT_DRIVERS, "unsupported", unsupported)
    args = argparse.Namespace(
        environment="dev",
        name="unsupported",
        driver="unsupported",
        source_path=".",
        force=False,
    )
    with pytest.raises(cli.OperationError, match="does not support scaffolding"):
        cli.command_create_unit(args)


def test_force_replaces_one_existing_representation_without_creating_a_duplicate(tmp_path: Path):
    create_project(tmp_path)
    create_environment(tmp_path)
    run_command(
        tmp_path,
        ["create", "unit", "--environment", "dev", "--name", "infra", "--driver", "terraform"],
    )
    with pytest.raises(cli.OperationError, match="already exists"):
        run_command(
            tmp_path,
            ["create", "unit", "--environment", "dev", "--name", "infra", "--driver", "oci-images"],
        )
    run_command(
        tmp_path,
        ["create", "unit", "--environment", "dev", "--name", "infra", "--driver", "oci-images", "--force"],
    )
    units = tmp_path / "deployment/environments/dev/units"
    assert [path.name for path in units.iterdir()] == ["infra.yaml"]
    assert yaml.safe_load((units / "infra.yaml").read_text())["kind"] == "OciImages"


def test_json_unit_contains_the_pinned_schema_property(tmp_path: Path):
    create_project(tmp_path, write_format="json")
    create_environment(tmp_path)
    run_command(
        tmp_path,
        ["create", "unit", "--environment", "dev", "--name", "infra", "--driver", "terraform"],
    )
    document = json.loads((tmp_path / "deployment/environments/dev/units/infra.json").read_text())
    assert document["$schema"].endswith("/Terraform/authored.schema.json")


def test_validate_whole_project_selected_environment_and_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    create_project(tmp_path)
    create_environment(tmp_path)
    run_command(
        tmp_path,
        ["create", "unit", "--environment", "dev", "--name", "infra", "--driver", "terraform"],
    )
    capsys.readouterr()

    run_command(tmp_path, ["validate"])
    assert "VALID" in capsys.readouterr().err
    run_command(tmp_path, ["validate", "--environment", "dev", "--environment", "dev"])
    assert "1 environment" in capsys.readouterr().err
    run_command(
        tmp_path,
        ["validate", "gitopsctr.yaml", "deployment/environments/dev/units/infra.yaml"],
    )
    assert "2 documents" in capsys.readouterr().err


def test_validate_collects_errors_and_can_fail_fast(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    create_project(tmp_path)
    create_environment(tmp_path)
    units = tmp_path / "deployment/environments/dev/units"
    write_yaml(units / "one.yaml", {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Missing"})
    write_yaml(units / "two.yaml", {"apiVersion": "unit.gitopsctr.io/v1", "kind": "AlsoMissing"})
    capsys.readouterr()

    with pytest.raises(cli.OperationError, match="2 errors"):
        run_command(tmp_path, ["validate", "--environment", "dev"])
    output = capsys.readouterr().err
    assert output.count("INVALID") == 2
    with pytest.raises(cli.OperationError, match="one.yaml"):
        run_command(tmp_path, ["validate", "--environment", "dev", "--fail-fast"])


def test_validate_rejects_duplicate_unit_representations(tmp_path: Path):
    create_project(tmp_path)
    create_environment(tmp_path)
    units = tmp_path / "deployment/environments/dev/units"
    document = cli.serialize_unit_document(
        cli.parse_authored_unit_document(
            {"name": "infra", "driver": "terraform", **UNIT_DRIVERS["terraform"].scaffold_unit_spec("infra", ".")},
            "infra",
        ),
        profile="authored",
    )
    write_yaml(units / "infra.yaml", document)
    (units / "infra.json").write_text(json.dumps(document))

    with pytest.raises(cli.OperationError, match="1 error"):
        run_command(tmp_path, ["validate", "--environment", "dev"])
    with pytest.raises(cli.OperationError, match="1 error"):
        run_command(tmp_path, ["validate", "deployment/environments/dev/units/infra.yaml"])


def test_validate_applies_cross_unit_observation_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    create_project(tmp_path)
    create_environment(tmp_path)
    units = tmp_path / "deployment/environments/dev/units"
    producer = {
        "name": "manifests",
        "driver": "kubernetes-manifests",
        **UNIT_DRIVERS["kubernetes-manifests"].scaffold_unit_spec("manifests", "kubernetes"),
    }
    consumer = {
        "name": "consumer",
        "driver": "terraform",
        **UNIT_DRIVERS["terraform"].scaffold_unit_spec("consumer", "terraform"),
        "inputs": {"value": {"fromReceipt": {"unit": "manifests"}}},
    }
    write_yaml(
        units / "manifests.yaml",
        cli.serialize_unit_document(cli.parse_authored_unit_document(producer, "manifests"), profile="authored"),
    )
    write_yaml(
        units / "consumer.yaml",
        cli.serialize_unit_document(cli.parse_authored_unit_document(consumer, "consumer"), profile="authored"),
    )
    capsys.readouterr()

    with pytest.raises(cli.OperationError, match="1 error"):
        run_command(tmp_path, ["validate", "--environment", "dev"])
    assert "cannot observe materialization-only unit" in capsys.readouterr().err


def test_validate_rejects_an_invalid_environment_target_before_path_resolution(tmp_path: Path):
    create_project(tmp_path)
    with pytest.raises(cli.OperationError, match="invalid environment name"):
        run_command(tmp_path, ["validate", "--environment", "../outside", "--fail-fast"])
