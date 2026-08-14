"""Restart and failure-injection acceptance coverage for Unit cleanup."""

import json
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.driver import DriverError, TeardownResult
from gitopsctr.errors import OperationError
from tests.conftest import write_test_document
from tests.test_finalization import _finalize_args, _mark, _prepare_finalization, _terraform_unit


def test_failed_teardown_survives_restart_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        if len(teardown_calls) == 1:
            raise DriverError("transient destroy failure")
        return TeardownResult(details={"attempt": len(teardown_calls)})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    with pytest.raises(DriverError, match="transient destroy failure"):
        controller.command_finalize(_finalize_args())
    assert len(teardown_calls) == 1
    retained = controller.load_desired_unit(desired / "units/application.json", "application")
    assert controller.resource_deletion(retained) is not None

    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 2
    assert not (desired / "units/application.yaml").exists()
    assert controller.load_teardown_evidence(observed, "application", "d1-application", 1) is not None


def test_evidence_publication_crash_does_not_repeat_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []

    def teardown(_driver, _context):
        teardown_calls.append(True)
        return TeardownResult(details={"destroyed": True})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    original_publish = controller.publish_desired_change
    monkeypatch.setattr(
        controller,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controller crashed after evidence")),
    )
    with pytest.raises(RuntimeError, match="controller crashed after evidence"):
        controller.command_finalize(_finalize_args())
    assert len(teardown_calls) == 1
    assert controller.load_teardown_evidence(observed, "application", "d1-application", 1) is not None

    monkeypatch.setattr(controller, "publish_desired_change", original_publish)
    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 1
    assert not (desired / "units/application.yaml").exists()


def test_effect_lease_blocks_opaque_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-opaque-application"
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.OpaqueCleanupRoot(
            path=current / ".gitopsctr/cleanup/units/application.json",
            payload={"not": "a Unit"},
            metadata=controller.ResourceMetadata(
                name="application",
                uid=uid,
            ).with_partition("application"),
            source=None,
        ),
    )
    controller.write_effect_lease(
        current,
        controller.EffectLease(
            unit_name="application",
            uid=uid,
            token="lease-token",
            owner="test",
            desired_revision="c" * 40,
            expires_at=None,
        ),
    )
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: output.mkdir(parents=True))
    monkeypatch.setattr(
        controller, "observed_tree", lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1]
    )

    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        source_revision="a" * 40,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    with pytest.raises(OperationError, match="active effect lease"):
        controller.command_recover_opaque_unit(args)


def test_opaque_operator_resolution_requires_external_cleanup_confirmation(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-opaque-application"
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.OpaqueCleanupRoot(
            path=current / ".gitopsctr/cleanup/units/application.json",
            payload="unparseable",
            metadata=controller.ResourceMetadata(
                name="application",
                uid=uid,
            ).with_partition("application"),
            source=None,
        ),
    )
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        controller, "observed_tree", lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1]
    )

    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        deletion_generation=1,
        reason="manual cleanup",
        confirm_external_cleanup=False,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    with pytest.raises(OperationError, match="confirm-external-cleanup"):
        controller.command_resolve_opaque_unit(args)


def test_opaque_recovery_restores_parseable_payload_with_deletion_metadata(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    source = tmp_path / "source"
    current.mkdir()
    source.mkdir()
    uid = "d1-opaque-application"
    payload = _terraform_unit("application", uid)
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.OpaqueCleanupRoot(
            path=current / ".gitopsctr/cleanup/units/application.json",
            payload=payload,
            metadata=controller.ResourceMetadata(
                name="application",
                uid=uid,
            ).with_partition("application"),
            source=None,
        ),
    )
    (source / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {"effectLease": None},
            }
        )
    )
    (source / "deployment/environments/dev").mkdir(parents=True)
    write_test_document(source / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
    published: list[Path] = []

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: True)

    def materialize(revision: str, output: Path):
        if revision == "a" * 40:
            shutil.copytree(source, output)
        else:
            output.mkdir(parents=True)

    def observed_tree(_ref: str, output: Path):
        shutil.copytree(current, output)
        return "c" * 40

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "materialize_revision", materialize)
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(controller, "publish_desired_change", publish)
    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        source_revision="a" * 40,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    assert controller.command_recover_opaque_unit(args) is True
    restored = controller.load_desired_unit(published[0] / "units/application.json", "application")
    assert restored.metadata.uid == uid
    assert controller.resource_deletion(restored) is not None
    assert controller.load_desired_cleanup_roots(published[0]) == {}
    assert not any("deletion" in path.name for path in published[0].rglob("*"))
