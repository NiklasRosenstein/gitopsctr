"""UID-fenced deletion requests for directly managed desired Units."""

import json
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from gitopsctr import controller as deploy_release
from gitopsctr.contracts import DesiredOwnerReference
from gitopsctr.errors import OperationError


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def _unit_document(
    name: str = "application",
    uid: str = "direct-application",
    mode: str = "direct",
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = deploy_release.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=(
            deploy_release.DesiredLifecycle(owner=owner)
            if owner is not None
            else deploy_release.DesiredLifecycle(
                management=deploy_release.LifecycleManagement(mode=mode)  # type: ignore[arg-type]
            )
        ),
    )
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": "a" * 40,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }


def _legacy_unit_document() -> dict[str, object]:
    return {
        "name": "application",
        "driver": "terraform",
        "source": {"path": "infra/deploy", "revision": "a" * 40},
        "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
    }


def _args(**overrides: object) -> Namespace:
    values = {
        "environment": "dev",
        "unit": "application",
        "uid": "direct-application",
        "desired_ref": "deploy/dev",
        "candidate_ref": None,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _prepare(tmp_path: Path, monkeypatch, document: dict[str, object]):
    current = tmp_path / "current"
    _write(current / "units/application.json", document)
    published: list[Path] = []

    def observed_tree(_ref: str, output: Path):
        shutil.copytree(current, output)
        return "b" * 40

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "c" * 40, None

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "publish_desired_change", publish)
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    return current, published


def test_request_delete_direct_unit_publishes_retained_uid_fenced_intent(tmp_path, monkeypatch):
    _current, published = _prepare(tmp_path, monkeypatch, _unit_document())

    assert deploy_release.command_request_delete_direct_unit(_args()) is True

    candidate = published[0]
    retained = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert retained.metadata.uid == "direct-application"
    assert intent.management_mode == "direct"
    assert intent.deletion_generation == 2
    assert deploy_release.load_desired_unit_incarnation_tombstones(candidate)["application"].state == "active"
    assert deploy_release.load_desired_transition_blocks(candidate)["application"] == (
        deploy_release.deletion_intent_reason(intent)
    )
    assert deploy_release.load_json(candidate / ".gitopsctr/deletion-intents/units/application.json")[
        "managementMode"
    ] == ("direct")


def test_request_delete_direct_unit_rejects_stale_uid(tmp_path, monkeypatch):
    _current, published = _prepare(tmp_path, monkeypatch, _unit_document())

    with pytest.raises(OperationError, match="stale desired Unit UID fence"):
        deploy_release.command_request_delete_direct_unit(_args(uid="other-application"))
    assert not published


def test_request_delete_direct_unit_rejects_source_tracked_unit(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, _unit_document(mode="sourceTracked"))

    with pytest.raises(OperationError, match="not directly managed"):
        deploy_release.command_request_delete_direct_unit(_args())


def test_request_delete_direct_unit_rejects_legacy_unit(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, _legacy_unit_document())

    with pytest.raises(OperationError, match="legacy"):
        deploy_release.command_request_delete_direct_unit(_args())


def test_request_delete_direct_unit_rejects_uid_owned_unit(tmp_path, monkeypatch):
    owner = DesiredOwnerReference(
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        name="parent",
        uid="parent-uid",
    )
    _prepare(tmp_path, monkeypatch, _unit_document(owner=owner))

    with pytest.raises(OperationError, match="UID-owned"):
        deploy_release.command_request_delete_direct_unit(_args())


def test_request_delete_direct_unit_rejects_conflicting_intent(tmp_path, monkeypatch):
    current, _published = _prepare(tmp_path, monkeypatch, _unit_document())
    unit_path = current / "units/application.json"
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, deploy_release.replace(intent, management_mode="sourceTracked"))

    with pytest.raises(OperationError, match="conflicting deletion intent"):
        deploy_release.command_request_delete_direct_unit(_args())


def test_request_delete_direct_unit_repeated_exact_request_is_noop(tmp_path, monkeypatch):
    current, published = _prepare(tmp_path, monkeypatch, _unit_document())
    unit_path = current / "units/application.json"
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current, 2)
    deploy_release.write_deletion_intent(current, intent)

    assert deploy_release.command_request_delete_direct_unit(_args()) is False
    assert not published


def test_request_delete_direct_unit_dry_run_does_not_publish(tmp_path, monkeypatch):
    current, published = _prepare(tmp_path, monkeypatch, _unit_document())
    calls = []

    def dry_publish(_environment, _candidate, *_args, **kwargs):
        calls.append(_args[6])
        return "b" * 40, None

    monkeypatch.setattr(deploy_release, "publish_desired_change", dry_publish)

    assert deploy_release.command_request_delete_direct_unit(_args(dry=True)) is False
    assert calls == [True]
    assert not published
    assert not (current / ".gitopsctr").exists()


def test_advance_desired_retains_direct_root_when_authored_source_is_absent(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    _write(
        source / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "test-project"},
            "spec": {"effectLease": None},
        },
    )
    _write(
        source / "deployment/environments/dev/environment.json",
        deploy_release.serialize_environment_document({"schema": 1, "name": "dev"}),
    )
    _write(current / "units/application.json", _unit_document())
    observed.mkdir()

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)

    retained = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    assert retained.metadata.lifecycle is not None
    assert retained.metadata.lifecycle.management is not None
    assert retained.metadata.lifecycle.management.mode == "direct"
    assert deploy_release.load_desired_deletion_intents(candidate) == {}
