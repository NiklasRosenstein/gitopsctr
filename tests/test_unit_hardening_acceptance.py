"""Restart and failure-injection acceptance coverage for Unit lifecycle hardening."""

from __future__ import annotations

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.driver import DriverError, TeardownResult
from gitopsctr.errors import OperationError
from tests.stack_support import project_repository
from tests.test_finalization import _finalize_args, _terraform_unit


def _stub_effect_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    def acquire(_ref: str, revision: str, unit_name: str, uid: str, **_kwargs: object):
        lease = controller.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="acceptance-lease",
            owner="acceptance",
            desired_revision=revision,
            expires_at=2_000_000_000,
        )
        return controller.EffectLeaseAcquisition(lease=lease, revision=revision)

    class NoopHeartbeat:
        def __init__(self, acquisition: controller.EffectLeaseAcquisition):
            self.acquisition = acquisition

        def stop(self) -> controller.EffectLeaseAcquisition:
            return self.acquisition

    monkeypatch.setattr(controller, "acquire_effect_lease", acquire)
    monkeypatch.setattr(controller, "release_effect_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller, "start_effect_lease_heartbeat", lambda _ref, acquisition, **_kwargs: NoopHeartbeat(acquisition)
    )
    monkeypatch.setattr(controller, "validate_effect_lease_head", lambda _ref, *_args: "c" * 40)
    monkeypatch.setattr(
        controller,
        "rebase_effect_completion",
        lambda _ref, acquisition, unit_name, uid, root: (
            replace(
                acquisition,
                lease=replace(acquisition.lease, snapshot=controller.effect_lease_snapshot(root, unit_name, uid)),
            )
            if acquisition.lease.snapshot is None
            else acquisition
        ),
    )


def test_failed_teardown_survives_restart_and_retries_from_durable_intent(tmp_path: Path, monkeypatch):
    desired_state = tmp_path / "desired-state"
    unit_path = desired_state / "units/application.json"
    unit_path.parent.mkdir(parents=True)
    unit_document = _terraform_unit("application", "d1-application")
    unit_path.write_text(json.dumps(unit_document))
    unit = controller.load_desired_unit(unit_path, "application")
    intent = controller.UnitDeletionIntent.from_unit(unit, unit_path, desired_state)
    controller.write_deletion_intent(desired_state, intent)
    controller.write_desired_transition_blocks(
        desired_state, {"application": controller.deletion_intent_reason(intent)}
    )
    observed_state = tmp_path / "observed-state"
    observed_state.mkdir()

    def observed_tree(ref: str, output: Path):
        source = desired_state if ref == "deploy/dev" else observed_state
        shutil.copytree(source, output)
        return "c" * 40 if ref == "deploy/dev" else None

    def publish_tree(ref: str, directory: Path, _parent: str | None, _message: str):
        target = desired_state if ref == "deploy/dev" else observed_state
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(directory, target)
        return "d" * 40

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: output.mkdir(parents=True))
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(controller, "publish_tree", publish_tree)
    _stub_effect_lease(monkeypatch)

    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        if len(teardown_calls) == 1:
            raise DriverError("transient destroy failure")
        return TeardownResult(details={"attempt": len(teardown_calls)})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    assert controller.command_finalize(_finalize_args()) is False
    assert controller.load_desired_deletion_intents(desired_state)["application"] == intent

    # A new controller process sees the same durable intent and retries it.
    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 2
    assert controller.load_desired_deletion_intents(desired_state) == {}
    assert not controller.unit_document_path(desired_state, "application").exists()


def test_evidence_publication_crash_is_restart_safe_without_repeating_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    desired_state = tmp_path / "desired-state"
    unit_path = desired_state / "units/application.json"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(json.dumps(_terraform_unit("application", "d1-application")))
    unit = controller.load_desired_unit(unit_path, "application")
    intent = controller.UnitDeletionIntent.from_unit(unit, unit_path, desired_state)
    controller.write_deletion_intent(desired_state, intent)
    controller.write_desired_transition_blocks(
        desired_state, {"application": controller.deletion_intent_reason(intent)}
    )
    observed_state = tmp_path / "observed-state"
    observed_state.mkdir()
    desired_revision = "c" * 40

    def observed_tree(ref: str, output: Path):
        source = desired_state if ref == "deploy/dev" else observed_state
        shutil.copytree(source, output)
        return desired_revision if ref == "deploy/dev" else None

    def materialize_revision(revision: str, output: Path):
        if revision in {"c" * 40, "d" * 40}:
            shutil.copytree(desired_state, output)
        else:
            output.mkdir(parents=True)

    def publish_tree(ref: str, directory: Path, _parent: str | None, _message: str):
        nonlocal desired_revision
        target = desired_state if ref == "deploy/dev" else observed_state
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(directory, target)
        if ref == "deploy/dev":
            desired_revision = "d" * 40
        return "d" * 40

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: desired_revision)
    monkeypatch.setattr(controller, "materialize_revision", materialize_revision)
    monkeypatch.setattr(controller, "publish_tree", publish_tree)
    monkeypatch.setattr(controller, "validate_effect_lease_head", lambda _ref, *_args: desired_revision)
    monkeypatch.setattr(
        controller,
        "rebase_effect_completion",
        lambda _ref, acquisition, _unit_name, _uid, _root: acquisition,
    )

    class NoopHeartbeat:
        def __init__(self, acquisition: controller.EffectLeaseAcquisition):
            self.acquisition = acquisition

        def stop(self) -> controller.EffectLeaseAcquisition:
            return self.acquisition

    monkeypatch.setattr(
        controller, "start_effect_lease_heartbeat", lambda _ref, acquisition, **_kwargs: NoopHeartbeat(acquisition)
    )

    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"destroyed": True})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    original_publish_desired_change = controller.publish_desired_change
    monkeypatch.setattr(
        controller,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controller crashed after evidence")),
    )

    with pytest.raises(RuntimeError, match="controller crashed after evidence"):
        controller.command_finalize(_finalize_args())

    assert len(teardown_calls) == 1
    assert (
        controller.load_teardown_evidence(observed_state, "application", intent.uid, intent.deletion_generation)
        is not None
    )
    assert controller.load_desired_effect_leases(desired_state)["application"].uid == intent.uid

    # A fresh controller resumes from the durable evidence and removes the
    # lease and deletion intent without invoking the external driver again.
    monkeypatch.setattr(controller, "publish_desired_change", original_publish_desired_change)
    assert controller.command_finalize(_finalize_args()) is True
    assert len(teardown_calls) == 1
    assert controller.load_desired_deletion_intents(desired_state) == {}
    assert controller.load_desired_effect_leases(desired_state) == {}
    assert not controller.unit_document_path(desired_state, "application").exists()


def test_stale_delete_request_cannot_target_recreated_same_name(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    environment = project_repository(source)
    source_unit_path = environment / "units/application.json"
    source_unit_path.parent.mkdir(parents=True)
    source_unit_path.write_text(
        '{"schema": 1, "name": "application", "driver": "terraform", '
        '"source": {"path": "infra/deploy"}, '
        '"terraform": {"backend": {}, "variables": {}, "observeOutputs": []}}'
    )
    (source / "infra/deploy").mkdir(parents=True)
    (source / "infra/deploy/main.tf").write_text("terraform {}\n")
    current.mkdir()
    observed.mkdir()
    old_uid = "d1-finalized-application"
    controller.write_unit_incarnation_tombstone(
        current,
        controller.UnitIncarnationTombstone(unit_name="application", uid=old_uid),
    )
    candidate = tmp_path / "candidate"
    controller.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)
    recreated = controller.load_desired_unit(candidate / "units/application.json", "application")
    assert recreated.metadata.uid != old_uid

    def observed_tree(_ref: str, output: Path):
        shutil.copytree(candidate, output)
        return "c" * 40

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        controller, "publish_desired_change", lambda *_args, **_kwargs: pytest.fail("stale request published")
    )

    with pytest.raises(OperationError, match="not directly managed"):
        controller.command_request_delete_direct_unit(
            _finalize_args(uid=old_uid, desired_ref="deploy/dev", observed_ref=None, deletion_generation=None)
        )


def test_operator_resolves_permanently_unparseable_root_with_uid_fence(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-unparseable-application"
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.OpaqueCleanupRoot(
            path=current / ".gitopsctr/cleanup/units/application.json",
            payload="not a Unit document",
            metadata=controller.ResourceMetadata(
                name="application",
                uid=uid,
                lifecycle=controller.DesiredLifecycle(management=controller.LifecycleManagement(mode="sourceTracked")),
            ),
            source=None,
        ),
    )
    controller.write_desired_transition_blocks(current, {"application": "opaque cleanup root retained"})
    published: list[Path] = []

    def observed_tree(_ref: str, output: Path):
        shutil.copytree(current, output)
        return "c" * 40

    def publish(_environment: str, candidate: Path, *_args: object, **_kwargs: object):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(controller, "publish_desired_change", publish)

    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        reason="provider confirmed manual cleanup",
        confirm_external_cleanup=True,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    assert controller.command_resolve_opaque_unit(args) is True
    resolved = published[0]
    assert controller.load_desired_cleanup_roots(resolved) == {}
    assert controller.load_desired_transition_blocks(resolved) == {}
    assert controller.load_desired_unit_incarnation_tombstones(resolved)["application"].uid == uid


def test_operator_resolution_rejects_parseable_root_and_missing_confirmation(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-parseable-application"
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
                lifecycle=controller.DesiredLifecycle(management=controller.LifecycleManagement(mode="sourceTracked")),
            ),
            source=None,
        ),
    )
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        controller, "observed_tree", lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1]
    )

    missing_confirmation = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        reason="manual cleanup",
        confirm_external_cleanup=False,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    with pytest.raises(OperationError, match="confirm-external-cleanup"):
        controller.command_resolve_opaque_unit(missing_confirmation)

    parseable = Namespace(**{**vars(missing_confirmation), "confirm_external_cleanup": True})
    with pytest.raises(OperationError, match="parseable; use recover-opaque-unit"):
        controller.command_resolve_opaque_unit(parseable)
