"""Deployment progress stays visible while machine-readable stdout stays clean."""

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli as deploy_release
from gitopsctr.contracts import DesiredSource, ResolvedInputs
from gitopsctr.contrib.drivers.frontend_s3_cloudfront import FrontendDesiredUnit
from gitopsctr.driver import UnitResolution
from gitopsctr.resources import ResourceMetadata
from tests.conftest import receipt_document, receipt_resource, write_test_document


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _promotion_context(root: Path) -> deploy_release.PromotionContext:
    return deploy_release.PromotionContext(
        source_environment="staging",
        desired_ref="deploy/staging",
        desired_revision="b" * 40,
        observed_ref="observed/staging",
        observed_revision="c" * 40,
        specification_revision="a" * 40,
        desired_root=root,
    )


def _terraform_desired_resource(name: str = "aws-application"):
    resource = deploy_release.RESOURCE_CATALOG.parse_unit(
        {
            "name": name,
            "driver": "terraform",
            "source": {"path": ".", "revision": "a" * 40},
        },
        profile="desired",
        expected_name=name,
    )
    return resource.with_metadata(ResourceMetadata.new_source_tracked(name))


def test_root_help_groups_commands_and_describes_each_command():
    help_text = deploy_release.build_parser().format_help()

    assert "usage: " in help_text and " COMMAND ..." in help_text
    assert "commands:\n" in help_text
    assert "positional arguments:" not in help_text
    assert "Project:\n" in help_text
    assert "Deployment:\n" in help_text
    assert "Inspection:\n" in help_text
    assert "Git data:\n" in help_text
    assert "    promote             promote reviewed desired state" in help_text
    assert "    reconcile           reconcile one deployment unit" in help_text


def test_desired_resolution_logs_unit_and_observation_decision(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    unit = {
        "schema": 1,
        "name": "frontend",
        "driver": "frontend-s3-cloudfront",
        "source": {"path": "scripts/deployment_drivers.py"},
    }
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(source / "deployment/environments/dev/units/frontend.json", unit)
    _write_json(
        current / "units/frontend.json",
        {**unit, "resolvedInputs": {"receipts": {"units/aws-application.json": "old"}}},
    )
    observed.mkdir()

    monkeypatch.setattr(
        deploy_release,
        "resolved_unit_source",
        lambda *_args: deploy_release.ResolvedUnitSourceResult(
            source=DesiredSource(
                path="scripts/deployment_drivers.py",
                revision="a" * 40,
                inputHash="sha256:value",
            ),
            inputs_changed=False,
        ),
    )
    monkeypatch.setattr(
        deploy_release.UNIT_DRIVERS["frontend-s3-cloudfront"],
        "resolve_unit",
        lambda _unit, context: UnitResolution(
            FrontendDesiredUnit(
                source=context.source,
                resolvedInputs=ResolvedInputs(receipts={"aws-application": "new"}),
            ),
            ResolvedInputs(receipts={"aws-application": "new"}),
        ),
    )

    deploy_release.build_desired_candidate(
        "dev",
        source,
        "b" * 40,
        current,
        observed,
        "c" * 40,
        candidate,
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "==> Resolve desired state for dev" in output.err
    assert "CHECK    frontend: inputs unchanged; retain aaaaaaaaaaaa" in output.err
    assert "OBSERVE  frontend: new observation changes resolved inputs" in output.err
    assert "UPDATE   frontend: desired state changed" in output.err


def test_desired_candidate_drops_legacy_artifact_catalogue(tmp_path, monkeypatch):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    unit = {
        "schema": 1,
        "name": "application-images",
        "driver": "oci-images",
        "source": {"path": "."},
    }
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(source / "deployment/environments/dev/units/application-images.json", unit)
    _write_json(current / "artifacts/containers.json", {"legacy": True})
    observed.mkdir()
    monkeypatch.setattr(
        deploy_release,
        "resolved_unit_source",
        lambda *_args: deploy_release.ResolvedUnitSourceResult(
            source=DesiredSource(path=".", revision="a" * 40, inputHash="sha256:value"),
            inputs_changed=False,
        ),
    )

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate)

    assert deploy_release.unit_document_path(candidate, "application-images").is_file()
    assert not (candidate / "artifacts").exists()


def test_show_receipt_reports_receipt_and_artifacts(monkeypatch, capsys):
    expected = receipt_document(
        "terraform",
        "aws-application",
        {"revision": "b" * 40, "unitBlob": "blob"},
        {"applied": {"sourceRevision": "c" * 40}, "outputs": {"api_url": "https://api.example"}},
        resolved_inputs={},
        controller={"observed_at": "2026-08-07T00:00:00Z"},
    )

    def materialize_observed(_ref, output):
        _write_json(output / "units/aws-application.json", expected)
        return "a" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", materialize_observed)
    args = deploy_release.build_parser().parse_args(
        ["show", "receipt", "--environment", "dev", "aws-application", "--json"]
    )

    args.handler(args)

    result = json.loads(capsys.readouterr().out)
    assert result == expected


def test_show_document_format_can_force_yaml_or_json():
    args = deploy_release.build_parser().parse_args(["show", "receipt", "--environment", "dev", "web", "--yaml"])
    assert args.yaml is True
    assert args.json is False

    with pytest.raises(SystemExit):
        deploy_release.build_parser().parse_args(["show", "receipt", "--environment", "dev", "web", "--json", "--yaml"])


def test_blocked_driver_transition_omits_previous_unit_and_reports_wait(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    specification = {
        "schema": 1,
        "name": "frontend",
        "driver": "frontend-s3-cloudfront",
        "source": {"path": "scripts/deployment_drivers.py"},
        "inputs": {
            "bundle": {
                "fromArtifact": {
                    "unit": "frontend-bundle",
                    "name": "frontend",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "FrontendBundle",
                    "pointer": "/bundle/uri",
                },
            }
        },
    }
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(source / "deployment/environments/dev/units/frontend.json", specification)
    _write_json(
        current / "units/frontend.json",
        {
            "schema": 1,
            "name": "frontend",
            "driver": "vite-s3-cloudfront",
            "source": {"path": "frontend", "revision": "a" * 40},
        },
    )
    observed.mkdir()
    monkeypatch.setattr(
        deploy_release,
        "resolved_unit_source",
        lambda *_args: deploy_release.ResolvedUnitSourceResult(
            source=DesiredSource(
                path="scripts/deployment_drivers.py",
                revision="b" * 40,
                inputHash="sha256:value",
            ),
            inputs_changed=True,
        ),
    )

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate)

    assert deploy_release.load_json(candidate / "units/frontend.json") == deploy_release.load_json(
        current / "units/frontend.json"
    )
    assert "retain legacy cleanup root" in capsys.readouterr().err


def test_blocked_unit_with_same_driver_retains_previous_desired_state(tmp_path, monkeypatch):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    specification = {
        "schema": 1,
        "name": "frontend",
        "driver": "frontend-s3-cloudfront",
        "source": {"path": "scripts/deployment_drivers.py"},
        "inputs": {
            "bundle": {
                "fromArtifact": {
                    "unit": "frontend-bundle",
                    "name": "frontend",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "FrontendBundle",
                    "pointer": "/bundle/uri",
                },
            }
        },
    }
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(source / "deployment/environments/dev/units/frontend.json", specification)
    previous = current / "units/frontend.json"
    _write_json(
        previous,
        {
            "name": "frontend",
            "driver": "frontend-s3-cloudfront",
            "source": {
                "path": "frontend",
                "revision": "a" * 40,
                "inputHash": "sha256:previous",
                "driverVersion": 2,
            },
        },
    )
    observed.mkdir()
    monkeypatch.setattr(
        deploy_release,
        "resolved_unit_source",
        lambda *_args: deploy_release.ResolvedUnitSourceResult(
            source=DesiredSource(
                path="scripts/deployment_drivers.py",
                revision="b" * 40,
                inputHash="sha256:value",
            ),
            inputs_changed=True,
        ),
    )

    result = deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate)

    retained = deploy_release.load_desired_unit(candidate / "units/frontend.json", "frontend")
    assert retained.is_legacy_compatibility is False
    assert retained.metadata.uid
    assert retained.metadata.lifecycle is not None
    assert "frontend" in result.blocked


def test_removing_producer_environment_preserves_existing_input_hash(tmp_path, monkeypatch):
    source = tmp_path / "source"
    current = tmp_path / "current"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    specification = {
        "schema": 1,
        "name": "application-images",
        "driver": "oci-images",
        "source": {"path": ".", "inputs": ["Dockerfile"]},
        "build": {"dockerfile": "Dockerfile", "platform": "linux/amd64"},
        "publish": {"targets": {"control": {"type": "registry", "repository": "registry.example.com/control"}}},
    }
    legacy_specification = {**specification, "environment": "dev"}
    legacy_resource = deploy_release.parse_authored_unit_document(legacy_specification, "application-images")
    specification_resource = deploy_release.parse_authored_unit_document(specification, "application-images")
    legacy_hash = deploy_release.unit_input_hash(legacy_resource, source)
    _write_json(
        current / "units/application-images.json",
        {
            **legacy_specification,
            "source": {
                **legacy_specification["source"],
                "revision": "a" * 40,
                "inputHash": legacy_hash,
            },
        },
    )
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)

    result = deploy_release.resolved_unit_source(
        specification_resource,
        source,
        "b" * 40,
        current,
        None,
        deploy_release.SourceRevisionPolicy(
            unavailable_when=deploy_release.SourceRevisionUnavailableWhen.MISSING,
        ),
    )

    assert result.source is not None
    assert result.source.inputHash == legacy_hash
    assert result.source.revision == "a" * 40
    assert result.inputs_changed is False


def _source_resolution_fixture(tmp_path: Path, previous_revision: str, input_hash: str):
    source = tmp_path / "source"
    current = tmp_path / "current"
    source.mkdir()
    (source / "main.tf").write_text("terraform {}\n")
    specification = deploy_release.parse_authored_unit_document(
        {
            "schema": 1,
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "."},
        },
        "aws-application",
    )
    _write_json(
        current / "units/aws-application.json",
        {
            "schema": 1,
            "name": "aws-application",
            "driver": "terraform",
            "source": {
                "path": ".",
                "revision": previous_revision,
                "inputHash": input_hash,
                "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
            },
        },
    )
    return source, current, specification


def test_matching_input_hash_retains_available_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda revision: revision == previous_revision)

    result = deploy_release.resolved_unit_source(
        specification,
        source,
        candidate_revision,
        current,
        None,
        deploy_release.SourceRevisionPolicy(
            unavailable_when=deploy_release.SourceRevisionUnavailableWhen.MISSING,
        ),
    )

    assert result.source is not None
    assert result.source.revision == previous_revision
    assert result.inputs_changed is False


def test_outside_candidate_history_retains_ancestor_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(
        deploy_release,
        "commit_is_ancestor",
        lambda previous, candidate: (previous, candidate) == (previous_revision, candidate_revision),
    )

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current, None)

    assert result.source is not None
    assert result.source.revision == previous_revision
    assert result.inputs_changed is False
    assert result.refresh_reason is None


def test_outside_candidate_history_refreshes_dangling_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(deploy_release, "commit_is_ancestor", lambda *_args: False)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current, None)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.inputs_changed is True
    assert result.refresh_reason is not None
    assert "outside candidate history" in result.refresh_reason


def test_matching_input_hash_refreshes_unavailable_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: False)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current, None)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.inputs_changed is True


def test_changed_input_hash_uses_candidate_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:old")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:new")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current, None)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.inputs_changed is True


def test_source_less_unit_remains_without_a_resolved_source(tmp_path):
    specification = deploy_release.parse_authored_unit_document(
        {
            "schema": 1,
            "name": "frontend",
            "driver": "frontend-s3-cloudfront",
        },
        "frontend",
    )

    result = deploy_release.resolved_unit_source(specification, tmp_path, "b" * 40, tmp_path / "current", None)

    assert result.source is None
    assert result.inputs_changed is False


@pytest.mark.parametrize(
    "unavailable_when",
    [
        deploy_release.SourceRevisionUnavailableWhen.MISSING,
        deploy_release.SourceRevisionUnavailableWhen.OUTSIDE_CANDIDATE_HISTORY,
    ],
)
def test_unavailable_retained_source_refreshes_desired_state_and_logs(tmp_path, monkeypatch, capsys, unavailable_when):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, _ = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(
        source / "deployment/environments/dev/units/aws-application.json",
        {
            "schema": 1,
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "."},
        },
    )
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    observed.mkdir()
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: False)

    result = deploy_release.build_desired_candidate(
        "dev",
        source,
        candidate_revision,
        current,
        observed,
        None,
        candidate,
        dry=True,
        source_revision_policy=deploy_release.SourceRevisionPolicy(unavailable_when=unavailable_when),
    )

    refreshed = deploy_release.load_desired_unit(candidate / "units/aws-application.json", "aws-application")
    assert result.blocked == {}
    assert refreshed.spec.source.revision == candidate_revision
    output = capsys.readouterr().err
    assert "REFRESH  aws-application: retained source aaaaaaaaaaaa is unavailable; use bbbbbbbbbbbb" in output


def test_outside_candidate_history_refreshes_desired_state_and_logs(tmp_path, monkeypatch, capsys):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, _ = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(
        source / "deployment/environments/dev/units/aws-application.json",
        {
            "schema": 1,
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "."},
        },
    )
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    observed.mkdir()
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(deploy_release, "commit_is_ancestor", lambda *_args: False)

    deploy_release.build_desired_candidate(
        "dev",
        source,
        candidate_revision,
        current,
        observed,
        None,
        candidate,
        dry=True,
        source_revision_policy=deploy_release.SourceRevisionPolicy(),
    )

    refreshed = deploy_release.load_desired_unit(candidate / "units/aws-application.json", "aws-application")
    assert refreshed.spec.source.revision == candidate_revision
    output = capsys.readouterr().err
    assert (
        "REFRESH  aws-application: retained source aaaaaaaaaaaa is outside candidate history; use bbbbbbbbbbbb"
    ) in output


def test_advance_policy_error_leaves_the_candidate_unpublished(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, _ = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    _write_json(source / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
    _write_json(
        source / "deployment/environments/dev/units/aws-application.json",
        {
            "schema": 1,
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "."},
        },
    )
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    observed.mkdir()
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(deploy_release, "commit_is_ancestor", lambda *_args: False)

    with pytest.raises(deploy_release.SourceRevisionUnavailableError, match="unavailable under project policy"):
        deploy_release.build_desired_candidate(
            "dev",
            source,
            candidate_revision,
            current,
            observed,
            None,
            candidate,
            source_revision_policy=deploy_release.SourceRevisionPolicy(
                when_unavailable_during_advance=deploy_release.SourceRevisionAction.ERROR,
            ),
            source_revision_operation="advance",
        )

    assert not deploy_release.unit_document_path(candidate, "aws-application").is_file()


def test_main_reports_actionable_plan_policy_error(monkeypatch, capsys):
    def handler(_args):
        raise deploy_release.SourceRevisionUnavailableError("aws-application", "a" * 40, "plan")

    monkeypatch.setattr(
        deploy_release,
        "build_parser",
        lambda: SimpleNamespace(
            parse_args=lambda: SimpleNamespace(command="schemas", handler=handler),
        ),
    )

    assert deploy_release.main() == 1
    output = capsys.readouterr().err
    assert "ERROR    aws-application: desired source aaaaaaaaaaaa is unavailable under project policy" in output
    assert "Run advance-desired from a durable source revision before planning." in output


def test_source_input_globs_hash_only_matching_files(tmp_path):
    source = tmp_path / "source"
    deploy = source / "infra/deploy"
    (deploy / "modules/api").mkdir(parents=True)
    (deploy / "main.tf").write_text("terraform {}\n")
    (deploy / "variables.tf").write_text('variable "name" {}\n')
    (deploy / "modules/api/main.tf").write_text('output "name" { value = "api" }\n')
    (deploy / "README.md").write_text("Documentation\n")

    glob_hash = deploy_release.hash_source_inputs(
        source,
        "infra/deploy",
        ["*.tf", "modules/**/*.tf"],
        {"kind": "test"},
    )
    explicit_hash = deploy_release.hash_source_inputs(
        source,
        "infra/deploy",
        ["main.tf", "variables.tf", "modules/api/main.tf"],
        {"kind": "test"},
    )
    (deploy / "README.md").write_text("Changed documentation\n")

    assert glob_hash == explicit_hash
    assert glob_hash == deploy_release.hash_source_inputs(
        source,
        "infra/deploy",
        ["*.tf", "modules/**/*.tf"],
        {"kind": "test"},
    )

    (deploy / "modules/api/main.tf").write_text('output "name" { value = "changed" }\n')
    assert glob_hash != deploy_release.hash_source_inputs(
        source,
        "infra/deploy",
        ["*.tf", "modules/**/*.tf"],
        {"kind": "test"},
    )


def test_source_input_glob_must_match_at_least_one_path(tmp_path):
    source = tmp_path / "source"
    (source / "infra/deploy").mkdir(parents=True)

    with pytest.raises(deploy_release.OperationError, match=r"source input pattern does not match: .*\*\.tf"):
        deploy_release.hash_source_inputs(source, "infra/deploy", ["*.tf"], {"kind": "test"})


def test_source_input_globs_become_git_glob_pathspecs():
    assert deploy_release.unit_source_paths(
        {
            "path": "infra/deploy",
            "inputs": ["*.tf", "modules/**/*.tf", ".terraform.lock.hcl"],
        }
    ) == [
        ":(glob)infra/deploy/*.tf",
        ":(glob)infra/deploy/modules/**/*.tf",
        "infra/deploy/.terraform.lock.hcl",
    ]


def test_change_explanation_lists_only_promotion_selectors_whose_fingerprint_changed():
    base = {
        "name": "application",
        "driver": "terraform",
        "source": {"path": ".", "revision": "a" * 40},
        "terraform": {"variables": {}},
    }
    previous = deploy_release.parse_desired_unit_document(
        {
            **base,
            "resolvedInputs": {"promotions": {"application#/image": "same", "application#/tag": "old"}},
        },
        "application",
    )
    current = deploy_release.parse_desired_unit_document(
        {
            **base,
            "resolvedInputs": {"promotions": {"application#/image": "same", "application#/tag": "new"}},
        },
        "application",
    )

    explanation = deploy_release.classify_unit_change(previous, current, "b" * 40)

    assert explanation.causes == ("reviewed promotion inputs changed: application#/tag",)


def test_promotion_reference_materializes_from_source_desired_unit(tmp_path):
    promotion = tmp_path / "promotion"
    source_unit = promotion / "units/aws-application.json"
    _write_json(
        source_unit,
        {
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": ".", "revision": "a" * 40},
            "terraform": {"variables": {"control_image_uri": "registry.example/control@sha256:" + "1" * 64}},
        },
    )

    resolution = deploy_release.resolve_template(
        {"control_image_uri": {"fromPromotion": {}}},
        tmp_path / "candidate",
        tmp_path / "observed",
        None,
        promotion=_promotion_context(promotion),
        target_unit="aws-application",
        target_gvk=deploy_release.GVK("unit.gitopsctr.io/v1", "Terraform"),
        pointer="/terraform/variables",
    )

    assert resolution.value == {"control_image_uri": "registry.example/control@sha256:" + "1" * 64}
    assert resolution.promotions == {
        "aws-application#/terraform/variables/control_image_uri": deploy_release.file_blob(source_unit)
    }
    assert resolution.receipts == {}
    assert resolution.artifacts == {}


def test_explicit_empty_promotion_pointer_selects_public_spec_and_allows_cross_gvk(tmp_path):
    promotion = tmp_path / "promotion"
    source_unit = promotion / "units/aws-application.json"
    _write_json(
        source_unit,
        {
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": ".", "revision": "a" * 40},
            "terraform": {"variables": {"environment": "prod"}},
        },
    )

    resolution = deploy_release.resolve_template(
        {"fromPromotion": {"unit": "aws-application", "pointer": ""}},
        tmp_path / "candidate",
        tmp_path / "observed",
        None,
        promotion=_promotion_context(promotion),
        target_unit="frontend",
        target_gvk=deploy_release.GVK("unit.gitopsctr.io/v1", "FrontendS3Cloudfront"),
    )

    source = deploy_release.load_desired_unit(source_unit, "aws-application")
    assert resolution.value == source.driver.desired_unit_contract.dump(source.spec)
    assert "name" not in resolution.value
    assert "driver" not in resolution.value
    assert resolution.promotions == {"aws-application#": deploy_release.file_blob(source_unit)}


def test_implicit_promotion_pointer_requires_matching_gvk_even_with_dry_fallback(tmp_path):
    promotion = tmp_path / "promotion"
    _write_json(
        promotion / "units/aws-application.json",
        {
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": ".", "revision": "a" * 40},
            "terraform": {"variables": {"image": "release"}},
        },
    )

    with pytest.raises(deploy_release.OperationError, match="requires matching GVKs"):
        deploy_release.resolve_template(
            {"image": {"fromPromotion": {"unit": "aws-application", "dryFallback": "preview"}}},
            tmp_path / "candidate",
            tmp_path / "observed",
            None,
            promotion=_promotion_context(promotion),
            target_unit="frontend",
            target_gvk=deploy_release.GVK("unit.gitopsctr.io/v1", "FrontendS3Cloudfront"),
            pointer="/inputs",
            dry=True,
        )


def test_active_promotion_missing_unit_or_pointer_is_fatal_despite_dry_fallback(tmp_path):
    promotion = tmp_path / "promotion"
    promotion.mkdir()
    context = _promotion_context(promotion)
    arguments = (
        tmp_path / "candidate",
        tmp_path / "observed",
        None,
    )

    with pytest.raises(deploy_release.OperationError, match="does not contain source unit"):
        deploy_release.resolve_template(
            {"fromPromotion": {"unit": "missing", "pointer": "/image", "dryFallback": "preview"}},
            *arguments,
            promotion=context,
            target_unit="application",
            dry=True,
        )

    _write_json(
        promotion / "units/application.json",
        {
            "name": "application",
            "driver": "terraform",
            "source": {"path": ".", "revision": "a" * 40},
            "terraform": {"variables": {}},
        },
    )
    with pytest.raises(deploy_release.OperationError, match="cannot resolve pointer"):
        deploy_release.resolve_template(
            {"fromPromotion": {"pointer": "/missing", "dryFallback": "preview"}},
            *arguments,
            promotion=context,
            target_unit="application",
            dry=True,
        )


def test_promoted_candidate_records_pinned_context_and_source_unit_blob(tmp_path, monkeypatch):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    promoted = tmp_path / "promoted"
    candidate = tmp_path / "candidate"
    specification = {
        "schema": 1,
        "name": "aws-application",
        "driver": "terraform",
        "source": {"path": "infra/deploy"},
        "terraform": {
            "variables": {
                "control_image_uri": {
                    "fromPromotion": {},
                }
            }
        },
    }
    _write_json(
        source / "deployment/environments/staging/environment.json",
        {
            "schema": 1,
            "name": "staging",
            "promotion": {"allowedSources": ["dev"]},
        },
    )
    _write_json(
        source / "deployment/environments/staging/units/aws-application.json",
        specification,
    )
    promoted_unit = promoted / "units/aws-application.json"
    _write_json(
        promoted_unit,
        {
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "infra/deploy", "revision": "a" * 40},
            "terraform": {"variables": {"control_image_uri": "registry.example/control@sha256:" + "1" * 64}},
        },
    )
    current.mkdir()
    observed.mkdir()
    monkeypatch.setattr(
        deploy_release,
        "resolved_unit_source",
        lambda *_args: deploy_release.ResolvedUnitSourceResult(
            source=DesiredSource(path="infra/deploy", revision="a" * 40, inputHash="sha256:value"),
            inputs_changed=True,
        ),
    )
    context = deploy_release.PromotionContext(
        source_environment="dev",
        desired_ref="deploy/dev",
        desired_revision="b" * 40,
        observed_ref="observed/dev",
        observed_revision="c" * 40,
        specification_revision="a" * 40,
        desired_root=promoted,
    )

    deploy_release.build_desired_candidate(
        "staging",
        source,
        "a" * 40,
        current,
        observed,
        None,
        candidate,
        promotion=context,
    )

    unit_path = deploy_release.unit_document_path(candidate, "aws-application")
    assert unit_path.read_text().startswith(
        "# yaml-language-server: $schema="
        "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/"
        "unit.gitopsctr.io/v1/Terraform/desired.schema.json\n"
    )
    unit = deploy_release.load_desired_unit(unit_path, "aws-application")
    assert unit.spec.terraform.variables["control_image_uri"].endswith("1" * 64)
    assert unit.spec.resolvedInputs.promotions == {
        "aws-application#/terraform/variables/control_image_uri": deploy_release.file_blob(promoted_unit)
    }
    promotion_path = deploy_release.document_candidates(candidate, "promotion")[0]
    assert promotion_path.read_text().startswith(
        "# yaml-language-server: $schema="
        "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/"
        "gitopsctr.io/v1/Promotion.schema.json\n"
    )
    raw_promotion = deploy_release.load_json(promotion_path)
    promotion = deploy_release.normalize_promotion_document(raw_promotion)
    assert promotion == {key: value for key, value in context.document().items() if key != "$schema"}


def test_promotion_requires_every_source_unit_to_be_clean(tmp_path):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    first = desired / "units/first.json"
    second = desired / "units/second.json"
    _write_json(first, _unit("first"))
    _write_json(second, _unit("second"))
    _write_json(
        observed / "units/first.json",
        receipt_document("terraform", "first", {"unitBlob": deploy_release.file_blob(first)}),
    )

    with pytest.raises(deploy_release.OperationError, match=r"second \(ready\)"):
        deploy_release.require_clean_source(desired, observed)

    _write_json(
        observed / "units/second.json",
        receipt_document("terraform", "second", {"unitBlob": deploy_release.file_blob(second)}),
    )
    deploy_release.require_clean_source(desired, observed)


def test_promoted_advance_uses_reviewed_specification_revision(tmp_path, monkeypatch):
    reviewed = "a" * 40
    materialized: list[str] = []
    built: list[str] = []

    def fake_materialize(revision, output):
        materialized.append(revision)
        _write_json(
            output / "deployment/environments/staging/environment.json",
            {
                "schema": 1,
                "name": "staging",
                "promotion": {"allowedSources": ["dev"]},
            },
        )
        _write_json(
            output / "deployment/environments/staging/units/aws-application.json",
            {
                "schema": 1,
                "name": "aws-application",
                "driver": "terraform",
                "source": {"path": "infra/deploy"},
            },
        )

    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)

    def fake_observed_tree(ref, output):
        output.mkdir(parents=True, exist_ok=True)
        return "c" * 40 if ref == "deploy/staging" else None

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    promotion_root = tmp_path / "promoted"
    promotion_root.mkdir()
    context = deploy_release.PromotionContext(
        source_environment="dev",
        desired_ref="deploy/dev",
        desired_revision="d" * 40,
        observed_ref="observed/dev",
        observed_revision="e" * 40,
        specification_revision=reviewed,
        desired_root=promotion_root,
    )
    monkeypatch.setattr(deploy_release, "load_promotion_context", lambda *_args: context)

    def fake_build(environment, source_root, source_revision, *_args, **kwargs):
        built.append(source_revision)
        candidate = _args[3]
        _write_json(
            candidate / "units/aws-application.json",
            deploy_release.serialize_unit_document(_terraform_desired_resource("aws-application")),
        )
        _write_json(candidate / "promotion.json", kwargs["promotion"].document())

    monkeypatch.setattr(deploy_release, "build_desired_candidate", fake_build)
    monkeypatch.setattr(
        deploy_release,
        "log_reconciliation_summary",
        lambda *_args: (_ for _ in ()).throw(AssertionError("suppressed advancement summary was logged")),
    )

    _, changed = deploy_release.advance_desired("staging", None, dry=True, summarize=False)

    assert changed is True
    assert materialized == [reviewed]
    assert built == [reviewed]


def test_promote_parser_exposes_environment_and_revision_contract():
    args = deploy_release.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--source-desired-revision",
            "a" * 40,
        ]
    )

    assert args.from_environment == "dev"
    assert args.to_environment == "staging"
    assert args.source_desired_revision == "a" * 40


def test_advance_desired_command_requests_dirty_source_warning(monkeypatch, capsys):
    calls = []

    def fake_advance(*args, **kwargs):
        calls.append((args, kwargs))
        return "a" * 40, False

    monkeypatch.setattr(deploy_release, "advance_desired", fake_advance)
    args = deploy_release.build_parser().parse_args(
        ["advance-desired", "--environment", "dev", "--source-revision", "HEAD", "--dry"]
    )

    args.handler(args)

    assert calls[0][1]["warn_uncommitted"] is True
    assert capsys.readouterr().out == "a" * 40 + "\n"


def _install_promotion_simulation(monkeypatch, gate: str):
    specification_revision = "a" * 40
    source_desired_revision = "b" * 40
    source_observed_revision = "c" * 40
    target_revision = "d" * 40
    publications = []

    def fake_git(*args, **_kwargs):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, specification_revision + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def materialize(revision, output):
        output.mkdir(parents=True, exist_ok=True)
        if revision != specification_revision:
            _write_json(output / "units/application.json", {"materialized": revision})
            return
        _write_json(
            output / "deployment/environments/dev/environment.json",
            {"schema": 1, "name": "dev"},
        )
        _write_json(
            output / "deployment/environments/prod/environment.json",
            {
                "schema": 1,
                "name": "prod",
                "changeGate": gate,
                "promotion": {"allowedSources": ["dev"]},
            },
        )

    def observed_tree(ref, output):
        output.mkdir(parents=True, exist_ok=True)
        return target_revision if ref == "gitopsctr/desired/prod" else None

    def publish(ref, _directory, parent, message):
        publications.append((ref, parent, message))
        return "e" * 40

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(
        deploy_release,
        "resolve_ref",
        lambda ref, _revision=None: (
            source_desired_revision if ref == "gitopsctr/desired/dev" else source_observed_revision
        ),
    )
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "require_clean_source", lambda *_args: None)
    monkeypatch.setattr(
        deploy_release,
        "build_desired_candidate",
        lambda *_args, **_kwargs: _write_json(
            _args[6] / "units/application.json",
            deploy_release.serialize_unit_document(_terraform_desired_resource("application")),
        ),
    )
    monkeypatch.setattr(deploy_release, "publish_tree", publish)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: None)
    monkeypatch.setattr(deploy_release, "candidate_identifier", lambda *_args, **_kwargs: "candidate123")
    return publications


def test_promotion_without_a_change_gate_publishes_target_directly(monkeypatch, capsys):
    publications = _install_promotion_simulation(monkeypatch, "none")
    monkeypatch.setattr(
        deploy_release,
        "ensure_change_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct promotion opened a pull request")),
    )
    args = deploy_release.build_parser().parse_args(
        ["promote", "--from-environment", "dev", "--to-environment", "prod"]
    )

    args.handler(args)

    assert publications == [("gitopsctr/desired/prod", "d" * 40, "Promote dev to prod from " + "b" * 40)]
    assert capsys.readouterr().out == "e" * 40 + "\n"


def test_promotion_with_a_change_gate_creates_a_pull_request(monkeypatch):
    publications = _install_promotion_simulation(monkeypatch, "pullRequest")
    requests = []

    def ensure(specification, **_kwargs):
        requests.append(specification)
        return deploy_release.ChangeRequestResult(status="created", url="https://github.example/pull/1")

    monkeypatch.setattr(deploy_release, "ensure_change_request", ensure)
    args = deploy_release.build_parser().parse_args(
        ["promote", "--from-environment", "dev", "--to-environment", "prod"]
    )

    args.handler(args)

    assert publications == [
        (
            "gitopsctr/candidates/prod/candidate123",
            "d" * 40,
            "Promote dev to prod from " + "b" * 40,
        )
    ]
    assert requests[0].base == "gitopsctr/desired/prod"
    assert requests[0].head == "gitopsctr/candidates/prod/candidate123"


def test_progress_helpers_keep_result_stdout_clean(capsys):
    deploy_release.log_heading("Reconcile frontend")
    deploy_release.log_status("DONE", "frontend: clean")

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "\n==> Reconcile frontend\n    DONE     frontend: clean\n"


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked", "ignored"])
def test_working_tree_change_detection_matches_git_status(tmp_path, monkeypatch, change):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("initial\n")
    if change == "ignored":
        (repository / ".gitignore").write_text("ignored.txt\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)

    if change in {"unstaged", "staged"}:
        tracked.write_text("changed\n")
    elif change == "untracked":
        (repository / "untracked.txt").write_text("new\n")
    else:
        (repository / "ignored.txt").write_text("ignored\n")
    if change == "staged":
        subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)

    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", repository)
    assert deploy_release.working_tree_has_uncommitted_changes() == (change != "ignored")


def test_source_revision_warning_is_actionable(monkeypatch, capsys):
    monkeypatch.setattr(deploy_release, "working_tree_has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(deploy_release, "describe_revision", lambda revision: revision[:12])

    deploy_release.warn_if_source_revision_excludes_changes("a" * 40)

    output = capsys.readouterr()
    assert output.out == ""
    assert "WARN" in output.err
    assert "uncommitted working-tree changes are excluded from source revision aaaaaaaaaaaa" in output.err
    assert "commit them and select the resulting commit" in output.err


class _FakeStream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_color_detection_respects_tty_ci_files_and_overrides(tmp_path, monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "CI", "TERM"):
        monkeypatch.delenv(name, raising=False)

    assert not deploy_release.color_enabled(_FakeStream(False))
    assert deploy_release.color_enabled(_FakeStream(True))

    monkeypatch.setenv("CI", "true")
    assert deploy_release.color_enabled(_FakeStream(False))
    with (tmp_path / "output.log").open("w") as output:
        assert not deploy_release.color_enabled(output)

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert not deploy_release.color_enabled(_FakeStream(True))
    monkeypatch.delenv("NO_COLOR")
    assert deploy_release.color_enabled(_FakeStream(False))

    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("FORCE_COLOR")
    assert not deploy_release.color_enabled(_FakeStream(False))


def test_colored_progress_uses_semantic_roles_and_keeps_stdout_clean(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    deploy_release.log_heading("Reconcile frontend")
    deploy_release.log_status("DONE", f"{deploy_release.style_unit('frontend')}: clean")
    deploy_release.log_status(
        "DESIRED",
        f"{deploy_release.style_branch('deploy/dev')} in {deploy_release.style_environment('dev')}",
    )
    deploy_release.log_status("RESULT", "FAILED: reconciliation failed")

    output = capsys.readouterr()
    assert output.out == ""
    assert "\x1b[1;36mReconcile frontend\x1b[0m" in output.err
    assert "\x1b[1;32mDONE\x1b[0m" in output.err
    assert "\x1b[1;31mRESULT\x1b[0m" in output.err
    assert "\x1b[1;36mfrontend\x1b[0m" in output.err
    assert "\x1b[1;36mdeploy/dev\x1b[0m" in output.err
    assert "\x1b[3;4mdev\x1b[23;24m" in output.err


def test_machine_readable_stdout_stays_uncolored_when_color_is_forced(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    print("a" * 40)

    assert capsys.readouterr().out == "a" * 40 + "\n"


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("Deploy frontend\n", 0, "Deploy frontend"),
        ("  Deploy\tfrontend\x00 now  \n", 0, "Deploy frontend now"),
        ("x" * 73 + "\n", 0, "x" * 71 + "…"),
        ("\n", 0, None),
        ("", 128, None),
    ],
)
def test_commit_subject_is_safe_bounded_and_optional(monkeypatch, stdout, returncode, expected):
    deploy_release.commit_subject.cache_clear()
    monkeypatch.setattr(
        deploy_release,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, returncode, stdout, ""),
    )

    assert deploy_release.commit_subject(Path("/repository"), "a" * 40) == expected


def test_describe_revision_includes_cached_subject_and_preserves_dry_prefix(tmp_path, monkeypatch):
    calls = []
    deploy_release.commit_subject.cache_clear()
    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)

    def run(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "Preview deployment\n", "")

    monkeypatch.setattr(deploy_release, "run", run)
    revision = "a" * 40

    assert deploy_release.describe_revision(revision) == "aaaaaaaaaaaa (Preview deployment)"
    assert deploy_release.describe_revision(f"dry:{revision}") == "dry:aaaaaaaaaaaa (Preview deployment)"
    assert deploy_release.describe_revision(None) == "none"
    assert len(calls) == 1


def test_status_and_ref_movement_include_commit_subjects(tmp_path, monkeypatch, capsys):
    revisions = {"deploy/dev": "a" * 40, "observed/dev": "b" * 40}
    subjects = {"a" * 40: "Prepare desired state", "b" * 40: "Observe frontend"}
    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", lambda ref, _output: revisions[ref])
    monkeypatch.setattr(deploy_release, "load_environment_specifications", lambda *_args: {})
    monkeypatch.setattr(deploy_release, "commit_subject", lambda _root, revision: subjects.get(revision))
    args = deploy_release.build_parser().parse_args(["status", "--environment", "dev"])

    args.handler(args)
    deploy_release.log_ref_advance(deploy_release.RefAdvance("desired", "deploy/dev", "a" * 40, "b" * 40))

    output = capsys.readouterr()
    assert output.out == ""
    assert "DESIRED  deploy/dev at aaaaaaaaaaaa (Prepare desired state)" in output.err
    assert "OBSERVED observed/dev at bbbbbbbbbbbb (Observe frontend)" in output.err
    assert "ADVANCE  deploy/dev aaaaaaaaaaaa (Prepare desired state) -> bbbbbbbbbbbb (Observe frontend)" in output.err


def test_reconciliation_statuses_identify_clean_ready_and_waiting_units(tmp_path):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    clean_unit = desired / "units/application-images.json"
    _write_json(clean_unit, _unit("application-images"))
    _write_json(desired / "units/aws-application.json", _unit("aws-application"))
    _write_json(
        observed / "units/application-images.json",
        receipt_document("terraform", "application-images", {"unitBlob": deploy_release.file_blob(clean_unit)}),
    )

    statuses = deploy_release.reconciliation_statuses(
        ["application-images", "aws-application", "frontend"], desired, observed
    )

    assert statuses == [
        ("application-images", "CLEAN", "observation matches desired state"),
        ("aws-application", "READY", "no observation receipt"),
        ("frontend", "WAIT", "desired inputs are not materialized"),
    ]


def test_desired_unit_rejects_an_incompatible_running_driver_version():
    unit = {
        "schema": 1,
        "name": "aws-application",
        "driver": "terraform",
        "source": {
            "path": "infra/deploy",
            "revision": "a" * 40,
            "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"] + 1,
        },
    }
    resource = deploy_release.RESOURCE_CATALOG.parse_unit(unit, profile="desired", expected_name="aws-application")

    with pytest.raises(deploy_release.OperationError, match="driver version"):
        deploy_release.require_unit(resource, "aws-application")


def test_duplicate_receipt_reuses_identical_semantic_result_without_writing(tmp_path, monkeypatch):
    existing = receipt_document(
        "terraform",
        "aws-application",
        {"unitBlob": "same"},
        {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://example.test"}},
        controller={"run": "old"},
    )

    def materialize(_ref, output):
        _write_json(output / "units/aws-application.json", existing)
        return "b" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", materialize)
    monkeypatch.setattr(
        deploy_release,
        "publish_tree",
        lambda *_args: (_ for _ in ()).throw(AssertionError("duplicate receipt was written")),
    )
    candidate = receipt_resource(
        "terraform",
        "aws-application",
        {"unitBlob": "same"},
        {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://example.test"}},
        controller={"run": "new"},
    )

    assert (
        deploy_release.publish_observation_cas(
            "observed/dev",
            "aws-application",
            candidate,
            _terraform_desired_resource(),
            {},
            "c" * 40,
        )
        == "b" * 40
    )


def test_duplicate_receipt_rejects_a_different_semantic_result(tmp_path, monkeypatch):
    existing = receipt_document(
        "terraform",
        "aws-application",
        {"unitBlob": "same"},
        {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://old.example.test"}},
    )

    def materialize(_ref, output):
        _write_json(output / "units/aws-application.json", existing)
        return "b" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", materialize)
    candidate = receipt_resource(
        "terraform",
        "aws-application",
        {"unitBlob": "same"},
        {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://new.example.test"}},
    )

    with pytest.raises(deploy_release.OperationError, match="different semantic result"):
        deploy_release.publish_observation_cas(
            "observed/dev",
            "aws-application",
            candidate,
            _terraform_desired_resource(),
            {},
            "c" * 40,
        )


def test_unit_change_explanation_classifies_causal_changes(monkeypatch):
    previous = {
        "driver": "terraform",
        "source": {
            "path": "infra/deploy",
            "revision": "a" * 40,
            "inputHash": "sha256:old",
            "driverVersion": 1,
        },
        "resolvedInputs": {"receipts": {"images": "old"}},
        "terraform": {"variables": {"environment": "old"}},
    }
    current = {
        "driver": "terraform",
        "source": {
            "path": "infra/deploy",
            "revision": "b" * 40,
            "inputHash": "sha256:new",
            "driverVersion": 2,
        },
        "resolvedInputs": {"receipts": {"images": "new"}},
        "terraform": {"variables": {"environment": "dev"}},
    }
    monkeypatch.setattr(
        deploy_release,
        "source_change_evidence",
        lambda *_args: (
            ("abc123 Use default API Gateway stage",),
            ("M\tinfra/deploy/main.tf",),
        ),
    )

    previous_resource = deploy_release.RESOURCE_CATALOG.parse_unit(
        {"name": "aws-application", **previous}, profile="desired", expected_name="aws-application"
    )
    current_resource = deploy_release.RESOURCE_CATALOG.parse_unit(
        {"name": "aws-application", **current}, profile="desired", expected_name="aws-application"
    )
    explanation = deploy_release.classify_unit_change(previous_resource, current_resource, "c" * 40)

    assert explanation.causes == (
        "reconciliation driver changed",
        "source inputs changed",
        "upstream observations changed: images",
        "unit specification changed",
    )
    assert explanation.commits == ("abc123 Use default API Gateway stage",)
    assert explanation.files == ("M\tinfra/deploy/main.tf",)
    assert explanation.specification_paths == ("/terraform/variables/environment",)


def test_reconciliation_explanation_is_visible_and_bounded_before_approval(tmp_path, monkeypatch, capsys):
    explanation = deploy_release.UnitChangeExplanation(
        previous_desired_revision="a" * 40,
        previous_source_revision="b" * 40,
        current_source_revision="c" * 40,
        causes=("source inputs changed",),
        commits=tuple(f"commit-{index}" for index in range(6)),
        files=("M\tinfra/deploy/main.tf",),
        specification_paths=(),
    )
    monkeypatch.setattr(
        deploy_release,
        "unit_change_explanation",
        lambda *_args: explanation,
    )

    deploy_release.log_reconciliation_status(
        "dev",
        [("aws-application", "READY", "desired inputs changed since its last receipt")],
        "d" * 40,
        tmp_path / "desired",
        tmp_path / "observed",
    )

    output = capsys.readouterr().err
    assert "LAST     desired aaaaaaaaaaaa; source bbbbbbbbbbbb" in output
    assert "CURRENT  desired dddddddddddd; source cccccccccccc" in output
    assert "CAUSE    source inputs changed" in output
    assert "COMMIT   commit-4" in output
    assert "COMMIT   commit-5" not in output
    assert "... and 1 more; use --verbose to show all" in output
    assert "FILE     M\tinfra/deploy/main.tf" in output


def test_convergence_plan_distinguishes_next_later_wait_and_clean():
    rows = deploy_release.convergence_plan_rows(
        [
            ("application-images", "CLEAN", "observation matches desired state"),
            ("aws-application", "READY", "desired inputs changed since its last receipt"),
            ("frontend", "READY", "desired inputs changed since its last receipt"),
            ("frontend-bundle", "WAIT", "desired inputs are not materialized"),
        ],
        ["application-images", "aws-application", "frontend-bundle", "frontend"],
    )

    assert rows == [
        ("application-images", "CLEAN", "observation matches desired state"),
        ("aws-application", "NEXT", "desired inputs changed since its last receipt"),
        ("frontend-bundle", "WAIT", "desired inputs are not materialized"),
        ("frontend", "LATER", "re-evaluate after aws-application"),
    ]


def test_compact_approval_card_shows_driver_change_evidence_and_write_boundary(tmp_path, monkeypatch, capsys):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    _write_json(
        desired / "units/aws-application.json",
        {
            "name": "aws-application",
            "driver": "terraform",
            "source": {"path": "infra/deploy", "revision": "a" * 40},
        },
    )
    explanation = deploy_release.UnitChangeExplanation(
        previous_desired_revision="a" * 40,
        previous_source_revision="b" * 40,
        current_source_revision="c" * 40,
        causes=("source inputs changed",),
        commits=("f4fa74b Consume extracted deployment action", "1234567 Older change"),
        files=("M\tinfra/deploy/README.md", "M\tinfra/deploy/main.tf"),
        specification_paths=(),
    )
    monkeypatch.setattr(deploy_release, "unit_change_explanation", lambda *_args: explanation)

    deploy_release.log_convergence_action(
        "aws-application",
        "desired inputs changed since its last receipt",
        "d" * 40,
        desired,
        observed,
        "observed/dev",
    )

    output = capsys.readouterr().err
    assert "Next action: aws-application" in output
    assert "DRIVER   terraform" in output
    assert "SOURCE   bbbbbbbbbbbb -> cccccccccccc" in output
    assert "CAUSE    source inputs changed" in output
    assert "COMMIT   f4fa74b Consume extracted deployment action (+1 more)" in output
    assert "FILE     M\tinfra/deploy/README.md (+1 more)" in output
    assert "WRITES   driver effects; receipt to observed/dev on success" in output


def test_status_allows_all_environment_and_single_unit_modes():
    all_environments = deploy_release.build_parser().parse_args(["status"])
    assert all_environments.environment is None
    assert all_environments.unit is None
    args = deploy_release.build_parser().parse_args(["status", "--environment", "staging"])
    assert args.environment == "staging"
    assert args.unit is None
    assert args.desired_ref is None
    assert args.observed_ref is None
    assert args.verbose is False

    unit = deploy_release.build_parser().parse_args(["status", "--environment", "staging", "--unit", "web"])
    assert unit.environment == "staging"
    assert unit.unit == "web"


def test_status_without_environment_delegates_to_environment_summary(monkeypatch):
    captured = []
    monkeypatch.setattr(deploy_release, "command_list_environments", lambda args: captured.append(args.json))

    args = deploy_release.build_parser().parse_args(["status"])
    args.handler(args)

    assert captured == [False]


def test_status_can_focus_on_one_unit(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", lambda _ref, output: output.mkdir() or "a" * 40)
    monkeypatch.setattr(
        deploy_release,
        "load_environment_specifications",
        lambda *_args: {"web": {}, "api": {}},
    )
    monkeypatch.setattr(
        deploy_release,
        "reconciliation_statuses",
        lambda *_args: [("api", "CLEAN", "observation matches desired state"), ("web", "READY", "inputs changed")],
    )
    monkeypatch.setattr(
        deploy_release,
        "log_reconciliation_status",
        lambda environment, statuses, *_args, **_kwargs: captured.append((environment, statuses)),
    )

    args = deploy_release.build_parser().parse_args(["status", "--environment", "dev", "--unit", "web"])
    args.handler(args)

    assert captured == [("dev", [("web", "READY", "inputs changed")])]


def test_environment_refs_use_project_defaults_and_allow_environment_and_cli_overrides(tmp_path):
    environment = tmp_path / "deployment/environments/staging/environment.json"
    _write_json(environment, {"schema": 1, "name": "staging"})

    assert deploy_release.deployment_refs(tmp_path, "staging") == (
        "gitopsctr/desired/staging",
        "gitopsctr/observed/staging",
    )

    _write_json(
        tmp_path / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {
                "environmentDefaults": {
                    "refs": {
                        "desired": "deployments/{environment}",
                        "observed": "observations/{environment}",
                    }
                }
            },
        },
    )
    assert deploy_release.deployment_refs(tmp_path, "staging") == (
        "deployments/staging",
        "observations/staging",
    )

    _write_json(
        environment,
        {
            "schema": 1,
            "name": "staging",
            "refs": {"desired": "releases/staging"},
        },
    )
    assert deploy_release.deployment_refs(tmp_path, "staging") == (
        "releases/staging",
        "observations/staging",
    )
    assert deploy_release.deployment_refs(
        tmp_path,
        "staging",
        desired_override="manual/desired",
        observed_override="manual/observed",
    ) == (
        "manual/desired",
        "manual/observed",
    )


def test_environment_refs_must_differ_after_project_template_expansion(tmp_path):
    environment = tmp_path / "deployment/environments/staging/environment.json"
    _write_json(environment, {"schema": 1, "name": "staging"})
    _write_json(
        tmp_path / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {
                "environmentDefaults": {
                    "refs": {
                        "desired": "state/{environment}",
                        "observed": "state/{environment}",
                    }
                }
            },
        },
    )

    with pytest.raises(deploy_release.OperationError, match="desired and observed refs must differ"):
        deploy_release.deployment_refs(tmp_path, "staging")


def test_candidate_ref_templates_use_project_and_environment_configuration(tmp_path):
    environment = tmp_path / "deployment/environments/staging/environment.json"
    _write_json(environment, {"schema": 1, "name": "staging"})

    assert deploy_release.candidate_ref_template(tmp_path, "staging") == ("gitopsctr/candidates/{environment}/{id}")
    assert deploy_release.resolve_candidate_ref(tmp_path, "staging", "promotion", "abc123") == (
        "gitopsctr/candidates/staging/abc123"
    )

    _write_json(
        tmp_path / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {"environmentDefaults": {"refs": {"candidate": "changes/{environment}/{operation}/{id}"}}},
        },
    )
    assert deploy_release.resolve_candidate_ref(tmp_path, "staging", "rollback", "def456") == (
        "changes/staging/rollback/def456"
    )

    _write_json(
        environment,
        {
            "schema": 1,
            "name": "staging",
            "refs": {"candidate": "candidate/{environment}"},
        },
    )
    assert deploy_release.resolve_candidate_ref(tmp_path, "staging", "promotion", "ignored") == ("candidate/staging")
    assert (
        deploy_release.resolve_candidate_ref(
            tmp_path,
            "staging",
            "promotion",
            "ignored",
            "manual/candidate",
        )
        == "manual/candidate"
    )


def test_environment_candidate_ref_template_rejects_unknown_placeholders(tmp_path):
    _write_json(
        tmp_path / "deployment/environments/staging/environment.json",
        {
            "schema": 1,
            "name": "staging",
            "refs": {"candidate": "candidate/{environment}/{unknown}"},
        },
    )

    with pytest.raises(deploy_release.OperationError, match="candidate"):
        deploy_release.load_environment(tmp_path, "staging")


def test_candidate_identifier_covers_operation_target_context_and_tree(tmp_path):
    candidate = tmp_path / "candidate"
    _write_json(candidate / "units/application.json", {"value": "one"})
    arguments = ("dev", candidate, "gitopsctr/desired/dev", "a" * 40, {"source": "main"})

    first = deploy_release.candidate_identifier("promotion", *arguments)

    assert first == deploy_release.candidate_identifier("promotion", *arguments)
    assert first != deploy_release.candidate_identifier("rollback", *arguments)
    assert first != deploy_release.candidate_identifier(
        "promotion", "dev", candidate, "gitopsctr/desired/dev", "b" * 40, {"source": "main"}
    )
    _write_json(candidate / "units/application.json", {"value": "two"})
    assert first != deploy_release.candidate_identifier("promotion", *arguments)


def test_occupied_candidate_slot_reuses_only_the_same_proposal(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    desired_document = deploy_release.serialize_unit_document(_terraform_desired_resource("application"))
    _write_json(candidate / "units/application.json", desired_document)
    target_revision = "b" * 40
    existing_revision = "c" * 40
    outcome = deploy_release.ChangeRequestResult(status="existing", url="https://github.example/pull/1")

    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: existing_revision)

    def materialize(_revision, output):
        _write_json(output / "units/application.json", desired_document)

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)

    def fake_git(*args, **_kwargs):
        if args[0] == "check-ref-format":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, target_revision + "\n", "")
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, "Candidate message\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(deploy_release, "git", fake_git)
    monkeypatch.setattr(deploy_release, "ensure_change_request", lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(
        deploy_release,
        "publish_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("candidate was republished")),
    )

    revision, actual = deploy_release.publish_change_candidate(
        candidate,
        "gitopsctr/candidates/dev",
        "gitopsctr/desired/dev",
        target_revision,
        "Candidate message",
        "Candidate",
        "Candidate body",
    )

    assert (revision, actual) == (existing_revision, outcome)

    with pytest.raises(deploy_release.OperationError, match="occupied by a different proposal"):
        deploy_release.publish_change_candidate(
            candidate,
            "gitopsctr/candidates/dev",
            "gitopsctr/desired/dev",
            target_revision,
            "Different message",
            "Candidate",
            "Candidate body",
        )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, "none"), ("none", "none"), ("pullRequest", "pullRequest")],
)
def test_change_gate_is_explicit_and_defaults_to_none(tmp_path, configured, expected):
    environment = {"schema": 1, "name": "prod"}
    if configured is not None:
        environment["changeGate"] = configured
    _write_json(
        tmp_path / "deployment/environments/prod/environment.json",
        environment,
    )

    assert deploy_release.change_gate(tmp_path, "prod") == expected


def test_change_gate_rejects_unknown_modes(tmp_path):
    _write_json(
        tmp_path / "deployment/environments/prod/environment.json",
        {"schema": 1, "name": "prod", "changeGate": "review"},
    )

    with pytest.raises(deploy_release.OperationError, match="changeGate"):
        deploy_release.load_environment(tmp_path, "prod")


def test_advance_source_revision_is_required_only_for_source_tracked_environments(tmp_path, monkeypatch):
    _write_json(
        tmp_path / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(
        tmp_path / "deployment/environments/prod/environment.json",
        {
            "schema": 1,
            "name": "prod",
            "promotion": {"allowedSources": ["staging"]},
        },
    )
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 0, "a" * 40 + "\n", ""),
    )

    assert deploy_release.resolve_advance_source_revision(tmp_path, "dev", "HEAD") == "a" * 40
    with pytest.raises(deploy_release.OperationError, match="requires --source-revision"):
        deploy_release.resolve_advance_source_revision(tmp_path, "dev", None)
    assert deploy_release.resolve_advance_source_revision(tmp_path, "prod", None) is None
    with pytest.raises(deploy_release.OperationError, match="does not accept --source-revision"):
        deploy_release.resolve_advance_source_revision(tmp_path, "prod", "HEAD")


def _unit(name: str, inputs: dict | None = None) -> dict:
    return {
        "schema": 1,
        "name": name,
        "driver": "terraform",
        "source": {"path": "infra/deploy"},
        **({"inputs": inputs} if inputs else {}),
    }


def _unit_resource(name: str, inputs: dict | None = None):
    return deploy_release.parse_authored_unit_document(_unit(name, inputs), name)


def test_convergence_scope_includes_only_transitive_observation_dependencies():
    specifications = {
        "application-images": _unit_resource("application-images"),
        "aws-application": _unit_resource(
            "aws-application",
            {
                "image": {
                    "fromReceipt": {"unit": "application-images", "pointer": "/image"},
                }
            },
        ),
        "frontend-bundle": _unit_resource("frontend-bundle"),
        "frontend": _unit_resource(
            "frontend",
            {
                "bundle": {
                    "fromReceipt": {"unit": "frontend-bundle", "pointer": "/bundle"},
                },
                "api": {
                    "fromReceipt": {"unit": "aws-application", "pointer": "/api"},
                },
            },
        ),
        "unrelated": _unit_resource("unrelated"),
    }

    selection = deploy_release.convergence_scope(specifications, ["frontend"])
    targets, scope = selection.targets, selection.scope

    assert targets == ("frontend",)
    assert scope == (
        "application-images",
        "aws-application",
        "frontend",
        "frontend-bundle",
    )
    assert deploy_release.convergence_order(specifications, scope).index(
        "application-images"
    ) < deploy_release.convergence_order(specifications, scope).index("aws-application")
    graph = deploy_release.dependency_graph(specifications, scope)
    assert graph.render_tree("frontend") == (
        "frontend",
        "├── aws-application",
        "│   └── application-images",
        "└── frontend-bundle",
    )


def test_dependencies_parser_defaults_to_head_and_accepts_repeated_units():
    args = deploy_release.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--unit",
            "frontend",
            "--unit",
            "aws-application",
        ]
    )

    assert args.source_revision == "HEAD"
    assert args.unit == ["frontend", "aws-application"]
    assert args.depth is None
    assert args.list is False
    assert args.json is False

    with pytest.raises(SystemExit):
        deploy_release.build_parser().parse_args(
            [
                "dependencies",
                "--environment",
                "dev",
                "--unit",
                "frontend",
                "--list",
                "--json",
            ]
        )

    with pytest.raises(deploy_release.OperationError, match="--depth"):
        deploy_release.convergence_scope({"frontend": _unit_resource("frontend")}, ["frontend"], max_depth=-1)


def test_dependencies_command_prints_the_resolved_tree(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 0, "a" * 40 + "\n", ""),
    )

    def materialize(_revision: str, output: Path):
        _write_json(
            output / "deployment/environments/dev/environment.json",
            {"schema": 1, "name": "dev"},
        )
        _write_json(
            output / "deployment/environments/dev/units/base.json",
            _unit("base"),
        )
        _write_json(
            output / "deployment/environments/dev/units/producer.json",
            _unit(
                "producer",
                {
                    "value": {
                        "fromReceipt": {"unit": "base", "pointer": "/value"},
                    }
                },
            ),
        )
        _write_json(
            output / "deployment/environments/dev/units/consumer.json",
            _unit(
                "consumer",
                {
                    "value": {
                        "fromReceipt": {"unit": "producer", "pointer": "/value"},
                    }
                },
            ),
        )

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    args = deploy_release.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--unit",
            "consumer",
        ]
    )

    args.handler(args)

    assert capsys.readouterr().out == "consumer\n└── producer\n    └── base\n"

    list_args = deploy_release.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--unit",
            "consumer",
            "--list",
            "--depth",
            "1",
        ]
    )
    list_args.handler(list_args)
    assert capsys.readouterr().out == "producer\nconsumer\n"

    json_args = deploy_release.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--unit",
            "consumer",
            "--json",
        ]
    )
    json_args.handler(json_args)
    document = json.loads(capsys.readouterr().out)
    assert document["targets"] == ["consumer"]
    assert document["units"] == [
        {"name": "base", "dependencies": []},
        {"name": "producer", "dependencies": ["base"]},
        {"name": "consumer", "dependencies": ["producer"]},
    ]


def test_converge_parser_accepts_repeated_targets_and_optional_guard():
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--unit",
            "frontend",
            "--unit",
            "aws-application",
            "--fail-on-repeat",
            "--max-steps",
            "7",
            "--yes",
            "--verbose",
        ]
    )

    assert args.unit == ["frontend", "aws-application"]
    assert args.fail_on_repeat is True
    assert args.max_steps == 7
    assert args.yes is True
    assert args.verbose is True

    promoted = deploy_release.build_parser().parse_args(["converge", "--environment", "prod", "--yes"])
    assert promoted.source_revision is None


def _install_convergence_simulation(
    monkeypatch,
    tmp_path,
    specifications: dict[str, dict],
    desired_units: list[dict[str, str]],
):
    source_revision = "a" * 40
    desired_revisions = [chr(ord("b") + index) * 40 for index in range(len(desired_units))]
    state = {
        "desired_index": -1,
        "desired": "0" * 40,
        "observed": None,
        "receipts": {},
        "reconciled": [],
        "advance_calls": 0,
        "advance_summarize": [],
    }

    def fake_git(*args, **_kwargs):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, source_revision + "\n", "")

    monkeypatch.setattr(deploy_release, "git", fake_git)

    def write_source(output: Path):
        _write_json(
            output / "deployment/environments/dev/environment.json",
            {"schema": 1, "name": "dev"},
        )
        for unit_name, specification in specifications.items():
            _write_json(
                output / f"deployment/environments/dev/units/{unit_name}.json",
                specification,
            )

    def materialize(revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if revision == source_revision:
            write_source(output)
            return
        index = desired_revisions.index(revision)
        for unit_name, blob in desired_units[index].items():
            specification = specifications[unit_name]
            desired = {
                **specification,
                "source": {
                    **specification["source"],
                    "revision": source_revision,
                    "driverVersion": deploy_release.DRIVER_VERSIONS[specification["driver"]],
                },
                "terraform": {"variables": {"blob": blob}},
            }
            if "inputs" in specification:
                desired["inputs"] = {"value": blob}
            _write_json(output / f"units/{unit_name}.json", desired)

    def observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/dev":
            return state["desired"]
        for unit_name, blob in state["receipts"].items():
            _write_json(
                output / f"units/{unit_name}.json",
                receipt_document(
                    specifications[unit_name]["driver"],
                    unit_name,
                    {"unitBlob": blob},
                    {"applied": {"sourceRevision": source_revision}, "outputs": {}}
                    if specifications[unit_name]["driver"] == "terraform"
                    else {},
                ),
            )
        return state["observed"]

    def fetch_ref(ref: str):
        return state["desired"] if ref == "deploy/dev" else state["observed"]

    def advance(*_args, **kwargs):
        state["advance_calls"] += 1
        state["advance_summarize"].append(kwargs.get("summarize"))
        next_index = min(state["desired_index"] + 1, len(desired_units) - 1)
        changed = next_index != state["desired_index"]
        state["desired_index"] = next_index
        state["desired"] = desired_revisions[next_index]
        return state["desired"], changed

    def reconcile(args):
        current = desired_units[state["desired_index"]]
        assert args.desired_revision == state["desired"]
        assert args.unit in current
        state["reconciled"].append(args.unit)
        state["receipts"][args.unit] = current[args.unit]
        state["observed"] = str(len(state["reconciled"])) * 40
        return True

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", fetch_ref)
    monkeypatch.setattr(deploy_release, "advance_desired", advance)
    monkeypatch.setattr(deploy_release, "command_reconcile", reconcile)
    monkeypatch.setattr(
        deploy_release,
        "file_blob",
        lambda path: deploy_release.load_json(path)["terraform"]["variables"]["blob"],
    )
    return state


def test_converge_runs_dependency_first_and_ignores_unselected_unit(tmp_path, monkeypatch, capsys):
    specifications = {
        "producer": _unit("producer"),
        "consumer": _unit(
            "consumer",
            {
                "value": {
                    "fromReceipt": {"unit": "producer", "pointer": "/value"},
                }
            },
        ),
        "unrelated": _unit("unrelated"),
    }
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        specifications,
        [
            {"producer": "producer-v1"},
            {"producer": "producer-v1", "consumer": "consumer-v1"},
        ],
    )
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--unit",
            "consumer",
            "--yes",
        ]
    )

    args.handler(args)

    assert state["reconciled"] == ["producer", "consumer"]
    assert state["advance_calls"] == 3
    assert state["advance_summarize"] == [False, False, False]
    output = capsys.readouterr().err
    assert "SCOPE    consumer, producer" in output
    assert "NEXT     producer: no observation receipt" in output
    assert "WAIT     consumer: desired inputs are not materialized" in output
    assert "RUN      producer" in output
    assert "RUN      consumer" in output
    assert "RESULT   CLEAN: 2/2 units" in output
    assert "DEPEND" not in output
    assert "WARN" not in output


def test_converge_warns_once_for_uncommitted_source_changes(tmp_path, monkeypatch, capsys):
    _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        {"application": _unit("application")},
        [{"application": "v1"}],
    )
    monkeypatch.setattr(deploy_release, "working_tree_has_uncommitted_changes", lambda: True)
    args = deploy_release.build_parser().parse_args(
        ["converge", "--environment", "dev", "--source-revision", "HEAD", "--yes"]
    )

    args.handler(args)

    assert capsys.readouterr().err.count("WARN") == 1


def test_converge_requires_approval_before_each_reconciliation(tmp_path, monkeypatch, capsys):
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        {"application": _unit("application")},
        [{"application": "v1"}],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("no\n"))
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match="was not approved"):
        args.handler(args)

    assert state["reconciled"] == []
    output = capsys.readouterr().err
    assert "Next action: application" in output
    assert "APPROVE  Continue with application? [y/N]" in output
    assert "RESULT   FAILED: reconciliation of application was not approved" in output


def test_converge_verbose_preserves_dependency_and_reconciliation_trace(tmp_path, monkeypatch, capsys):
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        {"application": _unit("application")},
        [{"application": "v1"}],
    )
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--yes",
            "--verbose",
        ]
    )

    args.handler(args)

    assert state["reconciled"] == ["application"]
    output = capsys.readouterr().err
    assert "DEPEND   application: none" in output
    assert "Reconciliation status for dev" in output
    assert "Convergence step 1 (limit 2): application" in output
    assert "Convergence summary for dev" in output


def test_converge_repeat_guard_fails_before_running_same_unit_again(tmp_path, monkeypatch, capsys):
    specifications = {"bootstrap": _unit("bootstrap")}
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        specifications,
        [{"bootstrap": "v1"}, {"bootstrap": "v2"}],
    )
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--fail-on-repeat",
            "--yes",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match="repeated ready unit.*bootstrap"):
        args.handler(args)

    assert state["reconciled"] == ["bootstrap"]
    output = capsys.readouterr().err
    assert "RESULT   FAILED: convergence heuristic" in output


def test_converge_allows_a_repeated_unit_by_default(tmp_path, monkeypatch):
    specifications = {"bootstrap": _unit("bootstrap")}
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        specifications,
        [{"bootstrap": "v1"}, {"bootstrap": "v2"}],
    )
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--yes",
        ]
    )

    args.handler(args)

    assert state["reconciled"] == ["bootstrap", "bootstrap"]


def test_converge_without_repeat_guard_is_still_bounded(tmp_path, monkeypatch, capsys):
    specifications = {"bootstrap": _unit("bootstrap")}
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        specifications,
        [{"bootstrap": "v1"}, {"bootstrap": "v2"}, {"bootstrap": "v3"}],
    )
    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--max-steps",
            "2",
            "--yes",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match="within 2 reconciliation steps"):
        args.handler(args)

    assert state["reconciled"] == ["bootstrap", "bootstrap"]
    assert "RESULT   FAILED" in capsys.readouterr().err


def test_converge_exits_at_unmerged_promotion_review_gate(tmp_path, monkeypatch, capsys):
    specification = _unit(
        "application",
        {
            "image": {
                "fromPromotion": {"unit": "application", "pointer": "/image"},
            }
        },
    )
    state = _install_convergence_simulation(
        monkeypatch,
        tmp_path,
        {"application": specification},
        [{"application": "v1"}],
    )

    args = deploy_release.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
        ]
    )
    with pytest.raises(deploy_release.OperationError, match="review gate"):
        args.handler(args)

    assert state["advance_calls"] == 0
    assert state["reconciled"] == []
    output = capsys.readouterr().err
    assert "REVIEW" in output
    assert "RESULT   FAILED: review gate" in output


def test_promoted_converge_uses_merged_specification_without_source_revision(tmp_path, monkeypatch, capsys):
    reviewed = "a" * 40
    desired_revision = "d" * 40
    promotion_root = tmp_path / "promotion"
    promotion_root.mkdir()
    context = deploy_release.PromotionContext(
        source_environment="staging",
        desired_ref="deploy/staging",
        desired_revision="b" * 40,
        observed_ref="observed/staging",
        observed_revision="c" * 40,
        specification_revision=reviewed,
        desired_root=promotion_root,
    )

    def materialize(revision, output):
        output.mkdir(parents=True, exist_ok=True)
        if revision == reviewed:
            _write_json(
                output / "deployment/environments/prod/environment.json",
                {
                    "schema": 1,
                    "name": "prod",
                    "promotion": {"allowedSources": ["staging"]},
                },
            )
            _write_json(
                output / "deployment/environments/prod/units/application.json",
                {
                    **_unit(
                        "application",
                        {
                            "image": {
                                "fromPromotion": {"unit": "application", "pointer": "/image"},
                            }
                        },
                    )
                },
            )
        elif revision == desired_revision:
            _write_json(
                output / "units/application.json",
                {
                    **_unit("application"),
                    "source": {
                        "path": "infra/deploy",
                        "revision": reviewed,
                        "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
                    },
                    "terraform": {"variables": {"blob": "release-v1"}},
                },
            )

    def observed_tree(ref, output):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/prod":
            return desired_revision
        _write_json(
            output / "units/application.json",
            receipt_document("terraform", "application", {"unitBlob": "release-v1"}),
        )
        return "e" * 40

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda ref: "e" * 40)
    monkeypatch.setattr(deploy_release, "load_promotion_context", lambda *_args: context)
    monkeypatch.setattr(
        deploy_release,
        "file_blob",
        lambda path: deploy_release.load_json(path)["terraform"]["variables"]["blob"],
    )

    def advance(_environment, source_revision, *_args, **_kwargs):
        assert source_revision is None
        return desired_revision, False

    monkeypatch.setattr(deploy_release, "advance_desired", advance)
    args = deploy_release.build_parser().parse_args(["converge", "--environment", "prod", "--yes"])

    args.handler(args)

    output = capsys.readouterr().err
    assert "RESULT   CLEAN" in output
    assert "WARN" not in output
