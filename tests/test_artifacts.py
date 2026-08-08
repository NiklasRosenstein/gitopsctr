"""First-class artifact publication and validation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from gitopsctr import cli
from gitopsctr import driver as driver_registry
from gitopsctr.driver import ArtifactDocumentContract, DriverError


def project(root: Path, write_format: str) -> None:
    (root / "gitopsctr.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "artifacts-test"},
                "spec": {"writeFormat": write_format},
            },
            sort_keys=False,
        )
    )


def container_images() -> dict[str, object]:
    return {
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
            "inputHash": "sha256:" + "b" * 64,
        },
        "images": {"application": {"uri": "registry.example/application@sha256:" + "c" * 64}},
    }


@pytest.mark.parametrize(("write_format", "suffix"), (("yaml", ".yaml"), ("json", ".json")))
def test_artifact_documents_follow_project_format_and_replace_stale_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_format: str,
    suffix: str,
) -> None:
    project(tmp_path, write_format)
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    observed = tmp_path / "observed"
    stale = observed / "artifacts/images/stale.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale: true\n")

    descriptors = cli.write_artifact_documents(
        observed,
        "images",
        "oci-images",
        {"containers": container_images()},
    )

    path = observed / f"artifacts/images/containers{suffix}"
    assert path.is_file()
    assert not stale.exists()
    document = cli.load_document(path)
    schema_url = "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/artifact.gitopsctr.io/v1/ContainerImages.schema.json"
    if write_format == "yaml":
        assert path.read_text().startswith(f"# yaml-language-server: $schema={schema_url}\n")
    else:
        assert document["$schema"] == schema_url
    assert document["images"]["application"]["uri"].endswith("c" * 64)
    assert descriptors == {
        "containers": {
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "path": f"artifacts/images/containers{suffix}",
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "mediaType": f"application/vnd.gitopsctr.container-images.v1+{write_format}",
        }
    }


def test_artifact_driver_contract_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path, "json")
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)

    with pytest.raises(DriverError, match=r"expected \['containers'\]"):
        cli.write_artifact_documents(tmp_path / "observed", "images", "oci-images", {})
    with pytest.raises(DriverError, match="extra"):
        cli.write_artifact_documents(
            tmp_path / "observed",
            "images",
            "oci-images",
            {"containers": container_images(), "extra": {}},
        )


def test_artifact_resource_name_mismatch_warns_without_rejecting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = container_images()
    document["metadata"] = {"name": "release-images"}

    cli.validate_artifact_output_identity(
        "oci-images",
        {
            "name": "images",
            "source": {
                "revision": "a" * 40,
                "inputHash": "sha256:" + "b" * 64,
            },
        },
        {"containers": document},
    )

    warning = capsys.readouterr().err
    assert "WARN" in warning
    assert "resource name 'release-images'" in warning


def test_artifact_gvk_rejects_conflicting_installed_contracts() -> None:
    contract = cli.UNIT_DRIVERS["oci-images"].artifact_contracts["containers"]
    registry: dict[str, ArtifactDocumentContract] = {}

    driver_registry._register_artifact_gvk(registry, contract)
    driver_registry._register_artifact_gvk(registry, contract)

    with pytest.raises(DriverError, match="conflicting installed contracts"):
        driver_registry._register_artifact_gvk(
            registry,
            ArtifactDocumentContract(
                contract.api_version,
                contract.kind,
                contract.contract,
                "application/vnd.example.conflict",
            ),
        )


def test_artifact_validation_rejects_tampering_and_extra_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project(tmp_path, "json")
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    observed = tmp_path / "observed"
    unit = {
        "name": "images",
        "driver": "oci-images",
        "source": {
            "revision": "a" * 40,
            "inputHash": "sha256:" + "b" * 64,
        },
    }
    descriptors = cli.write_artifact_documents(
        observed,
        "images",
        "oci-images",
        {"containers": container_images()},
    )
    receipt = {"unit": "images", "driver": "oci-images", "artifacts": descriptors}
    artifact_path = observed / "artifacts/images/containers.json"

    cli.validate_receipt_artifacts(observed, unit, receipt)
    artifact_path.write_text("{}\n")
    with pytest.raises(cli.ReferenceUnavailable, match="digest"):
        cli.validate_receipt_artifacts(observed, unit, receipt)

    descriptors = cli.write_artifact_documents(
        observed,
        "images",
        "oci-images",
        {"containers": container_images()},
    )
    receipt["artifacts"] = descriptors
    (observed / "artifacts/images/extra.json").write_text("{}\n")
    with pytest.raises(cli.OperationError, match="complete contract set"):
        cli.validate_receipt_artifacts(observed, unit, receipt)


def test_stale_artifact_receipt_does_not_block_reconciliation_status(tmp_path: Path) -> None:
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    desired_unit = desired / "units/images.json"
    desired_unit.parent.mkdir(parents=True)
    desired_unit.write_text(
        json.dumps(
            {
                "name": "images",
                "driver": "oci-images",
                "source": {
                    "path": ".",
                    "revision": "d" * 40,
                    "driverVersion": 1,
                    "inputHash": "sha256:" + "e" * 64,
                },
                "build": {"dockerfile": "Dockerfile", "platform": "linux/amd64"},
                "publish": {
                    "targets": {
                        "application": {
                            "type": "registry",
                            "repository": "registry.example/application",
                        }
                    }
                },
            }
        )
    )
    receipt_path = observed / "units/images.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
                {
                    "unit": "images",
                    "driver": "oci-images",
                    "desired": {"unitBlob": "stale-unit-blob"},
                    "artifacts": {
                        "containers": {
                            "apiVersion": "artifact.gitopsctr.io/v1",
                            "kind": "ContainerImages",
                            "path": "artifacts/images/containers.json",
                            "digest": "sha256:" + "0" * 64,
                            "mediaType": "application/vnd.gitopsctr.container-images.v1+json",
                        }
                    },
                }
        )
    )

    assert cli.reconciliation_statuses(["images"], desired, observed) == [
        ("images", "READY", "desired inputs changed since its last receipt")
    ]


def test_observation_publication_writes_receipt_and_artifact_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project(tmp_path, "json")
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    captured: dict[str, bytes] = {}

    def observed_tree(_ref: str, output: Path) -> None:
        output.mkdir(parents=True)

    def publish_tree(_ref: str, tree: Path, _expected: str | None, _message: str) -> str:
        for path in tree.rglob("*"):
            if path.is_file():
                captured[path.relative_to(tree).as_posix()] = path.read_bytes()
        return "d" * 40

    monkeypatch.setattr(cli, "observed_tree", observed_tree)
    monkeypatch.setattr(cli, "publish_tree", publish_tree)

    revision = cli.publish_observation_cas(
        "observed/dev",
        "images",
        {
            "unit": "images",
            "driver": "oci-images",
            "desired": {"revision": "e" * 40, "unitBlob": "unit-blob"},
            "controller": {},
        },
        {
            "name": "images",
            "driver": "oci-images",
            "source": {
                "revision": "a" * 40,
                "inputHash": "sha256:" + "b" * 64,
            },
        },
        {"containers": container_images()},
        "e" * 40,
    )

    assert revision == "d" * 40
    artifact_bytes = captured["artifacts/images/containers.json"]
    receipt = json.loads(captured["units/images.json"])
    descriptor = receipt["status"]["artifacts"]["containers"]
    assert descriptor["path"] == "artifacts/images/containers.json"
    assert descriptor["digest"] == "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()


def test_observation_publication_retries_without_losing_concurrent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project(tmp_path, "json")
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    attempts = 0
    published: dict[str, bytes] = {}

    def observed_tree(_ref: str, output: Path) -> str:
        nonlocal attempts
        attempts += 1
        output.mkdir(parents=True)
        if attempts == 2:
            concurrent = output / "units/concurrent.json"
            concurrent.parent.mkdir(parents=True)
            concurrent.write_text('{"concurrent":true}\n')
        return ("a" if attempts == 1 else "b") * 40

    def publish_tree(_ref: str, tree: Path, expected: str | None, _message: str) -> str:
        if expected == "a" * 40:
            raise subprocess.CalledProcessError(
                1,
                ["git", "push"],
                stderr="rejected (non-fast-forward)",
            )
        for path in tree.rglob("*"):
            if path.is_file():
                published[path.relative_to(tree).as_posix()] = path.read_bytes()
        return "c" * 40

    monkeypatch.setattr(cli, "observed_tree", observed_tree)
    monkeypatch.setattr(cli, "publish_tree", publish_tree)

    revision = cli.publish_observation_cas(
        "observed/dev",
        "images",
        {
            "unit": "images",
            "driver": "oci-images",
            "desired": {"revision": "e" * 40, "unitBlob": "unit-blob"},
            "controller": {},
        },
        {
            "name": "images",
            "driver": "oci-images",
            "source": {
                "revision": "a" * 40,
                "inputHash": "sha256:" + "b" * 64,
            },
        },
        {"containers": container_images()},
        "e" * 40,
    )

    assert revision == "c" * 40
    assert attempts == 2
    assert "units/concurrent.json" in published
    assert "units/images.json" in published
    assert "artifacts/images/containers.json" in published
