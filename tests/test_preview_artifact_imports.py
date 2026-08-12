"""Acceptance coverage for promoted Stack artifact imports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import ArtifactImport, DesiredSource, StackTemplatePromotionReference
from gitopsctr.resources import DesiredLifecycle, ResourceMetadata
from tests.conftest import receipt_resource


@dataclass(frozen=True)
class ImportFixture:
    source: Path
    desired: Path
    observed: Path
    promotion: controller.PromotionContext
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
                "spec": {"effectLease": None},
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
    projection: controller.StackProjection,
    source_revision: str,
) -> None:
    image = projection.generated_units["application--image"]
    driver = controller.UNIT_DRIVERS["oci-images"]
    resolved = driver.resolve_unit(
        image.spec,
        controller.UnitResolutionContext(
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
    image_path = controller.write_desired_candidate_unit(
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
    descriptors = controller.write_artifact_documents(
        observed_root,
        "application--image",
        "oci-images",
        {"containers": _container_images(source_revision)},
    )
    receipt = receipt_resource(
        "oci-images",
        "application--image",
        {"unitBlob": controller.file_blob(image_path)},
        artifacts=descriptors,
    )
    controller.write_document(
        observed_root / "units/application--image.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
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
    projection = controller.project_stack_resources(source, "dev", source_revision, desired, source)
    observed = tmp_path / "dev-observed"
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    monkeypatch.setattr(controller, "file_blob", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    _write_source_observation(source, desired, observed, projection, source_revision)
    current = tmp_path / "empty-current"
    current.mkdir()
    target_observed = tmp_path / "staging-observed"
    target_observed.mkdir()
    return ImportFixture(
        source=source,
        desired=desired,
        observed=observed,
        promotion=controller.PromotionContext(
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
    return controller.build_desired_candidate(
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
    ("failure", "expected"),
    {
        "stack-uid": "invalid Stack owner fence",
        "unit-owner-uid": "invalid Stack owner fence",
        "missing-unit": "promoted source Unit is unavailable",
        "stale-receipt": "promoted artifact receipt is stale",
        "wrong-gvk": "was expected",
        "wrong-digest": "does not match its digest",
        "missing-artifact": "artifact files do not match",
        "unmatched-import": "imports an artifact from unknown Unit template",
    }.items(),
)
def test_promoted_artifact_import_rejects_invalid_lineage(tmp_path: Path, monkeypatch, failure: str, expected: str):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    if failure == "stack-uid":
        path = controller.document_candidates(fixture.desired / "stacks", "application")[0]
        document = controller.RESOURCE_CATALOG.load_document(path)
        document["metadata"]["uid"] = "different-stack-uid"
        path.write_text(json.dumps(document))
    elif failure == "unit-owner-uid":
        path = controller.unit_document_path(fixture.desired, "application--image")
        document = controller.RESOURCE_CATALOG.load_document(path)
        document["metadata"]["lifecycle"]["owner"]["uid"] = "different-stack-uid"
        path.write_text(json.dumps(document))
    elif failure == "missing-unit":
        controller.unit_document_path(fixture.desired, "application--image").unlink()
    elif failure == "stale-receipt":
        path = controller.unit_document_path(fixture.observed, "application--image")
        document = controller.RESOURCE_CATALOG.load_document(path)
        document["spec"]["desired"]["unitBlob"] = "stale-unit-blob"
        path.write_text(json.dumps(document))
    elif failure in {"wrong-gvk", "wrong-digest"}:
        path = controller.unit_document_path(fixture.observed, "application--image")
        document = controller.RESOURCE_CATALOG.load_document(path)
        artifact = document["status"]["artifacts"]["containers"]
        artifact["kind"] = "WrongArtifact" if failure == "wrong-gvk" else artifact["kind"]
        artifact["digest"] = "sha256:" + "0" * 64 if failure == "wrong-digest" else artifact["digest"]
        path.write_text(json.dumps(document))
    elif failure == "missing-artifact":
        controller.document_candidates(fixture.observed / "artifacts/application--image", "containers")[0].unlink()
    elif failure == "unmatched-import":
        path = fixture.source / "deployment/environments/staging/stacks/application.json"
        document = json.loads(path.read_text())
        document["spec"]["artifactImports"][0]["unit"] = "missing"
        path.write_text(json.dumps(document))

    candidate = tmp_path / "failed-candidate"
    try:
        result = _advance_import_fixture(fixture, candidate)
    except (controller.ReferenceUnavailable, controller.OperationError) as error:
        assert expected in str(error)
    else:
        assert result.blocked
        assert expected in " ".join(str(reason) for reason in result.blocked.values())
    finally:
        assert not controller.document_candidates(candidate / "units", "application--deploy")


def test_promoted_artifact_import_records_lineage_and_reconciles_from_desired(
    tmp_path: Path,
    monkeypatch,
):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    staging = tmp_path / "staging-desired"
    result = _advance_import_fixture(fixture, staging)

    assert result.blocked == {}
    deploy = controller.load_desired_unit(
        controller.unit_document_path(staging, "application--deploy"), "application--deploy"
    )
    assert deploy.spec.terraform is not None
    assert deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["image"].endswith("c" * 64)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(controller.document_candidates(staging / "stacks", "application")[0]),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedArtifactImports is not None
    assert list(stack.spec.resolvedArtifactImports) == ["image/containers"]
    evidence = stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence.sourceStack == "application"
    assert evidence.sourceUnit == "application--image"
    assert evidence.sourceDesiredRevision == fixture.promotion.desired_revision
    assert evidence.sourceObservedRevision == "d" * 40
    assert evidence.targetStackUid == stack.metadata.uid

    specifications, _ = controller.load_convergence_specifications(
        fixture.source, "staging", staging, "f" * 40, tmp_path / "staging-reconcile"
    )
    assert specifications["application--deploy"].spec.terraform.variables["image"].endswith("c" * 64)


def test_promoted_artifact_import_chains_from_staging_to_production(tmp_path: Path, monkeypatch):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    staging = tmp_path / "staging-desired"
    staging_result = _advance_import_fixture(fixture, staging)
    assert staging_result.blocked == {}
    staging_stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(controller.document_candidates(staging / "stacks", "application")[0]),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(staging_stack.spec, controller.DesiredStackSpec)
    assert staging_stack.spec.resolvedArtifactImports is not None
    staging_evidence = staging_stack.spec.resolvedArtifactImports["image/containers"]

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
    promotion = controller.PromotionContext(
        source_environment="staging",
        desired_ref="deploy/staging",
        desired_revision="e" * 40,
        observed_ref="observed/staging",
        observed_revision=None,
        specification_revision="e" * 40,
        desired_root=staging,
    )
    result = controller.build_desired_candidate(
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
    deploy = controller.load_desired_unit(
        controller.unit_document_path(production, "application--deploy"), "application--deploy"
    )
    assert deploy.spec.terraform is not None
    assert deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["image"].endswith("c" * 64)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(
            controller.document_candidates(production / "stacks", "application")[0]
        ),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedArtifactImports is not None
    evidence = stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence == replace(staging_evidence, targetStackUid=stack.metadata.uid)


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
    with pytest.raises(controller.OperationError, match="ambiguous"):
        controller.resolve_template(
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
