from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.inventory import InventoryRecord, InventorySession
from gitopsctr.resource_model import StackInspectionSummary
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


def test_get_all_renders_registry_defined_environment_tables(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = run_get(repository, capsys, "all", "--environment", "dev")
    lines = output.splitlines()

    assert lines[0] == "UNITS"
    assert lines[1].split() == ["NAME", "KIND", "DESIRED", "OBSERVATION", "RECONCILIATION", "REASON"]
    assert "application" in output and "external" in output
    assert "\n\nRECEIPTS\n" in output
    assert "CURRENT" in output
    assert "ENVIRONMENTS" not in output


def test_get_all_raw_output_is_always_one_provenance_list(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = json.loads(run_get(repository, capsys, "all", "--environment", "dev", "-o", "json"))

    assert result["apiVersion"] == "inspection.gitopsctr.io/v1"
    assert result["kind"] == "ResourceList"
    assert {item["document"]["kind"] for item in result["items"]} >= {"Terraform", "Receipt"}
    assert {item["provenance"]["environment"] for item in result["items"]} == {"dev"}
    assert {item["provenance"]["plane"] for item in result["items"]} == {"desired", "observed"}


def test_get_all_across_environments_keeps_family_tables_and_namespace_columns(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = run_get(repository, capsys, "all", "-A")
    lines = output.splitlines()

    section_indexes = [index for index, line in enumerate(lines) if line in {"UNITS", "STACKS", "STACKTEMPLATES"}]
    assert section_indexes
    for index in section_indexes:
        assert lines[index + 1].split()[0] == "ENVIRONMENT"
    assert "dev" in output and "staging" in output


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


def test_get_named_stack_table_does_not_validate_unrelated_stack(repository: Path, capsys: pytest.CaptureFixture[str]):
    inventory_support.git(repository, "checkout", "desired")
    inventory_support.write_json(
        repository / "stacks/unrelated.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "unrelated", "uid": "uid-unrelated"},
            "spec": {},
        },
    )
    revision = inventory_support.commit(repository, "malformed unrelated desired Stack")
    inventory_support.git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/local-stack")
    inventory_support.git(repository, "checkout", "main")

    output = run_get(
        repository,
        capsys,
        "stack",
        "web",
        "--environment",
        "staging",
        "--desired-ref",
        "gitopsctr/desired/local-stack",
    )
    assert "web" in output
    assert "unrelated" not in output


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
    with pytest.raises(OperationError, match="get all requires"):
        run_get(repository, capsys, "all")
    with pytest.raises(OperationError, match="does not accept a resource name"):
        run_get(repository, capsys, "all", "application", "--environment", "dev")
    with pytest.raises(OperationError, match="does not accept --artifact"):
        run_get(repository, capsys, "all", "--environment", "dev", "--artifacts")
    with pytest.raises(OperationError, match="cannot be combined"):
        run_get(repository, capsys, "all", "-A", "--observed-revision", "a" * 40)


@pytest.mark.parametrize(
    ("arguments", "headers", "name"),
    [
        (("environment", "dev"), ("NAME", "DESIRED", "OBSERVED", "RECONCILIATION"), "dev"),
        (
            ("stacks", "--environment", "staging"),
            (
                "NAME",
                "UID",
                "TEMPLATE",
                "TEMPLATE-UID",
                "TEMPLATE-DIGEST",
                "PARTITION",
                "STRUCTURAL",
                "ACTIVE",
                "TOPOLOGY",
                "OBSERVATION",
                "STATE",
            ),
            "web",
        ),
        (
            ("stacktemplates", "--environment", "staging"),
            (
                "NAME",
                "UID",
                "CONTENT-DIGEST",
                "ACQUISITION",
                "SOURCE",
                "PARAMETERS",
                "UNITS",
                "PARTITION",
                "REFERENCES",
                "STATE",
            ),
            "web",
        ),
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
        assert "projection=" in output
        assert "application:Terraform" in output
        assert "topology=" not in output
        assert "CURRENT" not in output  # the fixture has no Stack-owned Unit receipt


def test_get_stack_and_template_documents_preserve_fences_and_content(
    repository: Path, capsys: pytest.CaptureFixture[str]
):
    template = json.loads(run_get(repository, capsys, "stacktemplate", "web", "--environment", "staging", "-o", "json"))
    stack = json.loads(run_get(repository, capsys, "stack", "web", "--environment", "staging", "-o", "json"))

    assert template["metadata"]["uid"] == "uid-web"
    assert template["spec"]["contentDigest"].startswith("sha256:")
    assert template["spec"]["acquisition"]["documentDigest"].startswith("sha256:")
    assert template["spec"]["acquisition"]["requestedSource"] == {"fromInput": {}}
    assert template["spec"]["acquisition"]["resolvedSource"] == {"fromInput": {}}
    assert stack["spec"]["templateRef"] == {
        "name": "web",
        "uid": "uid-web",
        "contentDigest": template["spec"]["contentDigest"],
    }
    assert stack["spec"]["structuralProjection"]["identity"]["templateUid"] == "uid-web"
    assert (
        stack["spec"]["structuralProjection"]["identity"]["templateContentDigest"] == template["spec"]["contentDigest"]
    )

    template_table = run_get(repository, capsys, "stacktemplate", "web", "--environment", "staging")
    stack_table = run_get(repository, capsys, "stack", "web", "--environment", "staging")
    assert "input(document=sha256:" in template_table
    assert "REFERENCES" in template_table and "web" in template_table
    assert "uid-web" in stack_table
    assert template["spec"]["contentDigest"] in stack_table
    assert "context=sha256:" in stack_table
    assert "application<-" in stack_table


def test_get_stacktemplate_inspection_renders_all_acquisition_modes(
    repository: Path, capsys: pytest.CaptureFixture[str]
):
    inventory_support.git(repository, "checkout", "desired")
    inline = inventory_support.stack_template("inline", desired=True)
    git_template = inventory_support.stack_template("git", desired=True)
    git_spec = git_template["spec"]
    assert isinstance(git_spec, dict)
    git_spec["acquisition"] = {
        "documentDigest": "sha256:" + "d" * 64,
        "requestedSource": {
            "fromGit": {
                "repository": "https://deploy:secret@example.com/org/templates.git",
                "revision": "main",
                "path": "templates/git.yaml",
            }
        },
        "resolvedSource": {
            "fromGit": {
                "repository": "https://deploy:secret@example.com/org/templates.git",
                "revision": "c" * 40,
                "path": "templates/git.yaml",
            }
        },
    }
    git_spec["sourceContext"] = {
        "repository": "https://deploy:secret@example.com/org/templates.git",
        "revision": "c" * 40,
    }
    promotion_template = inventory_support.stack_template("promotion", desired=True)
    promotion_spec = promotion_template["spec"]
    assert isinstance(promotion_spec, dict)
    promotion_spec["acquisition"] = {
        "documentDigest": "sha256:" + "e" * 64,
        "requestedSource": {"fromPromotion": {"stack": "application"}},
        "resolvedSource": {
            "fromPromotion": {
                "environment": "dev",
                "desiredRef": "gitopsctr/desired/dev",
                "desiredRevision": "b" * 40,
                "stack": "application",
                "stackUid": "uid-application",
                "template": "promotion",
                "templateUid": "uid-application-template",
                "templateContentDigest": promotion_spec["contentDigest"],
            }
        },
    }
    inventory_support.write_json(repository / "stack-templates/inline.yaml", inline)
    inventory_support.write_json(repository / "stack-templates/git.yaml", git_template)
    inventory_support.write_json(repository / "stack-templates/promotion.yaml", promotion_template)
    revision = inventory_support.commit(repository, "add StackTemplate acquisition inspection cases")
    inventory_support.git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/acquisition-modes")
    inventory_support.git(repository, "checkout", "main")

    output = run_get(
        repository,
        capsys,
        "stacktemplates",
        "--environment",
        "staging",
        "--desired-ref",
        "gitopsctr/desired/acquisition-modes",
    )

    assert "input(document=sha256:" in output
    assert "git(repository=https://example.com/org/templates.git;requested=main;resolved=" in output
    assert "promotion(requested=application;resolved=dev/gitopsctr/desired/dev@" in output
    assert "https://deploy:secret@example.com" not in output
    assert "c" * 40 in output
    assert "b" * 40 in output


def test_get_stack_table_prepares_with_explicit_observed_snapshot(
    repository: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    observed_ref = "gitopsctr/observed/staging"
    observed_revision = inventory_support.git(repository, "rev-parse", f"refs/remotes/origin/{observed_ref}")
    calls: list[tuple[tuple[str, ...], str, str | None]] = []
    original = InventorySession.prepare_stack_inspection

    def record_prepare(
        inventory: InventorySession,
        records: tuple[InventoryRecord, ...],
        *,
        observed_ref: str,
        observed_revision: str | None,
        allow_missing_observed_ref: bool,
    ) -> None:
        calls.append((tuple(record.name for record in records), observed_ref, observed_revision))
        original(
            inventory,
            records,
            observed_ref=observed_ref,
            observed_revision=observed_revision,
            allow_missing_observed_ref=allow_missing_observed_ref,
        )

    monkeypatch.setattr(InventorySession, "prepare_stack_inspection", record_prepare)

    output = run_get(
        repository,
        capsys,
        "stack",
        "web",
        "--environment",
        "staging",
        "--observed-ref",
        observed_ref,
        "--observed-revision",
        observed_revision,
    )

    assert "web" in output
    assert calls[0] == (("web",), observed_ref, observed_revision)


def test_get_stack_tables_batch_inspection_preparation(
    repository: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    inventory_support.git(repository, "checkout", "desired")
    template = json.loads((repository / "stack-templates/web.yaml").read_text())
    inventory_support.write_json(
        repository / "stacks/worker.yaml",
        inventory_support.stack("worker", "web", desired=True, template_document=template),
    )
    inventory_support.write_json(
        repository / "stack-templates/other.yaml",
        inventory_support.stack_template("other", desired=True),
    )
    desired_revision = inventory_support.commit(repository, "add inspection batch resources")
    inventory_support.git(
        repository,
        "push",
        "origin",
        f"{desired_revision}:refs/heads/gitopsctr/desired/inspection-batch",
    )
    inventory_support.git(repository, "checkout", "main")

    batches: list[tuple[str, ...]] = []
    original = InventorySession._build_stack_inspection_summaries

    def record_batch(
        inventory: InventorySession,
        records: tuple[InventoryRecord, ...],
        *,
        observed_ref: str,
        observed_revision: str | None,
        allow_missing_observed_ref: bool,
    ) -> dict[PurePosixPath, StackInspectionSummary]:
        batches.append(tuple(record.name for record in records))
        return original(
            inventory,
            records,
            observed_ref=observed_ref,
            observed_revision=observed_revision,
            allow_missing_observed_ref=allow_missing_observed_ref,
        )

    monkeypatch.setattr(InventorySession, "_build_stack_inspection_summaries", record_batch)

    stacks = run_get(
        repository,
        capsys,
        "stacks",
        "--environment",
        "staging",
        "--desired-ref",
        "gitopsctr/desired/inspection-batch",
    )
    templates = run_get(
        repository,
        capsys,
        "stacktemplates",
        "--environment",
        "staging",
        "--desired-ref",
        "gitopsctr/desired/inspection-batch",
    )

    assert "worker" in stacks
    assert "other" in templates
    assert {frozenset(batch) for batch in batches} == {
        frozenset(("web", "worker")),
        frozenset(("web", "other")),
    }
    assert len(batches) == 2


def test_get_unit_desired_column_is_the_resource_blob(repository: Path, capsys: pytest.CaptureFixture[str]):
    output = run_get(repository, capsys, "unit", "application", "--environment", "dev")
    desired = output.splitlines()[1].split()[2]
    assert len(desired) == 12
    assert all(character in "0123456789abcdef" for character in desired)


def test_get_desired_unit_table_uses_explicit_observed_snapshot(repository: Path, capsys: pytest.CaptureFixture[str]):
    inventory_support.git(repository, "checkout", "observed")
    (repository / "units/application.yaml").unlink()
    empty_revision = inventory_support.commit(repository, "empty observed override")
    inventory_support.git(
        repository,
        "push",
        "origin",
        f"{empty_revision}:refs/heads/gitopsctr/observed/empty-override",
    )
    inventory_support.git(repository, "checkout", "main")

    table = run_get(
        repository,
        capsys,
        "unit",
        "application",
        "--environment",
        "dev",
        "--observed-ref",
        "gitopsctr/observed/empty-override",
    )
    assert "MISSING" in table

    raw = json.loads(
        run_get(
            repository,
            capsys,
            "unit",
            "application",
            "--environment",
            "dev",
            "--observed-ref",
            "gitopsctr/observed/empty-override",
            "-o",
            "json",
        )
    )
    assert raw["metadata"]["name"] == "application"


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


def test_get_stack_table_explicit_missing_observed_ref_fails(repository: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(OperationError, match="observed ref 'gitopsctr/observed/missing' does not exist"):
        run_get(
            repository,
            capsys,
            "stack",
            "web",
            "--environment",
            "staging",
            "--observed-ref",
            "gitopsctr/observed/missing",
        )


def test_get_stack_table_explicit_missing_observed_revision_fails(repository: Path, capsys: pytest.CaptureFixture[str]):
    inventory_support.write_json(repository / "revision-only-marker.json", {})
    missing_revision = inventory_support.commit(repository, "unrelated revision for observed override")

    with pytest.raises(OperationError, match="requested revision is not part of"):
        run_get(
            repository,
            capsys,
            "stack",
            "web",
            "--environment",
            "staging",
            "--observed-revision",
            missing_revision,
        )


def test_documented_get_commands_execute_across_dev_and_staging(repository: Path, capsys: pytest.CaptureFixture[str]):
    commands = (
        ("environments",),
        ("environment", "dev"),
        ("all", "--environment", "dev"),
        ("all", "-A"),
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
