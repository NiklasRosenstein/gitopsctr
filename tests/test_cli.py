"""Deployment progress stays visible while machine-readable stdout stays clean."""

import io
import json
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from gitopsctr import controller as deploy_release
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


def _terraform_desired_document(
    name: str = "aws-application",
    *,
    revision: str = "a" * 40,
    input_hash: str = "sha256:test",
    driver_version: int | None = None,
    variables: dict[str, object] | None = None,
    resolved_inputs: dict[str, object] | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "source": {
            "path": "infra/deploy",
            "revision": revision,
            "inputHash": input_hash,
            "driverVersion": driver_version or deploy_release.DRIVER_VERSIONS["terraform"],
        },
        "terraform": {"backend": {}, "variables": variables or {}, "observeOutputs": []},
    }
    if resolved_inputs is not None:
        spec["resolvedInputs"] = resolved_inputs
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": ResourceMetadata.root_from_provenance(name, f"test:{name}", partition="application").document(
            profile="desired"
        ),
        "spec": spec,
    }


def _terraform_desired_resource(name: str = "aws-application"):
    return deploy_release.RESOURCE_CATALOG.parse_unit(
        _terraform_desired_document(name), profile="desired", expected_name=name
    )


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
    assert "finalize" not in help_text


def test_delete_is_public_but_finalize_is_not():
    parser = deploy_release.build_parser()

    delete = parser.parse_args(
        [
            "delete",
            "unit",
            "--environment",
            "preview",
            "--name",
            "application",
            "--uid",
            "d1-application",
        ]
    )
    assert delete.handler is deploy_release.command_delete_resource
    assert delete.kind == "Unit"
    assert delete.name == "application"
    assert delete.uid == "d1-application"

    with pytest.raises(SystemExit):
        parser.parse_args(["finalize"])


def test_candidate_publication_delegates_change_request_to_ci(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(deploy_release, "load_desired_resource_graph", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(deploy_release, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args, **_kwargs: "pullRequest")
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deploy_release, "publish_tree", lambda *_args, **_kwargs: "d" * 40)
    monkeypatch.setattr(deploy_release, "verify_gated_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deploy_release,
        "ensure_change_request",
        lambda *_args, **_kwargs: pytest.fail("candidate publication must not call a forge adapter"),
    )

    revision, outcome = deploy_release.publish_desired_change(
        "dev",
        tmp_path / "candidate",
        "deploy/dev",
        "b" * 40,
        "candidate/dev/0123456789ab",
        "Finalize deletion",
        "Finalize deletion",
        "Finalize a UID-fenced deletion.",
        False,
        request_change=False,
    )

    assert revision == "d" * 40
    assert isinstance(outcome, deploy_release.ManualChangeRequest)
    assert "delegated" in outcome.reason


def test_write_change_outputs_handles_delegated_change_request(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    deploy_release.write_change_outputs(
        "d" * 40,
        "deploy/dev",
        "candidate/dev/0123456789ab",
        deploy_release.ManualChangeRequest(
            reason="delegated",
            head="candidate/dev/0123456789ab",
            base="deploy/dev",
            title="Delete preview",
            body="Delete preview Stack.",
            remote_url=None,
        ),
    )

    assert output.read_text() == (
        "change_revision=" + "d" * 40 + "\n"
        "target_ref=deploy/dev\n"
        "candidate_ref=candidate/dev/0123456789ab\n"
        "change_status=manual\n"
        "change_url=\n"
    )


def test_parseable_driver_transition_retains_fenced_deletion_metadata(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _write_json(source / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
    _write_json(
        source / "deployment/environments/dev/units/application.json",
        {
            "schema": 1,
            "name": "application",
            "driver": "vite-oci-bundle",
            "source": {"path": "frontend"},
            "build": {"nodeVersion": "24"},
            "publish": {"repository": "registry.example/application"},
        },
    )
    current_unit = _terraform_desired_resource("application")
    _write_json(current / "units/application.json", deploy_release.serialize_unit_document(current_unit))
    observed.mkdir()

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)

    retained = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    deletion = retained.metadata.deletion
    assert retained.metadata.uid == current_unit.metadata.uid
    assert deletion is not None
    assert deletion.generation == 1
    assert deletion.resourceDigest == deploy_release.resource_content_digest(current_unit)
    assert deploy_release.reconciliation_statuses(["application"], candidate, observed) == [
        ("application", "WAIT", deploy_release.deletion_reason(retained))
    ]

    deploy_release.build_desired_candidate("dev", source, "c" * 40, candidate, observed, None, repeated, verbose=False)
    repeated_resource = deploy_release.load_desired_unit(repeated / "units/application.json", "application")
    assert repeated_resource.metadata.uid == current_unit.metadata.uid
    assert repeated_resource.metadata.deletion == deletion


def test_finalized_same_name_recreation_gets_new_uid_from_tombstone(tmp_path):
    source = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    repeated = tmp_path / "repeated"
    _write_json(source / "deployment/environments/dev/environment.json", {"schema": 1, "name": "dev"})
    _write_json(
        source / "deployment/environments/dev/units/application.json",
        {
            "schema": 1,
            "name": "application",
            "driver": "terraform",
            "source": {"path": "infra/deploy"},
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    )
    (source / "infra/deploy").mkdir(parents=True)
    (source / "infra/deploy/main.tf").write_text("terraform {}\n")
    current.mkdir()
    observed.mkdir()
    old_uid = "d1-finalized-application"
    deploy_release.write_resource_incarnation_tombstone(
        current,
        deploy_release.ResourceIncarnationTombstone(
            api_version="unit.gitopsctr.io/v1",
            kind="Terraform",
            name="application",
            uid=old_uid,
            deletion_generation=1,
        ),
    )

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, candidate, verbose=False)
    recreated = deploy_release.load_desired_unit(candidate / "units/application.json", "application")
    assert recreated.metadata.uid != old_uid
    assert (
        deploy_release.load_resource_incarnation_tombstones(candidate)[
            ("unit.gitopsctr.io/v1", "Terraform", "application")
        ].uid
        == old_uid
    )

    deploy_release.build_desired_candidate("dev", source, "b" * 40, current, observed, None, repeated, verbose=False)
    assert deploy_release.load_desired_unit(repeated / "units/application.json", "application").metadata.uid == (
        recreated.metadata.uid
    )


def test_effect_lease_is_cas_published_and_blocks_a_second_runner(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    revisions = {"value": "a" * 40}
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revisions["value"])
    monkeypatch.setattr(deploy_release, "effect_lease_owner", lambda: "runner-a")
    monkeypatch.setattr(deploy_release, "effect_lease_token", lambda: "lease-runner-a")

    def materialize(_revision, output):
        shutil.copytree(desired, output)

    def publish(_ref, directory, _parent, _message):
        shutil.rmtree(desired)
        shutil.copytree(directory, desired)
        revisions["value"] = "b" * 40
        return revisions["value"]

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "publish_tree", publish)

    acquired = deploy_release.acquire_effect_lease("deploy/dev", "a" * 40, "application", "d1-application")
    assert acquired.revision == "b" * 40
    persisted = deploy_release.load_desired_effect_leases(desired)["application"]
    assert persisted.token == "lease-runner-a"
    assert persisted.expires_at is None

    with pytest.raises(deploy_release.EffectLeaseUnavailable, match="explicit UID/token recovery"):
        deploy_release.acquire_effect_lease("deploy/dev", "b" * 40, "application", "d1-application")

    with pytest.raises(deploy_release.EffectLeaseUnavailable, match="recovery fence"):
        deploy_release.recover_effect_lease("deploy/dev", "application", "d1-application", "wrong-token")
    assert deploy_release.load_desired_effect_leases(desired)["application"].token == "lease-runner-a"

    recovered = deploy_release.recover_effect_lease("deploy/dev", "application", "d1-application", "lease-runner-a")
    assert recovered == "b" * 40
    assert deploy_release.load_desired_effect_leases(desired) == {}


def test_effect_lease_precondition_rechecks_after_publish_race(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    unit = _terraform_desired_resource("application")
    unit_path = desired / "units/application.json"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(json.dumps(deploy_release.serialize_unit_document(unit)))
    revisions = {"value": "a" * 40}
    publish_attempts = 0
    precondition_calls: list[Path] = []

    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revisions["value"])
    monkeypatch.setattr(deploy_release, "effect_lease_owner", lambda: "runner-a")
    monkeypatch.setattr(deploy_release, "effect_lease_token", lambda: "lease-runner-a")

    def materialize(_revision, output):
        shutil.copytree(desired, output)

    def publish(_ref, _directory, _parent, _message):
        nonlocal publish_attempts
        publish_attempts += 1
        raise subprocess.CalledProcessError(1, "git push", stderr="non-fast-forward")

    def precondition(root: Path) -> None:
        precondition_calls.append(root)
        if len(precondition_calls) == 2:
            raise deploy_release.EffectLeaseUnavailable("new dependent appeared")

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "publish_tree", publish)

    with pytest.raises(deploy_release.EffectLeaseUnavailable, match="new dependent appeared"):
        deploy_release.acquire_effect_lease(
            "deploy/dev",
            "a" * 40,
            "application",
            unit.metadata.uid,
            precondition=precondition,
        )

    assert publish_attempts == 1
    assert len(precondition_calls) == 2
    assert deploy_release.load_desired_effect_leases(desired) == {}


def test_effect_lease_heartbeat_renews_before_expiry_during_long_effect(monkeypatch):
    lease = deploy_release.EffectLease(
        unit_name="application",
        uid="d1-application",
        token="lease-runner-a",
        owner="runner-a",
        desired_revision="a" * 40,
        expires_at=None,
    )
    acquisition = deploy_release.EffectLeaseAcquisition(lease=lease, revision="a" * 40)
    renewals = []

    def renew(_ref, current):
        renewals.append(current)
        return deploy_release.EffectLeaseAcquisition(
            lease=replace(current.lease, expires_at=None),
            revision="a" * 40,
        )

    monkeypatch.setattr(deploy_release, "renew_effect_lease", renew)
    heartbeat = deploy_release.start_effect_lease_heartbeat("deploy/dev", acquisition, interval_seconds=0.01)
    time.sleep(0.04)
    renewed = heartbeat.stop()

    assert renewals
    assert renewed.lease.token == lease.token
    assert renewed.lease.expires_at is None


def test_different_unit_heartbeats_rebase_and_preserve_each_other(monkeypatch, tmp_path):
    desired = tmp_path / "desired"
    state_lock = threading.Lock()
    revision = {"value": "a" * 40}
    next_revisions = iter(["b" * 40, "c" * 40] + [f"{value:040x}" for value in range(4, 30)])
    tokens = iter(["lease-a", "lease-b"])
    units = {name: _terraform_desired_resource(name) for name in ("application", "worker")}
    for name, unit in units.items():
        path = desired / f"units/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(deploy_release.serialize_unit_document(unit)))

    monkeypatch.setattr(deploy_release, "effect_lease_token", lambda: next(tokens))
    monkeypatch.setattr(deploy_release, "effect_lease_owner", lambda: "runner")
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revision["value"])

    def materialize(_revision, output):
        with state_lock:
            shutil.copytree(desired, output)

    def publish(_ref, directory, parent, _message):
        with state_lock:
            if parent != revision["value"]:
                raise subprocess.CalledProcessError(1, "git push", stderr="non-fast-forward")
            shutil.rmtree(desired)
            shutil.copytree(directory, desired)
            revision["value"] = next(next_revisions)
            return revision["value"]

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(deploy_release, "publish_tree", publish)

    application = deploy_release.acquire_effect_lease(
        "deploy/dev", "a" * 40, "application", units["application"].metadata.uid
    )
    worker = deploy_release.acquire_effect_lease("deploy/dev", "b" * 40, "worker", units["worker"].metadata.uid)
    results = {}
    errors = []

    def renew(name, acquisition):
        try:
            heartbeat = deploy_release.start_effect_lease_heartbeat("deploy/dev", acquisition, interval_seconds=0.01)
            time.sleep(0.03)
            results[name] = heartbeat.stop()
        except Exception as exc:  # pragma: no cover - assertion below reports the error
            errors.append(exc)

    threads = [
        threading.Thread(target=renew, args=("application", application)),
        threading.Thread(target=renew, args=("worker", worker)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(results) == {"application", "worker"}
    leases = deploy_release.load_desired_effect_leases(desired)
    assert leases["application"].token == "lease-a"
    assert leases["worker"].token == "lease-b"


def test_completion_rebases_after_unrelated_unit_renewal(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    local = tmp_path / "local"
    revisions = {"value": "a" * 40}
    units = {name: _terraform_desired_resource(name) for name in ("application", "worker")}
    for name, unit in units.items():
        path = desired / f"units/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(deploy_release.serialize_unit_document(unit)))
    deploy_release.write_effect_lease(
        desired,
        deploy_release.EffectLease(
            unit_name="application",
            uid=units["application"].metadata.uid,
            token="lease-a",
            owner="runner-a",
            desired_revision="a" * 40,
            expires_at=None,
        ),
    )
    deploy_release.write_effect_lease(
        desired,
        deploy_release.EffectLease(
            unit_name="worker",
            uid=units["worker"].metadata.uid,
            token="lease-b",
            owner="runner-b",
            desired_revision="a" * 40,
            expires_at=None,
        ),
    )
    shutil.copytree(desired, local)
    application = deploy_release.load_desired_effect_leases(desired)["application"]
    worker = deploy_release.load_desired_effect_leases(desired)["worker"]
    deploy_release.write_effect_lease(desired, replace(worker, desired_revision="b" * 40))
    revisions["value"] = "b" * 40
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revisions["value"])
    monkeypatch.setattr(
        deploy_release, "materialize_revision", lambda _revision, output: shutil.copytree(desired, output)
    )

    rebased = deploy_release.rebase_effect_completion(
        "deploy/dev",
        deploy_release.EffectLeaseAcquisition(lease=application, revision="a" * 40),
        "application",
        units["application"].metadata.uid,
        local,
    )

    assert rebased.revision == "b" * 40
    assert deploy_release.load_desired_effect_leases(local)["application"].token == "lease-a"
    assert deploy_release.load_desired_effect_leases(local)["worker"].desired_revision == "b" * 40


def test_observation_publication_rebases_after_unrelated_lease_renewal(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    observed_publication: dict[str, bytes] = {}
    revision = {"value": "a" * 40}
    units = {name: _terraform_desired_resource(name) for name in ("application", "worker")}
    for name, unit in units.items():
        path = desired / f"units/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(deploy_release.serialize_unit_document(unit)))
    deploy_release.write_effect_lease(
        desired,
        deploy_release.EffectLease(
            unit_name="application",
            uid=units["application"].metadata.uid,
            token="lease-a",
            owner="runner-a",
            desired_revision="a" * 40,
            expires_at=None,
        ),
    )
    deploy_release.write_effect_lease(
        desired,
        deploy_release.EffectLease(
            unit_name="worker",
            uid=units["worker"].metadata.uid,
            token="lease-b",
            owner="runner-b",
            desired_revision="a" * 40,
            expires_at=None,
        ),
    )
    application_lease = deploy_release.load_desired_effect_leases(desired)["application"]
    worker_lease = deploy_release.load_desired_effect_leases(desired)["worker"]
    calls = 0

    original_validate = deploy_release.validate_effect_lease_head

    def validate_with_worker_renewal(_ref, unit_name, uid, token, snapshot):
        nonlocal calls
        calls += 1
        result = original_validate(_ref, unit_name, uid, token, snapshot)
        if calls == 1:
            deploy_release.write_effect_lease(desired, replace(worker_lease, desired_revision="b" * 40))
            revision["value"] = "b" * 40
        elif calls == 4:
            deploy_release.write_effect_lease(desired, replace(worker_lease, desired_revision="c" * 40))
            revision["value"] = "c" * 40
        return result

    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revision["value"])
    monkeypatch.setattr(
        deploy_release,
        "materialize_revision",
        lambda _revision, output: shutil.copytree(desired, output),
    )
    monkeypatch.setattr(deploy_release, "validate_effect_lease_head", validate_with_worker_renewal)
    monkeypatch.setattr(deploy_release, "observed_tree", lambda _ref, output: output.mkdir(parents=True) or None)

    def publish(_ref, directory, _parent, _message):
        observed_publication.update(deploy_release.directory_files(directory))
        return "c" * 40

    monkeypatch.setattr(deploy_release, "publish_tree", publish)
    receipt = receipt_resource(
        "terraform",
        "application",
        {"revision": "a" * 40, "unitBlob": "application-blob"},
    )
    result = deploy_release.publish_observation_cas(
        "observed/dev",
        "application",
        receipt,
        units["application"],
        {},
        "a" * 40,
        desired_ref="deploy/dev",
        expected_uid=units["application"].metadata.uid,
        lease_token=application_lease.token,
        lease_snapshot=application_lease.snapshot,
    )

    assert result == "c" * 40
    assert calls >= 3
    receipt_path = next(path for path in observed_publication if path.startswith("units/application."))
    assert yaml.safe_load(observed_publication[receipt_path])["spec"]["desired"]["revision"] == "b" * 40
    assert deploy_release.load_desired_effect_leases(desired)["worker"].token == worker_lease.token
    deploy_release.publish_teardown_observation_cas(
        "observed/dev",
        "application",
        units["application"].metadata.uid,
        1,
        "b" * 40,
        desired_ref="deploy/dev",
        lease_token=application_lease.token,
        lease_snapshot=application_lease.snapshot,
    )
    evidence = json.loads(
        observed_publication[f".gitopsctr/teardowns/units/application.{units['application'].metadata.uid}.1.json"]
    )
    assert evidence["desiredRevision"] == "c" * 40


def test_desired_mutation_cannot_drop_active_effect_lease(tmp_path, monkeypatch):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    lease = deploy_release.EffectLease(
        unit_name="application",
        uid="d1-application",
        token="lease-runner-a",
        owner="runner-a",
        desired_revision="a" * 40,
        expires_at=100,
    )
    deploy_release.write_effect_lease(current, lease)
    candidate.mkdir()
    monkeypatch.setattr(deploy_release, "effect_lease_now", lambda: 1)
    monkeypatch.setattr(
        deploy_release, "materialize_revision", lambda _revision, output: shutil.copytree(current, output)
    )

    with pytest.raises(deploy_release.EffectLeaseUnavailable, match="drop or alter"):
        deploy_release.validate_effect_leases_preserved("deploy/dev", "a" * 40, candidate)

    deploy_release.write_effect_lease(candidate, lease)
    deploy_release.validate_effect_leases_preserved("deploy/dev", "a" * 40, candidate)


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": "other"},
        {
            "name": "other",
            "uid": "uid-1",
            "labels": {"gitopsctr.io/partition": "application"},
        },
    ],
)
def test_opaque_cleanup_metadata_rejects_explicit_name_mismatch(metadata):
    with pytest.raises(deploy_release.OperationError, match="mismatched name"):
        deploy_release.opaque_cleanup_metadata(
            "frontend",
            {"metadata": metadata, "payload": {"name": "frontend"}},
            "b" * 40,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": "frontend", "uid": "uid-1", "lifecycle": {"management": {"mode": "invalid"}}},
        {
            "name": "frontend",
            "uid": "uid-1",
            "ownerReferences": [{"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "name": "owner"}],
        },
    ],
)
def test_opaque_cleanup_metadata_fails_closed_for_malformed_authority(metadata):
    with pytest.raises(deploy_release.OperationError, match="opaque cleanup metadata"):
        deploy_release.opaque_cleanup_metadata(
            "frontend",
            {"metadata": metadata, "payload": {"name": "frontend"}},
            "b" * 40,
        )


def _source_resolution_fixture(tmp_path: Path, previous_revision: str, input_hash: str):
    source = tmp_path / "source"
    current = tmp_path / "current"
    source.mkdir()
    (source / "main.tf").write_text("terraform {}\n")
    specification = deploy_release.parse_authored_unit_document(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "aws-application"},
            "spec": {"source": {"path": "."}, "terraform": {"backend": {}, "variables": {}}},
        },
        "aws-application",
    )
    _write_json(
        current / "units/aws-application.json",
        _terraform_desired_document(revision=previous_revision, input_hash=input_hash),
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
        deploy_release.SourceRevisionPolicy(
            unavailable_when=deploy_release.SourceRevisionUnavailableWhen.MISSING,
        ),
    )

    assert result.source is not None
    assert result.source.revision == previous_revision
    assert result.disposition is deploy_release.SourceResolutionDisposition.UNCHANGED


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

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current)

    assert result.source is not None
    assert result.source.revision == previous_revision
    assert result.disposition is deploy_release.SourceResolutionDisposition.UNCHANGED
    assert result.refresh_reason is None


def test_outside_candidate_history_refreshes_dangling_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(deploy_release, "commit_is_ancestor", lambda *_args: False)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.disposition is deploy_release.SourceResolutionDisposition.REVISION_REFRESHED
    assert result.refresh_reason is not None
    assert "outside candidate history" in result.refresh_reason


def test_matching_input_hash_refreshes_unavailable_previous_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:same")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:same")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: False)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.disposition is deploy_release.SourceResolutionDisposition.REVISION_REFRESHED


def test_changed_input_hash_uses_candidate_source_revision(tmp_path, monkeypatch):
    previous_revision = "a" * 40
    candidate_revision = "b" * 40
    source, current, specification = _source_resolution_fixture(tmp_path, previous_revision, "sha256:old")
    monkeypatch.setattr(deploy_release, "unit_input_hash", lambda *_args: "sha256:new")
    monkeypatch.setattr(deploy_release, "commit_is_available", lambda _revision: True)

    result = deploy_release.resolved_unit_source(specification, source, candidate_revision, current)

    assert result.source is not None
    assert result.source.revision == candidate_revision
    assert result.disposition is deploy_release.SourceResolutionDisposition.INPUTS_CHANGED


def test_source_less_unit_remains_without_a_resolved_source(tmp_path):
    specification = deploy_release.parse_authored_unit_document(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "FrontendS3Cloudfront",
            "metadata": {"name": "frontend"},
            "spec": {},
        },
        "frontend",
    )

    result = deploy_release.resolved_unit_source(specification, tmp_path, "b" * 40, tmp_path / "current")

    assert result.source is None
    assert result.disposition is deploy_release.SourceResolutionDisposition.UNCHANGED


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
    previous = deploy_release.parse_desired_unit_document(
        _terraform_desired_document(
            "application",
            resolved_inputs={"promotions": {"application#/image": "same", "application#/tag": "old"}},
        ),
        "application",
    )
    current = deploy_release.parse_desired_unit_document(
        _terraform_desired_document(
            "application",
            resolved_inputs={"promotions": {"application#/image": "same", "application#/tag": "new"}},
        ),
        "application",
    )

    explanation = deploy_release.classify_unit_change(previous, current, "b" * 40)

    assert explanation.causes == ("reviewed promotion inputs changed: application#/tag",)


def test_promotion_reference_materializes_from_source_desired_unit(tmp_path):
    promotion = tmp_path / "promotion"
    source_unit = promotion / "units/aws-application.json"
    _write_json(
        source_unit,
        _terraform_desired_document(variables={"control_image_uri": "registry.example/control@sha256:" + "1" * 64}),
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
        _terraform_desired_document(variables={"environment": "prod"}),
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
        _terraform_desired_document(variables={"image": "release"}),
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
        _terraform_desired_document("application"),
    )
    with pytest.raises(deploy_release.OperationError, match="cannot resolve pointer"):
        deploy_release.resolve_template(
            {"fromPromotion": {"pointer": "/missing", "dryFallback": "preview"}},
            *arguments,
            promotion=context,
            target_unit="application",
            dry=True,
        )


def test_promotion_requires_every_source_unit_to_be_clean(tmp_path):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    first = desired / "units/first.json"
    second = desired / "units/second.json"
    _write_json(first, _terraform_desired_document("first"))
    _write_json(second, _terraform_desired_document("second"))
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


def test_status_includes_commit_subjects(tmp_path, monkeypatch, capsys):
    revisions = {"deploy/dev": "a" * 40, "observed/dev": "b" * 40}
    subjects = {"a" * 40: "Prepare desired state", "b" * 40: "Observe frontend"}
    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", lambda ref, _output: revisions[ref])
    monkeypatch.setattr(deploy_release, "load_environment_specifications", lambda *_args: {})
    monkeypatch.setattr(deploy_release, "commit_subject", lambda _root, revision: subjects.get(revision))
    args = deploy_release.build_parser().parse_args(["status", "--environment", "dev"])

    args.handler(args)

    output = capsys.readouterr()
    assert output.out == ""
    assert "DESIRED  deploy/dev at aaaaaaaaaaaa (Prepare desired state)" in output.err
    assert "OBSERVED observed/dev at bbbbbbbbbbbb (Observe frontend)" in output.err


def test_reconciliation_statuses_identify_clean_ready_and_waiting_units(tmp_path):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    clean_unit = desired / "units/application-images.json"
    _write_json(clean_unit, _terraform_desired_document("application-images"))
    _write_json(desired / "units/aws-application.json", _terraform_desired_document("aws-application"))
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
    unit = _terraform_desired_document(driver_version=deploy_release.DRIVER_VERSIONS["terraform"] + 1)
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
    monkeypatch.setattr(
        deploy_release,
        "source_change_evidence",
        lambda *_args: (
            ("abc123 Use default API Gateway stage",),
            ("M\tinfra/deploy/main.tf",),
        ),
    )

    previous_resource = deploy_release.RESOURCE_CATALOG.parse_unit(
        _terraform_desired_document(
            input_hash="sha256:old",
            driver_version=1,
            variables={"environment": "old"},
            resolved_inputs={"receipts": {"images": "old"}},
        ),
        profile="desired",
        expected_name="aws-application",
    )
    current_resource = deploy_release.RESOURCE_CATALOG.parse_unit(
        _terraform_desired_document(
            revision="b" * 40,
            input_hash="sha256:new",
            driver_version=2,
            variables={"environment": "dev"},
            resolved_inputs={"receipts": {"images": "new"}},
        ),
        profile="desired",
        expected_name="aws-application",
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


def test_compact_approval_card_shows_driver_change_evidence_and_write_boundary(tmp_path, monkeypatch, capsys):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    _write_json(
        desired / "units/aws-application.json",
        _terraform_desired_document(),
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


def test_status_without_environment_delegates_to_registry_inventory(monkeypatch):
    captured = []
    monkeypatch.setattr(deploy_release, "inspect_resources", lambda root, args: captured.append((root, args.selector)))

    args = deploy_release.build_parser().parse_args(["status"])
    args.handler(args)

    assert captured == [(deploy_release.REPOSITORY_ROOT, "environments")]


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
                },
                "effectLease": None,
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
                },
                "effectLease": None,
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
            "spec": {
                "environmentDefaults": {"refs": {"candidate": "changes/{environment}/{operation}/{id}"}},
                "effectLease": None,
            },
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
        "verify_gated_candidate",
        lambda _candidate_revision, _target_revision: deploy_release.GatedCandidate(
            existing_revision, target_revision, target_revision
        ),
    )
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


def _unit(name: str, inputs: dict | None = None) -> dict:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": name},
        "spec": {
            "source": {"path": "infra/deploy"},
            **({"inputs": inputs} if inputs else {}),
        },
    }


def _unit_resource(name: str, inputs: dict | None = None):
    return deploy_release.parse_authored_unit_document(_unit(name, inputs), name)


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
