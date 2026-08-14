"""Reconciliation preserves planning and convergence behavior around clean units."""

import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller as deploy_release
from gitopsctr.resources import ResourceMetadata
from tests.conftest import receipt_document, write_test_document


@pytest.fixture(autouse=True)
def _local_effect_lease(monkeypatch):
    def acquire(_ref, revision, unit_name, uid, **_kwargs):
        lease = deploy_release.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="lease-test",
            owner="test-runner",
            desired_revision=revision,
            expires_at=2_000_000_000,
        )
        return deploy_release.EffectLeaseAcquisition(lease=lease, revision=revision)

    monkeypatch.setattr(deploy_release, "acquire_effect_lease", acquire)
    monkeypatch.setattr(deploy_release, "release_effect_lease", lambda *_args, **_kwargs: None)

    class NoopHeartbeat:
        def __init__(self, acquisition):
            self.acquisition = acquisition

        def stop(self):
            return self.acquisition

    monkeypatch.setattr(
        deploy_release,
        "start_effect_lease_heartbeat",
        lambda _ref, acquisition, **_kwargs: NoopHeartbeat(acquisition),
    )
    monkeypatch.setattr(
        deploy_release,
        "rebase_effect_completion",
        lambda _ref, acquisition, _unit_name, _uid, _root, **_kwargs: acquisition,
    )


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


def test_reconcile_parser_exposes_plan_without_a_dry_alias():
    parser = deploy_release.build_parser()

    args = parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--plan"])
    assert args.plan is True
    assert args.verbose is False
    verbose_args = parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--verbose"])
    assert verbose_args.verbose is True
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "--environment", "dev", "--unit", "app", "--dry"])


def test_reconciliation_artifact_effects_distinguish_added_updated_and_unchanged(monkeypatch, tmp_path):
    previous = SimpleNamespace(
        status=SimpleNamespace(
            artifacts={"same": object(), "updated": object()},
        )
    )
    driver = SimpleNamespace(
        driver_name="example",
        artifact_outputs={"added": object(), "same": object(), "updated": object()},
    )
    unit = SimpleNamespace(driver=driver, driver_name="example")
    previous_documents = {
        "same": {"value": "same"},
        "updated": {"value": "before"},
    }

    monkeypatch.setattr(
        deploy_release,
        "load_artifact_document",
        lambda _observed, _unit, _receipt, name, **_kwargs: (previous_documents[name], "sha256:previous"),
    )
    monkeypatch.setattr(
        deploy_release,
        "require_artifact_api",
        lambda _kind: SimpleNamespace(dump=lambda document: document),
    )
    monkeypatch.setattr(
        deploy_release,
        "parse_artifact_document",
        lambda _api, document, _description: document,
    )

    effects = deploy_release.reconciliation_artifact_effects(
        tmp_path,
        unit,
        previous,
        {
            "added": {"value": "new"},
            "same": {"value": "same"},
            "updated": {"value": "after"},
        },
    )

    assert effects == [
        ("ADDED", "Artifact added"),
        ("UNCHANGED", "Artifact same"),
        ("UPDATED", "Artifact updated"),
    ]


def test_dry_plan_uses_artifact_fallback_when_observation_is_unavailable(tmp_path):
    template = {
        "image": {
            "fromArtifact": {
                "unit": "application-images",
                "name": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "pointer": "/images/control/uri",
                "dryFallback": "preview.invalid/control@sha256:" + "0" * 64,
            },
        }
    }
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"

    resolution = deploy_release.resolve_template(template, candidate, observed, None, dry=True)

    assert resolution.value == {"image": "preview.invalid/control@sha256:" + "0" * 64}
    assert resolution.artifacts == {}


def test_observation_reference_materializes_artifact_into_consumer(tmp_path):
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    producer = candidate / "units/application-images.json"
    unit = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "OciImages",
        "metadata": ResourceMetadata.root_from_provenance(
            "application-images", "artifact-reference-test", partition="application"
        ).document(profile="desired"),
        "spec": {
            "source": {
                "path": ".",
                "revision": "a" * 40,
                "driverVersion": 1,
                "inputHash": "sha256:" + "2" * 64,
            }
        },
    }
    _write_json(producer, unit)
    artifact_path = observed / "artifacts/application-images/containers.yaml"
    _write_json(
        artifact_path,
        {
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "metadata": {"name": "containers"},
            "producer": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "OciImages",
                "name": "application-images",
                "driverVersion": 1,
                "sourceRevision": "a" * 40,
                "inputHashVersion": 1,
                "inputHash": "sha256:" + "2" * 64,
            },
            "images": {"control": {"uri": "registry.example/control@sha256:" + "1" * 64}},
        },
    )
    _write_json(
        observed / "units/application-images.json",
        receipt_document(
            "oci-images",
            "application-images",
            {"unitBlob": deploy_release.file_blob(producer)},
            artifacts={
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": "artifacts/application-images/containers.yaml",
                    "digest": deploy_release.sha256_file(artifact_path),
                    "mediaType": "application/vnd.gitopsctr.container-images.v1+yaml",
                }
            },
        ),
    )

    resolution = deploy_release.resolve_template(
        {
            "image": {
                "fromArtifact": {
                    "unit": "application-images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "pointer": "/images/control/uri",
                },
            }
        },
        candidate,
        observed,
        "a" * 40,
    )

    assert resolution.value == {"image": "registry.example/control@sha256:" + "1" * 64}
    assert resolution.promotions == {}
    assert resolution.receipts == {}
    assert resolution.artifacts == {"application-images/containers": deploy_release.sha256_file(artifact_path)}

    with pytest.raises(deploy_release.ReferenceUnavailable, match="not artifact.gitopsctr.io/v1/FrontendBundle"):
        deploy_release.resolve_template(
            {
                "fromArtifact": {
                    "unit": "application-images",
                    "name": "containers",
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "FrontendBundle",
                }
            },
            candidate,
            observed,
            "a" * 40,
        )


def test_receipt_reference_normalizes_resource_receipt_before_applying_pointer(tmp_path):
    candidate = tmp_path / "candidate"
    observed = tmp_path / "observed"
    producer = candidate / "units/infrastructure.yaml"
    _write_json(
        producer,
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": ResourceMetadata.root_from_provenance(
                "infrastructure", "receipt-reference-test", partition="application"
            ).document(profile="desired"),
            "spec": {
                "source": {
                    "path": "infra/deploy",
                    "revision": "a" * 40,
                    "inputHash": "sha256:test",
                    "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
                },
                "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
            },
        },
    )
    receipt_path = observed / "units/infrastructure.yaml"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            receipt_document(
                "terraform",
                "infrastructure",
                {"revision": "d" * 40, "unitBlob": deploy_release.file_blob(producer)},
                {"applied": {"sourceRevision": "a" * 40}, "outputs": {"url": "https://example.invalid"}},
                resolved_inputs={},
                controller={
                    "version": "0.1.0",
                    "revision": "a" * 40,
                    "observed_at": "2026-08-08T00:00:00Z",
                },
            )
        )
    )

    resolution = deploy_release.resolve_template(
        {
            "url": {
                "fromReceipt": {"unit": "infrastructure", "pointer": "/outputs/url"},
            }
        },
        candidate,
        observed,
        "b" * 40,
    )

    assert resolution.value == {"url": "https://example.invalid"}
    assert resolution.promotions == {}
    assert resolution.receipts == {"infrastructure": deploy_release.file_blob(receipt_path)}
    assert resolution.artifacts == {}


def test_known_unmaterialized_unit_remains_a_successful_wait(monkeypatch):
    outputs = []

    def fake_observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        return "b" * 40

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, output: output.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda reconciled, desired="": outputs.append((reconciled, desired)),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="frontend",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision=None,
            observed_ref="observed/dev",
            plan=False,
            report=None,
        )
    )

    assert outputs == [(False, "")]


def test_reconcile_reports_persisted_transition_reason_before_unit_materialization(monkeypatch, capsys):
    outputs = []

    def fake_observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        return "b" * 40

    def fake_materialize(_revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        _write_json(
            output / ".gitopsctr/transition-blocks.json",
            {"schema": 1, "blocks": {"frontend": "persisted opaque cleanup reason"}},
        )

    monkeypatch.setattr(deploy_release, "observed_tree", fake_observed_tree)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", ""),
    )
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "c" * 40)
    monkeypatch.setattr(deploy_release, "materialize_revision", fake_materialize)
    monkeypatch.setattr(
        deploy_release,
        "write_reconcile_outputs",
        lambda reconciled, desired="": outputs.append((reconciled, desired)),
    )

    deploy_release.command_reconcile(
        Namespace(
            unit="frontend",
            environment="dev",
            desired_ref="deploy/dev",
            desired_revision=None,
            observed_ref="observed/dev",
            plan=False,
            report=None,
        )
    )

    assert outputs == [(False, "")]
    assert "WAIT     persisted opaque cleanup reason" in capsys.readouterr().err
