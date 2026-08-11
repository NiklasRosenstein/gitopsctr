"""Acceptance coverage for promoted Stack artifact imports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.contracts import ArtifactImport, DesiredSource, StackTemplatePromotionReference
from gitopsctr.resources import DesiredLifecycle, ResourceMetadata
from tests.conftest import receipt_resource


@dataclass(frozen=True)
class ImportFixture:
    source: Path
    desired: Path
    observed: Path
    promotion: cli.PromotionContext
    current: Path
    target_observed: Path


def _repository(root: Path) -> None:
    root.mkdir()
    (root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "preview-artifacts"},
                "spec": {},
            }
        )
    )
    (root / "deployment/stack-templates").mkdir(parents=True)
    (root / "deployment/stack-templates/application.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "application"},
                "spec": {
                    "unitTemplates": {
                        "image": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "OciImages",
                            "spec": {"source": {"path": "."}},
                        },
                        "deploy": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {
                                    "variables": {
                                        "image": {
                                            "fromArtifact": {
                                                "unit": "image",
                                                "name": "containers",
                                                "apiVersion": "artifact.gitopsctr.io/v1",
                                                "kind": "ContainerImages",
                                                "pointer": "/images/application/uri",
                                            }
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
            }
        )
    )
    for environment in ("dev", "staging", "production"):
        environment_root = root / "deployment/environments" / environment
        (environment_root / "stacks").mkdir(parents=True)
        (environment_root / "environment.json").write_text(
            json.dumps(
                {
                    "apiVersion": "gitopsctr.io/v1",
                    "kind": "Environment",
                    "metadata": {"name": environment},
                    "spec": {},
                }
            )
        )


def _write_stack(root: Path, environment: str, document: dict[str, object]) -> None:
    name = str(document["metadata"]["name"])
    (root / "deployment/environments" / environment / "stacks" / f"{name}.json").write_text(json.dumps(document))


def _container_images(source_revision: str) -> dict[str, object]:
    return {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": "application--image",
            "driverVersion": 1,
            "sourceRevision": source_revision,
            "inputHashVersion": 1,
            "inputHash": "sha256:" + "b" * 64,
        },
        "images": {"application": {"uri": "registry.example/application@sha256:" + "c" * 64}},
    }


def _write_source_observation(
    source_root: Path,
    desired_root: Path,
    observed_root: Path,
    projection: cli.StackProjection,
    source_revision: str,
) -> None:
    image = projection.generated_units["application--image"]
    driver = cli.UNIT_DRIVERS["oci-images"]
    resolved = driver.resolve_unit(
        image.spec,
        cli.UnitResolutionContext(
            source=DesiredSource(
                path=".",
                revision=source_revision,
                inputHash="sha256:" + "b" * 64,
                driverVersion=driver.version,
            ),
            resolve_template=lambda _value, _pointer: (_ for _ in ()).throw(AssertionError("not called")),
        ),
    ).unit
    image_path = desired_root / "units/application--image.json"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path = cli.write_desired_candidate_unit(
        image_path,
        image.with_spec(resolved).with_metadata(
            ResourceMetadata(
                name="application--image",
                uid="source-image-unit",
                lifecycle=DesiredLifecycle(owner=projection.owners["application--image"]),
            )
        ),
        source_root,
    )
    observed_root.mkdir()
    descriptors = cli.write_artifact_documents(
        observed_root,
        "application--image",
        "oci-images",
        {"containers": _container_images(source_revision)},
    )
    receipt = receipt_resource(
        "oci-images",
        "application--image",
        {"unitBlob": cli.file_blob(image_path)},
        artifacts=descriptors,
    )
    cli.write_document(
        observed_root / "units/application--image.json",
        cli.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=cli.DocumentFormat.JSON,
    )


def _setup_import_fixture(tmp_path: Path, monkeypatch) -> ImportFixture:
    source = tmp_path / "source"
    _repository(source)
    _write_stack(
        source,
        "dev",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {"template": "application"},
        },
    )
    _write_stack(
        source,
        "staging",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {
                "template": {"name": "application", "source": {"fromPromotion": {"stack": "application"}}},
                "units": ["deploy"],
                "artifactImports": [
                    {
                        "unit": "image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "fromPromotion": {"stack": "application"},
                    }
                ],
            },
        },
    )
    source_revision = "a" * 40
    desired = tmp_path / "dev-desired"
    projection = cli.project_stack_resources(source, "dev", source_revision, desired, source)
    observed = tmp_path / "dev-observed"
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", source)
    monkeypatch.setattr(cli, "file_blob", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    _write_source_observation(source, desired, observed, projection, source_revision)
    current = tmp_path / "empty-current"
    current.mkdir()
    target_observed = tmp_path / "staging-observed"
    target_observed.mkdir()
    return ImportFixture(
        source=source,
        desired=desired,
        observed=observed,
        promotion=cli.PromotionContext(
            source_environment="dev",
            desired_ref="deploy/dev",
            desired_revision=source_revision,
            observed_ref="observed/dev",
            observed_revision="d" * 40,
            specification_revision=source_revision,
            desired_root=desired,
            observed_root=observed,
        ),
        current=current,
        target_observed=target_observed,
    )


def _advance_import_fixture(fixture: ImportFixture, output: Path):
    return cli.build_desired_candidate(
        "staging",
        fixture.source,
        "e" * 40,
        fixture.current,
        fixture.target_observed,
        None,
        output,
        promotion=fixture.promotion,
        verbose=False,
    )


@pytest.mark.parametrize(
    "failure",
    (
        "stack-uid",
        "unit-owner-uid",
        "missing-unit",
        "stale-receipt",
        "wrong-gvk",
        "wrong-digest",
        "missing-artifact",
        "unmatched-import",
    ),
)
def test_promoted_artifact_import_rejects_invalid_lineage(tmp_path: Path, monkeypatch, failure: str):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    if failure == "stack-uid":
        path = cli.document_candidates(fixture.desired / "stacks", "application")[0]
        document = cli.RESOURCE_CATALOG.load_document(path)
        document["metadata"]["uid"] = "different-stack-uid"
        path.write_text(json.dumps(document))
    elif failure == "unit-owner-uid":
        path = cli.unit_document_path(fixture.desired, "application--image")
        document = cli.RESOURCE_CATALOG.load_document(path)
        document["metadata"]["lifecycle"]["owner"]["uid"] = "different-stack-uid"
        path.write_text(json.dumps(document))
    elif failure == "missing-unit":
        cli.unit_document_path(fixture.desired, "application--image").unlink()
    elif failure == "stale-receipt":
        cli.unit_document_path(fixture.observed, "application--image").unlink()
    elif failure in {"wrong-gvk", "wrong-digest"}:
        path = cli.unit_document_path(fixture.observed, "application--image")
        document = cli.RESOURCE_CATALOG.load_document(path)
        artifact = document["status"]["artifacts"]["containers"]
        artifact["kind"] = "WrongArtifact" if failure == "wrong-gvk" else artifact["kind"]
        artifact["digest"] = "sha256:" + "0" * 64 if failure == "wrong-digest" else artifact["digest"]
        path.write_text(json.dumps(document))
    elif failure == "missing-artifact":
        cli.document_candidates(fixture.observed / "artifacts/application--image", "containers")[0].unlink()
    elif failure == "unmatched-import":
        path = fixture.source / "deployment/environments/staging/stacks/application.json"
        document = json.loads(path.read_text())
        document["spec"]["artifactImports"][0]["unit"] = "missing"
        path.write_text(json.dumps(document))

    try:
        result = _advance_import_fixture(fixture, tmp_path / "failed-candidate")
    except (cli.ReferenceUnavailable, cli.OperationError):
        return
    assert result.blocked
    assert not cli.document_candidates(tmp_path / "failed-candidate/units", "application--deploy")


def test_promoted_artifact_import_records_lineage_and_reconciles_from_desired(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source"
    _repository(source)
    _write_stack(
        source,
        "dev",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {"template": "application"},
        },
    )
    _write_stack(
        source,
        "staging",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {
                "template": {"name": "application", "source": {"fromPromotion": {"stack": "application"}}},
                "units": ["deploy"],
                "artifactImports": [
                    {
                        "unit": "image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "fromPromotion": {"stack": "application"},
                    }
                ],
            },
        },
    )
    source_revision = "a" * 40
    dev_desired = tmp_path / "dev-desired"
    dev_projection = cli.project_stack_resources(source, "dev", source_revision, dev_desired, source)
    observed = tmp_path / "dev-observed"
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", source)
    monkeypatch.setattr(cli, "file_blob", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    _write_source_observation(source, dev_desired, observed, dev_projection, source_revision)

    promotion = cli.PromotionContext(
        source_environment="dev",
        desired_ref="deploy/dev",
        desired_revision=source_revision,
        observed_ref="observed/dev",
        observed_revision="d" * 40,
        specification_revision=source_revision,
        desired_root=dev_desired,
        observed_root=observed,
    )
    staging = tmp_path / "staging-desired"
    current = tmp_path / "empty-current"
    current.mkdir()
    staging_observed = tmp_path / "staging-observed"
    staging_observed.mkdir()
    result = cli.build_desired_candidate(
        "staging",
        source,
        "e" * 40,
        current,
        staging_observed,
        None,
        staging,
        promotion=promotion,
        verbose=False,
    )

    assert result.blocked == {}
    deploy = cli.load_desired_unit(cli.unit_document_path(staging, "application--deploy"), "application--deploy")
    assert deploy.spec.terraform is not None
    assert deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["image"].endswith("c" * 64)
    stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(cli.document_candidates(staging / "stacks", "application")[0]),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, cli.DesiredStackSpec)
    assert stack.spec.resolvedArtifactImports is not None
    assert list(stack.spec.resolvedArtifactImports) == ["image/containers"]
    evidence = stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence.sourceStack == "application"
    assert evidence.sourceUnit == "application--image"
    assert evidence.sourceDesiredRevision == source_revision
    assert evidence.sourceObservedRevision == "d" * 40
    assert evidence.targetStackUid == stack.metadata.uid

    specifications, _ = cli.load_convergence_specifications(
        source, "staging", staging, "f" * 40, tmp_path / "staging-reconcile"
    )
    assert specifications["application--deploy"].spec.terraform.variables["image"].endswith("c" * 64)


def test_promoted_artifact_import_chains_from_staging_to_production(tmp_path: Path, monkeypatch):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    staging = tmp_path / "staging-desired"
    _advance_import_fixture(fixture, staging)

    _write_stack(
        fixture.source,
        "production",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {
                "template": {"name": "application", "source": {"fromPromotion": {"stack": "application"}}},
                "units": ["deploy"],
                "artifactImports": [
                    {
                        "unit": "image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "fromPromotion": {"stack": "application"},
                    }
                ],
            },
        },
    )
    current = tmp_path / "production-current"
    current.mkdir()
    observed = tmp_path / "production-observed"
    observed.mkdir()
    promotion = cli.PromotionContext(
        source_environment="staging",
        desired_ref="deploy/staging",
        desired_revision="e" * 40,
        observed_ref="observed/staging",
        observed_revision=None,
        specification_revision="e" * 40,
        desired_root=staging,
    )
    result = cli.build_desired_candidate(
        "production",
        fixture.source,
        "f" * 40,
        current,
        observed,
        None,
        tmp_path / "production-desired",
        promotion=promotion,
        verbose=False,
    )

    assert result.blocked == {}
    production = tmp_path / "production-desired"
    deploy = cli.load_desired_unit(cli.unit_document_path(production, "application--deploy"), "application--deploy")
    assert deploy.spec.terraform is not None
    assert deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["image"].endswith("c" * 64)
    stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(cli.document_candidates(production / "stacks", "application")[0]),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, cli.DesiredStackSpec)
    assert stack.spec.resolvedArtifactImports is not None
    evidence = stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence.sourceDesiredRevision == "a" * 40
    assert evidence.targetStackUid == stack.metadata.uid


def test_promoted_artifact_import_rejects_ambiguous_imports(tmp_path: Path):
    imports = (
        ArtifactImport(
            unit="image",
            name="containers",
            apiVersion="artifact.gitopsctr.io/v1",
            kind="ContainerImages",
            fromPromotion=StackTemplatePromotionReference(stack="application"),
        ),
        ArtifactImport(
            unit="application--image",
            name="containers",
            apiVersion="artifact.gitopsctr.io/v1",
            kind="ContainerImages",
            fromPromotion=StackTemplatePromotionReference(stack="application"),
        ),
    )
    with pytest.raises(cli.OperationError, match="ambiguous"):
        cli.resolve_template(
            {
                "image": {
                    "fromArtifact": {
                        "unit": "application--image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "pointer": "/images/application/uri",
                    }
                }
            },
            tmp_path / "candidate",
            tmp_path / "observed",
            None,
            artifact_imports=imports,
        )
