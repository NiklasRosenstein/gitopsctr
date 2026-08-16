"""Integration coverage for artifact references across projected Stack promotion."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import ArtifactImport, PromotionStackReference
from tests.conftest import receipt_resource
from tests.stack_support import commit, git, project_repository


@dataclass(frozen=True)
class ImportFixture:
    source: Path
    source_revision: str
    desired: Path
    observed: Path
    promotion: controller.PromotionContext
    current: Path
    target_observed: Path


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True))


def _mutate(path: Path, change: Callable[[dict[str, object]], None]) -> None:
    document = controller.RESOURCE_CATALOG.load_document(path)
    assert isinstance(document, dict)
    change(document)
    document_format = (
        controller.DocumentFormat.YAML if path.suffix in {".yaml", ".yml"} else controller.DocumentFormat.JSON
    )
    controller.write_document(path, document, format=document_format)


def _template() -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "application"},
        "spec": {
            "parameters": [],
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
            },
        },
    }


def _stack(*, units: list[str] | None = None, imports: list[dict[str, object]] | None = None) -> dict[str, object]:
    spec: dict[str, object] = {"template": "application"}
    if units is not None:
        spec["units"] = units
    if imports is not None:
        spec["artifactImports"] = imports
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "application"},
        "spec": spec,
    }


def _artifact_import(unit: str = "image") -> dict[str, object]:
    return {
        "unit": unit,
        "name": "containers",
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "fromPromotion": {"stack": "application"},
    }


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
        "images": {"application": {"uri": "registry.example/app@sha256:" + "c" * 64}},
    }


def _source_observation(
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
            source=controller.DesiredSource(
                path=".",
                revision=source_revision,
                inputHash="sha256:" + "b" * 64,
                driverVersion=driver.version,
            ),
            resolve_template=lambda *_args: (_ for _ in ()).throw(AssertionError("not called")),
        ),
    ).unit
    image_path = controller.write_desired_candidate_unit(
        desired_root / "units/application--image.json",
        image.with_spec(resolved).with_metadata(
            controller.ResourceMetadata(
                name="application--image",
                uid="source-image-unit",
                ownerReferences=[projection.owners["application--image"]],
            )
        ),
        source_root,
    )
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


def _setup_import_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportFixture:
    source = tmp_path / "source"
    dev = project_repository(source)
    staging = source / "deployment/environments/staging"
    staging.mkdir(parents=True)
    _write(
        staging / "environment.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Environment",
            "metadata": {"name": "staging"},
            "spec": {"promotion": {"allowedSources": ["dev"]}},
        },
    )
    _write(source / "deployment/stack-templates/application.json", _template())
    _write(dev / "stacks/application.json", _stack())
    _write(
        staging / "stacks/application.json",
        _stack(units=["deploy"], imports=[_artifact_import()]),
    )
    git(source, "init", "-b", "main")
    source_revision = commit(source, "artifact import promotion fixture")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()

    desired = tmp_path / "dev-desired"
    projection = controller.project_stack_resources(source, "dev", source_revision, desired, source)
    observed = tmp_path / "dev-observed"
    _source_observation(source, desired, observed, projection, source_revision)
    current = tmp_path / "empty-current"
    current.mkdir()
    target_observed = tmp_path / "staging-observed"
    target_observed.mkdir()
    return ImportFixture(
        source=source,
        source_revision=source_revision,
        desired=desired,
        observed=observed,
        promotion=controller.PromotionContext(
            source_environment="dev",
            desired_ref="gitopsctr/desired/dev",
            desired_revision=source_revision,
            observed_ref="gitopsctr/observed/dev",
            observed_revision="d" * 40,
            specification_revision=source_revision,
            desired_root=desired,
            observed_root=observed,
        ),
        current=current,
        target_observed=target_observed,
    )


def _advance(fixture: ImportFixture, output: Path, *, promotion=None):
    return controller.build_desired_candidate(
        "staging",
        fixture.source,
        fixture.source_revision,
        fixture.current,
        fixture.target_observed,
        None,
        output,
        promotion=fixture.promotion if promotion is None else promotion,
        verbose=False,
    )


def _source_receipt_path(fixture: ImportFixture) -> Path:
    return controller.unit_document_path(fixture.observed, "application--image")


def _source_artifact_path(fixture: ImportFixture) -> Path:
    return controller.document_candidates(fixture.observed / "artifacts/application--image", "containers")[0]


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("stack-uid", "Stack"),
        ("unit-owner-uid", "owner fence"),
        ("missing-unit", "source Unit"),
        ("missing-receipt", "receipt"),
        ("stale-receipt", "receipt"),
        ("wrong-desired-revision", "producer"),
        ("missing-observed-revision", "UID identity"),
        ("wrong-gvk", "artifact"),
        ("wrong-media-type", "receipt"),
        ("wrong-digest", "digest"),
        ("wrong-producer", "producer"),
        ("missing-artifact", "artifact"),
        ("unmatched-import", "producer"),
        ("ambiguous-import", "ambiguous"),
    ),
)
def test_promoted_artifact_import_rejects_invalid_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    if failure == "stack-uid":
        _mutate(
            controller.document_candidates(fixture.desired / "stacks", "application")[0],
            lambda document: document["metadata"].__setitem__("uid", "different-stack-uid"),
        )
    elif failure == "unit-owner-uid":
        _mutate(
            controller.unit_document_path(fixture.desired, "application--image"),
            lambda document: document["metadata"]["ownerReferences"][0].__setitem__("uid", "different-stack-uid"),
        )
    elif failure == "missing-unit":
        controller.unit_document_path(fixture.desired, "application--image").unlink()
    elif failure == "missing-receipt":
        _source_receipt_path(fixture).unlink()
    elif failure == "stale-receipt":
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["spec"]["desired"].__setitem__("unitBlob", "stale-unit-blob"),
        )
    elif failure == "wrong-desired-revision":
        unit_path = controller.unit_document_path(fixture.desired, "application--image")
        _mutate(unit_path, lambda document: document["spec"]["source"].__setitem__("revision", "a" * 40))
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["spec"]["desired"].__setitem__("unitBlob", controller.file_blob(unit_path)),
        )
    elif failure == "missing-observed-revision":
        fixture = replace(fixture, promotion=replace(fixture.promotion, observed_revision=None))
    elif failure == "wrong-gvk":
        artifact_path = _source_artifact_path(fixture)
        _mutate(artifact_path, lambda document: document.__setitem__("kind", "WrongArtifact"))
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["status"]["artifacts"]["containers"].__setitem__(
                "digest", controller.sha256_file(artifact_path)
            ),
        )
    elif failure == "wrong-media-type":
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["status"]["artifacts"]["containers"].__setitem__(
                "mediaType", "application/octet-stream"
            ),
        )
    elif failure == "wrong-digest":
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["status"]["artifacts"]["containers"].__setitem__("digest", "sha256:" + "0" * 64),
        )
    elif failure == "wrong-producer":
        artifact_path = _source_artifact_path(fixture)
        _mutate(artifact_path, lambda document: document["producer"].__setitem__("name", "other--image"))
        _mutate(
            _source_receipt_path(fixture),
            lambda document: document["status"]["artifacts"]["containers"].__setitem__(
                "digest", controller.sha256_file(artifact_path)
            ),
        )
    elif failure == "missing-artifact":
        _source_artifact_path(fixture).unlink()
    elif failure == "unmatched-import":
        _mutate(
            fixture.source / "deployment/environments/staging/stacks/application.json",
            lambda document: document["spec"]["artifactImports"][0].__setitem__("unit", "missing"),
        )
    elif failure == "ambiguous-import":
        _mutate(
            fixture.source / "deployment/stack-templates/application.json",
            lambda document: document["spec"]["unitTemplates"]["deploy"]["spec"]["terraform"]["variables"]["image"][
                "fromArtifact"
            ].__setitem__("unit", "application--image"),
        )
        _mutate(
            fixture.source / "deployment/environments/staging/stacks/application.json",
            lambda document: document["spec"].__setitem__(
                "artifactImports", [_artifact_import(), _artifact_import("application--image")]
            ),
        )

    candidate = tmp_path / "failed-candidate"
    try:
        result = _advance(fixture, candidate)
    except (controller.ContractError, controller.OperationError) as error:
        assert expected.lower() in str(error).lower()
    else:
        assert result.blocked
        assert expected.lower() in " ".join(str(reason) for reason in result.blocked.values()).lower()
    assert not controller.unit_document_path(candidate, "application--deploy").exists()


def test_promoted_artifact_import_resolves_and_records_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    target = tmp_path / "staging-desired"
    result = _advance(fixture, target)

    assert result.blocked == {}
    deploy = controller.load_desired_unit(
        controller.unit_document_path(target, "application--deploy"), "application--deploy"
    )
    assert deploy.spec.terraform is not None
    assert deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["image"].endswith("c" * 64)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(controller.document_candidates(target / "stacks", "application")[0]),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedArtifactImports is not None
    evidence = stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence.sourceStack == "application"
    assert evidence.sourceUnit == "application--image"
    assert evidence.sourceDesiredRevision == fixture.promotion.desired_revision
    assert evidence.sourceObservedRevision == fixture.promotion.observed_revision
    assert evidence.targetStackUid == stack.metadata.uid


def test_promoted_artifact_import_reloads_from_persisted_desired_without_template_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    target = tmp_path / "staging-desired"
    assert _advance(fixture, target).blocked == {}

    restarted_source = tmp_path / "restarted-source"
    project_repository(restarted_source)
    staging = restarted_source / "deployment/environments/staging"
    staging.mkdir(parents=True)
    _write(
        staging / "environment.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Environment",
            "metadata": {"name": "staging"},
            "spec": {},
        },
    )
    reloaded = tmp_path / "reloaded-desired"
    shutil.copytree(target, reloaded)

    specifications, dependencies = controller.load_convergence_specifications(
        restarted_source,
        "staging",
        reloaded,
        fixture.source_revision,
        tmp_path / "restart-projection",
    )
    assert specifications["application--deploy"].spec.terraform.variables["image"].endswith("c" * 64)
    assert dependencies["application--deploy"] == ()
    assert not (restarted_source / "deployment/stack-templates").exists()


def test_promoted_artifact_import_chains_from_persisted_staging_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _setup_import_fixture(tmp_path, monkeypatch)
    staging = tmp_path / "staging-desired"
    assert _advance(fixture, staging).blocked == {}

    _write(
        fixture.source / "deployment/environments/production/environment.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Environment",
            "metadata": {"name": "production"},
            "spec": {},
        },
    )
    _write(
        fixture.source / "deployment/environments/production/stacks/application.json",
        _stack(units=["deploy"], imports=[_artifact_import()]),
    )
    current = tmp_path / "production-current"
    current.mkdir()
    observed = tmp_path / "production-observed"
    observed.mkdir()
    result = controller.build_desired_candidate(
        "production",
        fixture.source,
        fixture.source_revision,
        current,
        observed,
        None,
        tmp_path / "production-desired",
        promotion=controller.PromotionContext(
            source_environment="staging",
            desired_ref="gitopsctr/desired/staging",
            desired_revision="e" * 40,
            observed_ref="gitopsctr/observed/staging",
            observed_revision=None,
            specification_revision=fixture.source_revision,
            desired_root=staging,
        ),
        verbose=False,
    )
    assert result.blocked == {}
    production = tmp_path / "production-desired"
    deploy = controller.load_desired_unit(
        controller.unit_document_path(production, "application--deploy"), "application--deploy"
    )
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
    assert stack.spec.resolvedArtifactImports["image/containers"].targetStackUid == stack.metadata.uid


def test_promoted_artifact_import_rejects_ambiguous_imports_directly(tmp_path: Path):
    imports = (
        ArtifactImport(
            unit="image",
            name="containers",
            apiVersion="artifact.gitopsctr.io/v1",
            kind="ContainerImages",
            fromPromotion=PromotionStackReference(stack="application"),
        ),
        ArtifactImport(
            unit="application--image",
            name="containers",
            apiVersion="artifact.gitopsctr.io/v1",
            kind="ContainerImages",
            fromPromotion=PromotionStackReference(stack="application"),
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
