from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests import test_inventory as inventory_support

pytest_plugins = ("tests.test_inventory",)


def run_get(repository: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> str:
    args = controller.build_parser().parse_args(["get", *arguments])
    controller.inspect_resources(repository, args)
    return capsys.readouterr().out


def test_get_environments_and_units_vertical_slice(repository: Path, capsys: pytest.CaptureFixture[str]):
    environments = run_get(repository, capsys, "environments")
    assert environments.splitlines()[0].split() == ["NAME", "DESIRED", "OBSERVED", "RECONCILIATION"]
    assert "dev" in environments and "staging" in environments
    assert "clean=1" in environments
    assert "wait=1" in environments

    units = run_get(repository, capsys, "units", "--environment", "dev")
    assert units.splitlines()[0].split() == ["NAME", "KIND", "DESIRED", "OBSERVATION", "RECONCILIATION", "REASON"]
    assert "application" in units and "CURRENT" in units and "CLEAN" in units
    assert "external" in units and "N/A" in units and "MATERIALIZED" in units


def test_get_named_raw_document_and_multi_result_envelope(repository: Path, capsys: pytest.CaptureFixture[str]):
    raw = json.loads(run_get(repository, capsys, "unit", "application", "--environment", "dev", "-o", "json"))
    assert raw["apiVersion"] == "unit.gitopsctr.io/v1"
    assert raw["kind"] == "Terraform"
    assert raw["metadata"]["name"] == "application"
    assert "provenance" not in raw

    result = json.loads(run_get(repository, capsys, "unit", "application", "-A", "-o", "json"))
    assert result["apiVersion"] == "inspection.gitopsctr.io/v1"
    assert result["kind"] == "ResourceList"
    assert [item["provenance"]["environment"] for item in result["items"]] == ["dev", "staging"]
    assert all(item["document"]["metadata"]["name"] == "application" for item in result["items"])


def test_get_named_raw_document_does_not_evaluate_unrelated_resources(
    repository: Path, capsys: pytest.CaptureFixture[str]
):
    inventory_support.git(repository, "checkout", "desired")
    inventory_support.write_json(
        repository / "units/unrelated.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "unrelated"},
            "spec": {},
        },
    )
    revision = inventory_support.commit(repository, "malformed unrelated desired Unit")
    inventory_support.git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/raw-scope")
    inventory_support.git(repository, "checkout", "main")

    raw = json.loads(
        run_get(
            repository,
            capsys,
            "unit",
            "application",
            "--environment",
            "dev",
            "--desired-ref",
            "gitopsctr/desired/raw-scope",
            "-o",
            "json",
        )
    )
    assert raw["metadata"]["name"] == "application"


def test_get_named_all_environments_tolerates_uninitialized_refs(repository: Path, capsys: pytest.CaptureFixture[str]):
    inventory_support.git(repository, "push", "origin", "--delete", "gitopsctr/desired/staging")
    result = json.loads(run_get(repository, capsys, "unit", "application", "-A", "-o", "json"))
    assert result["metadata"]["name"] == "application"


def test_get_validates_scope_overrides_and_named_misses(repository: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(OperationError, match="requires --environment"):
        run_get(repository, capsys, "units")
    with pytest.raises(OperationError, match="cannot be combined"):
        run_get(repository, capsys, "units", "-A", "--desired-revision", "a" * 40)
    with pytest.raises(OperationError, match="no unit named 'missing'"):
        run_get(repository, capsys, "unit", "missing", "--environment", "dev")


@pytest.mark.parametrize(
    ("arguments", "headers", "name"),
    [
        (("environment", "dev"), ("NAME", "DESIRED", "OBSERVED", "RECONCILIATION"), "dev"),
        (("stacks", "--environment", "staging"), ("NAME", "TEMPLATE", "PARTITION", "UNITS", "STATE"), "web"),
        (("stacktemplates", "--environment", "staging"), ("NAME", "CONTENT-DIGEST", "PARAMETERS", "UNITS"), "web"),
        (
            ("promotions", "--environment", "staging"),
            ("NAME", "SOURCE", "DESIRED-REVISION", "OBSERVED-REVISION", "SPECIFICATION-REVISION"),
            "dev",
        ),
        (("receipts", "--environment", "dev"), ("NAME", "KIND", "OBSERVATION", "ARTIFACTS"), "application"),
    ],
)
def test_get_all_initial_inspection_tables(
    repository: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    headers: tuple[str, ...],
    name: str,
):
    output = run_get(repository, capsys, *arguments)
    assert tuple(output.splitlines()[0].split()) == headers
    assert name in output
    if arguments[0] == "stacks":
        assert output.splitlines()[1].split()[3] == "1"


def test_get_unit_desired_column_is_the_resource_blob(repository: Path, capsys: pytest.CaptureFixture[str]):
    output = run_get(repository, capsys, "unit", "application", "--environment", "dev")
    desired = output.splitlines()[1].split()[2]
    assert len(desired) == 12
    assert all(character in "0123456789abcdef" for character in desired)


def test_get_all_environments_always_includes_environment_column(repository: Path, capsys: pytest.CaptureFixture[str]):
    output = run_get(repository, capsys, "unit", "external", "-A")
    assert output.splitlines()[0].split()[0] == "ENVIRONMENT"
    assert [line.split()[0] for line in output.splitlines()[1:]] == ["dev", "staging"]


def test_get_raw_empty_collection_is_a_versioned_empty_list(repository: Path, capsys: pytest.CaptureFixture[str]):
    inventory_support.git(repository, "checkout", "observed")
    (repository / "units/application.yaml").unlink()
    empty_revision = inventory_support.commit(repository, "empty observed snapshot")
    inventory_support.git(
        repository,
        "push",
        "origin",
        f"{empty_revision}:refs/heads/gitopsctr/observed/empty",
    )
    inventory_support.git(repository, "checkout", "main")
    result = json.loads(
        run_get(
            repository,
            capsys,
            "receipts",
            "--environment",
            "dev",
            "--observed-ref",
            "gitopsctr/observed/empty",
            "-o",
            "json",
        )
    )
    assert result == {
        "apiVersion": "inspection.gitopsctr.io/v1",
        "kind": "ResourceList",
        "metadata": {},
        "items": [],
    }


def test_get_explicit_missing_ref_fails_precisely(repository: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(OperationError, match="observed ref 'gitopsctr/observed/missing' does not exist"):
        run_get(
            repository,
            capsys,
            "receipts",
            "--environment",
            "dev",
            "--observed-ref",
            "gitopsctr/observed/missing",
            "-o",
            "json",
        )


def test_documented_get_commands_execute_across_dev_and_staging(repository: Path, capsys: pytest.CaptureFixture[str]):
    commands = (
        ("environments",),
        ("environment", "dev"),
        ("units", "--environment", "dev"),
        ("unit", "application", "--environment", "dev"),
        ("units", "-A"),
        ("unit", "application", "-A"),
        ("stacks", "--environment", "staging"),
        ("stack", "web", "--environment", "staging"),
        ("stacktemplates", "--environment", "staging"),
        ("stacktemplate", "web", "--environment", "staging"),
        ("promotions", "--environment", "staging"),
        ("promotion", "dev", "--environment", "staging"),
        ("receipts", "--environment", "dev"),
        ("receipt", "application", "--environment", "dev"),
    )

    for command in commands:
        assert run_get(repository, capsys, *command).strip()


def test_retired_inspection_commands_have_no_parser_aliases():
    parser = controller.build_parser()
    for arguments in (
        ("list", "environments"),
        ("list", "units", "--environment", "dev"),
        ("show", "desired", "--environment", "dev", "application"),
        ("show", "desired-unit", "--environment", "dev", "application"),
        ("show", "receipt", "--environment", "dev", "application"),
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(arguments)


def test_get_receipt_artifact_returns_validated_persisted_resource(
    repository: Path, capsys: pytest.CaptureFixture[str]
):
    inventory_support.git(repository, "checkout", "desired")
    inventory_support.write_json(
        repository / "units/images.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "metadata": {
                "name": "images",
                "uid": "uid-images",
                "labels": {"gitopsctr.io/partition": "application"},
            },
            "spec": {
                "source": {
                    "path": ".",
                    "revision": "a" * 40,
                    "driverVersion": 1,
                    "inputHash": "sha256:inputs",
                }
            },
        },
    )
    desired_revision = inventory_support.commit(repository, "desired artifact producer")
    inventory_support.git(repository, "push", "origin", f"{desired_revision}:refs/heads/gitopsctr/desired/dev")
    desired_blob = inventory_support.git(repository, "rev-parse", f"{desired_revision}:units/images.yaml")

    inventory_support.git(repository, "checkout", "observed")
    artifact_path = repository / "artifacts/images/containers.yaml"
    artifact = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": "images",
            "driverVersion": 1,
            "sourceRevision": "a" * 40,
            "inputHashVersion": 1,
            "inputHash": "sha256:inputs",
        },
        "images": {},
    }
    inventory_support.write_json(artifact_path, artifact)
    digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    inventory_support.write_json(
        repository / "units/images.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Receipt",
            "metadata": {"name": "images"},
            "spec": {
                "subject": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "OciImages", "name": "images"},
                "desired": {"unitBlob": desired_blob},
            },
            "status": {
                "controller": {},
                "result": {},
                "artifacts": {
                    "containers": {
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "path": "artifacts/images/containers.yaml",
                        "digest": digest,
                        "mediaType": "application/vnd.gitopsctr.container-images.v1+yaml",
                    }
                },
            },
        },
    )
    observed_revision = inventory_support.commit(repository, "observed artifact")
    inventory_support.git(repository, "push", "origin", f"{observed_revision}:refs/heads/gitopsctr/observed/dev")
    inventory_support.git(repository, "checkout", "main")

    output = run_get(
        repository,
        capsys,
        "receipt",
        "images",
        "--environment",
        "dev",
        "--artifact",
        "containers",
        "-o",
        "json",
    )
    assert json.loads(output) == artifact

    all_output = run_get(
        repository,
        capsys,
        "receipt",
        "images",
        "--environment",
        "dev",
        "--artifacts",
        "-o",
        "json",
    )
    assert json.loads(all_output) == artifact

    with pytest.raises(OperationError, match="has no artifact named 'missing'"):
        run_get(
            repository,
            capsys,
            "receipt",
            "images",
            "--environment",
            "dev",
            "--artifact",
            "missing",
        )
