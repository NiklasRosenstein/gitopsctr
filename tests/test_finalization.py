"""Durable source-tracked deletion intents and fenced finalization."""

from argparse import Namespace
from pathlib import Path

import pytest

from gitopsctr import cli as deploy_release
from gitopsctr.contracts import DesiredOwnerReference, DesiredSource
from gitopsctr.driver import TeardownResult
from gitopsctr.errors import OperationError
from tests.conftest import write_test_document


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _terraform_unit(
    name: str,
    uid: str,
    source_revision: str = "a" * 40,
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = deploy_release.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=(
            deploy_release.DesiredLifecycle(owner=owner)
            if owner is not None
            else deploy_release.DesiredLifecycle(management=deploy_release.LifecycleManagement(mode="sourceTracked"))
        ),
    )
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": source_revision,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }


def _vite_unit(name: str, uid: str) -> dict[str, object]:
    metadata = deploy_release.ResourceMetadata(
        name=name,
        uid=uid,
        lifecycle=deploy_release.DesiredLifecycle(management=deploy_release.LifecycleManagement(mode="sourceTracked")),
    )
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "ViteOciBundle",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "frontend",
                "revision": "a" * 40,
                "inputHash": "sha256:" + "2" * 64,
                "driverVersion": deploy_release.DRIVER_VERSIONS["vite-oci-bundle"],
            },
            "build": {"nodeVersion": "24"},
            "publish": {"repository": "registry.example/frontend"},
        },
    }


def _source_root(root: Path) -> None:
    _write_json(root / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})


def _finalize_args(unit: str = "application", **overrides: object) -> Namespace:
    values = {
        "unit": unit,
        "environment": "dev",
        "desired_ref": "deploy/dev",
        "observed_ref": "observed/dev",
        "candidate_ref": None,
        "uid": "d1-application",
        "deletion_generation": 1,
        "report": None,
        "dry": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _copy_observed_files(files: dict[str, bytes]):
    def observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return "c" * 40

    return observed_tree


def test_source_absence_publishes_durable_deletion_intent_and_retries(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _source_root(source)
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    observed.mkdir()

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, observed, None, candidate, verbose=False
    )

    intent = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert result.blocked["application"] == deploy_release.deletion_intent_reason(intent)
    assert intent.uid == "d1-application"
    assert intent.deletion_generation == 1
    assert intent.retained_source == DesiredSource(
        path="infra/deploy",
        revision="a" * 40,
        inputHash="sha256:" + "1" * 64,
        driverVersion=deploy_release.DRIVER_VERSIONS["terraform"],
    )
    assert deploy_release.unit_document_path(candidate, "application").is_file()
    assert deploy_release.reconciliation_statuses([], candidate, observed) == [
        ("application", "WAIT", deploy_release.deletion_intent_reason(intent))
    ]

    deploy_release.build_desired_candidate("dev", source, "c" * 40, candidate, observed, None, repeated, verbose=False)

    assert (
        deploy_release.unit_document_path(repeated, "application").read_bytes()
        == deploy_release.unit_document_path(candidate, "application").read_bytes()
    )
    assert (repeated / ".gitopsctr/deletion-intents/units/application.json").read_bytes() == (
        candidate / ".gitopsctr/deletion-intents/units/application.json"
    ).read_bytes()
    assert deploy_release.load_desired_deletion_intents(repeated)["application"].deletion_generation == 1


def test_finalize_tears_down_then_publishes_absent_state_and_is_idempotent(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    deploy_release.write_desired_transition_blocks(
        current, {"application": deploy_release.deletion_intent_reason(intent)}
    )

    desired_files = deploy_release.directory_files(current)
    publications: list[dict[str, object]] = []
    teardown_calls = []

    def observed_tree(ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        if ref == "deploy/dev":
            for relative, content in desired_files.items():
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return "c" * 40
        (output / "units/application.json").parent.mkdir(parents=True, exist_ok=True)
        (output / "units/application.json").write_text("stale receipt")
        artifact = output / "artifacts/application/output.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("stale artifact")
        return None

    def publish_tree(ref: str, directory: Path, parent: str, message: str):
        publications.append(
            {"ref": ref, "parent": parent, "message": message, "files": deploy_release.directory_files(directory)}
        )
        return "d" * 40

    def materialize_revision(_revision: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"uid": context.resource_uid, "generation": context.deletion_generation})

    driver_type = type(deploy_release.UNIT_DRIVERS["terraform"])
    monkeypatch.setattr(driver_type, "teardown", teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize_revision)
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(deploy_release, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(deploy_release, "publish_tree", publish_tree)

    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is True
    assert len(teardown_calls) == 1
    assert teardown_calls[0].resource_uid == intent.uid
    assert teardown_calls[0].deletion_generation == 1
    evidence_files = publications[0]["files"]
    assert ".gitopsctr/teardowns/units/application.json" in evidence_files
    assert "units/application.json" not in evidence_files
    assert "artifacts/application/output.txt" not in evidence_files
    files = publications[-1]["files"]
    assert "units/application.json" not in files
    assert ".gitopsctr/deletion-intents/units/application.json" not in files
    assert ".gitopsctr/transition-blocks.json" not in files
    assert capsys.readouterr().out == "d" * 40 + "\n"

    desired_files = files
    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is False
    assert len(teardown_calls) == 1


def test_finalize_blocks_unsupported_driver_and_retains_intent(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _vite_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    assert deploy_release.command_finalize(_finalize_args(uid=intent.uid, deletion_generation=1)) is False
    assert deploy_release.load_desired_deletion_intents(current)["application"].uid == intent.uid
    assert "does not support teardown" in capsys.readouterr().err


def test_finalize_failure_keeps_intent_for_retry(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    def observed_tree(_ref: str, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        for relative, content in deploy_release.directory_files(current).items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return "c" * 40

    def fail_teardown(_driver, _context):
        raise deploy_release.DriverError("destroy failed")

    monkeypatch.setattr(type(deploy_release.UNIT_DRIVERS["terraform"]), "teardown", fail_teardown)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(
        deploy_release, "materialize_revision", lambda _revision, output: output.mkdir(parents=True, exist_ok=True)
    )

    assert deploy_release.command_finalize(_finalize_args()) is False
    assert deploy_release.load_desired_deletion_intents(current)["application"].deletion_generation == 1
    assert "teardown failed: destroy failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [("uid", "d1-stale"), ("deletion_generation", 2)],
)
def test_finalize_rejects_stale_fences(tmp_path, monkeypatch, field, value):
    current = tmp_path / "current"
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)

    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(
        type(deploy_release.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args: pytest.fail("stale deletion intent reached teardown"),
    )

    with pytest.raises(OperationError, match="stale deletion"):
        deploy_release.command_finalize(_finalize_args(**{field: value}))


def test_finalize_requires_both_runtime_fences(tmp_path):
    with pytest.raises(SystemExit):
        deploy_release.build_parser().parse_args(["finalize", "--environment", "dev", "--unit", "application"])
    with pytest.raises(OperationError, match="requires --uid"):
        deploy_release.command_finalize(_finalize_args(uid=None))
    with pytest.raises(OperationError, match="requires --deletion-generation"):
        deploy_release.command_finalize(_finalize_args(deletion_generation=None))


def test_finalize_blocks_active_owned_children_before_teardown(tmp_path, monkeypatch, capsys):
    current = tmp_path / "current"
    parent_path = current / "units/parent.json"
    child_path = current / "units/child.json"
    _write_json(parent_path, _terraform_unit("parent", "d1-parent"))
    _write_json(
        child_path,
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
        ),
    )
    parent = deploy_release.load_desired_unit(parent_path, "parent")
    intent = deploy_release.UnitDeletionIntent.from_unit(parent, parent_path, current)
    deploy_release.write_deletion_intent(current, intent)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", _copy_observed_files(deploy_release.directory_files(current)))
    monkeypatch.setattr(
        type(deploy_release.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args: pytest.fail("parent teardown ran with an active child"),
    )

    assert (
        deploy_release.command_finalize(_finalize_args(unit="parent", uid=intent.uid, deletion_generation=1)) is False
    )
    assert "active owned/dependent Units must be finalized first: child" in capsys.readouterr().err


def test_repeated_advance_preserves_opaque_payload_for_mutated_intent(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    _source_root(source)
    unit_path = current / "units/application.json"
    _write_json(unit_path, _terraform_unit("application", "d1-application"))
    unit = deploy_release.load_desired_unit(unit_path, "application")
    intent = deploy_release.UnitDeletionIntent.from_unit(unit, unit_path, current)
    deploy_release.write_deletion_intent(current, intent)
    unit_path.write_text("not parseable")

    result = deploy_release.build_desired_candidate(
        "dev", source, "b" * 40, current, tmp_path / "observed", None, candidate, verbose=False
    )

    assert "application" in result.blocked
    assert not deploy_release.unit_document_path(candidate, "application").exists()
    opaque = deploy_release.load_desired_cleanup_roots(candidate)["application"]
    assert opaque.metadata.uid == intent.uid
    assert opaque.payload == "not parseable"
    assert deploy_release.load_desired_deletion_intents(candidate)["application"].uid == intent.uid


def test_rollback_cleanup_merge_carries_current_intent_and_discards_historical_intent(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current_unit_path = current / "units/application.json"
    historical_unit_path = candidate / "units/application.json"
    _write_json(current_unit_path, _terraform_unit("application", "d1-current"))
    _write_json(historical_unit_path, _terraform_unit("application", "d1-historical"))
    current_unit = deploy_release.load_desired_unit(current_unit_path, "application")
    historical_unit = deploy_release.load_desired_unit(historical_unit_path, "application")
    current_intent = deploy_release.UnitDeletionIntent.from_unit(current_unit, current_unit_path, current)
    historical_intent = deploy_release.UnitDeletionIntent.from_unit(historical_unit, historical_unit_path, candidate)
    deploy_release.write_deletion_intent(current, current_intent)
    deploy_release.write_deletion_intent(candidate, historical_intent)
    deploy_release.write_desired_transition_blocks(
        current, {"application": deploy_release.deletion_intent_reason(current_intent)}
    )
    deploy_release.write_desired_transition_blocks(candidate, {"application": "historical block"})

    deploy_release.merge_current_cleanup_state(current, candidate)

    merged = deploy_release.load_desired_deletion_intents(candidate)["application"]
    assert merged.uid == current_intent.uid
    assert deploy_release.unit_document_path(candidate, "application").read_bytes() == current_unit_path.read_bytes()
    assert deploy_release.load_desired_transition_blocks(candidate)[
        "application"
    ] == deploy_release.deletion_intent_reason(current_intent)
