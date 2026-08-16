"""Generic metadata-based Unit finalization and cleanup safety tests."""

import shutil
import tempfile
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredOwnerReference
from gitopsctr.contrib.drivers.terraform import AppliedTerraformModel, TerraformResultModel
from gitopsctr.driver import DriverError, ReconciliationOutput, TeardownResult, TeardownUnsupported
from gitopsctr.errors import OperationError
from tests.conftest import write_test_document


def _write(path: Path, document: object) -> None:
    write_test_document(path, document)


def _terraform_unit(
    name: str,
    uid: str,
    source_revision: str = "a" * 40,
    owner: DesiredOwnerReference | None = None,
) -> dict[str, object]:
    metadata = controller.ResourceMetadata(
        name=name,
        uid=uid,
        ownerReferences=[owner] if owner is not None else None,
    )
    if owner is None:
        metadata = metadata.with_partition("application")
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": metadata.document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": source_revision,
                "inputHash": "sha256:" + "1" * 64,
                "driverVersion": controller.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
        },
    }


def _reconcile_args(name: str = "application", **overrides: object) -> Namespace:
    values = {
        "unit": name,
        "environment": "dev",
        "desired_ref": "deploy/dev",
        "observed_ref": "observed/dev",
        "desired_revision": "c" * 40,
        "plan": False,
        "report": None,
        "reapply": False,
        "verbose": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _stub_effect_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    def acquire(_ref: str, revision: str, unit_name: str, uid: str, **_kwargs: object):
        lease = controller.EffectLease(
            unit_name=unit_name,
            uid=uid,
            token="lease-test",
            owner="test-runner",
            desired_revision=revision,
        )
        return controller.EffectLeaseAcquisition(lease=lease, revision=revision)

    monkeypatch.setattr(controller, "acquire_effect_lease", acquire)
    monkeypatch.setattr(controller, "release_effect_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "start_effect_lease_heartbeat",
        lambda *_args, **_kwargs: pytest.fail("non-expiring leases must not start periodic renewal"),
    )
    monkeypatch.setattr(controller, "validate_effect_lease_head", lambda _ref, *_args, **_kwargs: "c" * 40)
    monkeypatch.setattr(
        controller,
        "rebase_effect_completion",
        lambda _ref, acquisition, unit_name, uid, root, **_kwargs: (
            replace(
                acquisition,
                lease=replace(
                    acquisition.lease,
                    snapshot=controller.effect_lease_snapshot(root, unit_name, uid),
                ),
            )
            if acquisition.lease.snapshot is None
            else acquisition
        ),
    )


def _prepare_finalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: dict[str, object]):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    desired.mkdir()
    observed.mkdir()
    name = str(document["metadata"]["name"])  # type: ignore[index]
    _write(desired / f"units/{name}.yaml", document)
    desired_revision = "c" * 40
    publications: list[Path] = []
    teardown_publications: list[Path] = []

    def observed_tree(ref: str, output: Path):
        source = desired if ref == "deploy/dev" else observed
        shutil.copytree(source, output)
        return desired_revision if ref == "deploy/dev" else None

    def publish_tree(_ref: str, directory: Path, _parent: str | None, _message: str, **_kwargs: object):
        snapshot = tmp_path / f"observed-{len(teardown_publications)}"
        shutil.copytree(directory, snapshot)
        teardown_publications.append(snapshot)
        shutil.rmtree(observed)
        shutil.copytree(directory, observed)
        return "e" * 40

    def publish_desired(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / f"published-{len(publications)}"
        shutil.copytree(candidate, snapshot)
        publications.append(snapshot)
        shutil.rmtree(desired)
        shutil.copytree(candidate, desired)
        return "d" * 40, None

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "observed_tree", observed_tree)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: desired_revision)
    monkeypatch.setattr(controller, "resolve_ref", lambda _ref, _revision=None: desired_revision)

    def materialize(revision: str, output: Path) -> None:
        if revision == desired_revision:
            shutil.copytree(desired, output)
        else:
            output.mkdir(parents=True)

    monkeypatch.setattr(controller, "materialize_revision", materialize)
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
    monkeypatch.setattr(controller, "publish_tree", publish_tree)
    monkeypatch.setattr(controller, "publish_desired_change", publish_desired)
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: "deploy/dev")
    _stub_effect_lease(monkeypatch)
    return desired, observed, publications, teardown_publications


def _mark(document: dict[str, object], name: str = "application") -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gitopsctr-mark-") as directory:
        path = Path(directory) / f"{name}.json"
        _write(path, document)
        resource = controller.load_desired_unit(path, name)
        marked = controller.mark_resource_for_deletion(resource)
        return controller.serialize_unit_document(marked, profile="desired")


def test_reconcile_deleting_unit_runs_teardown_records_observed_evidence_and_removes_unit(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, publications, teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"destroyed": True})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    assert controller.command_reconcile(_reconcile_args()) is True
    assert len(teardown_calls) == 1
    assert len(publications) == 1
    assert teardown_publications
    assert not (desired / "units/application.yaml").exists()
    evidence = controller.load_teardown_evidence(observed, "application", "d1-application", 1)
    assert evidence is not None
    assert evidence.effect_lease_ref == "deploy/dev"
    assert evidence.details == {"destroyed": True}


def test_reconcile_reports_explicit_recovery_when_non_expiring_lease_release_is_deferred(tmp_path, monkeypatch, capsys):
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(
        tmp_path, monkeypatch, _terraform_unit("application", "d1-application")
    )
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "reconcile",
        lambda *_args, **_kwargs: ReconciliationOutput(
            result=TerraformResultModel(
                applied=AppliedTerraformModel(sourceRevision="a" * 40),
                outputs={},
            )
        ),
    )
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("lease store unavailable")),
    )

    assert controller.command_reconcile(_reconcile_args()) is True

    stderr = capsys.readouterr().err
    assert "explicit recovery remains available" in stderr
    assert "pending expiry" not in stderr
    assert (desired / "units/application.yaml").exists()


def test_reconcile_plan_never_runs_deletion_teardown_or_publication(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, publications, teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda *_args, **_kwargs: pytest.fail("plan must not run teardown"),
    )

    assert controller.command_reconcile(_reconcile_args(plan=True)) is False

    assert (desired / "units/application.yaml").exists()
    assert publications == []
    assert teardown_publications == []


@pytest.mark.parametrize(
    "failure",
    [
        DriverError("temporary teardown failure"),
        controller.subprocess.CalledProcessError(1, ["terraform", "destroy"]),
    ],
    ids=["driver-error", "subprocess-error"],
)
def test_reconcile_driver_failure_releases_lease_for_automatic_retry(tmp_path, monkeypatch, failure):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    releases: list[str] = []
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda _ref, _name, token, *_args, **_kwargs: releases.append(token),
    )
    attempts = 0

    def teardown(_driver, _context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure
        return TeardownResult()

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    with pytest.raises(type(failure)):
        controller.command_reconcile(_reconcile_args())
    assert releases == ["lease-test"]
    assert (desired / "units/application.yaml").exists()

    assert controller.command_reconcile(_reconcile_args()) is True
    assert attempts == 2


def test_reconcile_invalid_teardown_result_releases_lease(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    releases: list[str] = []
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda _ref, _name, token, *_args, **_kwargs: releases.append(token),
    )
    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", lambda *_args: object())

    with pytest.raises(DriverError, match="invalid result"):
        controller.command_reconcile(_reconcile_args())

    assert releases == ["lease-test"]
    assert (desired / "units/application.yaml").exists()


def test_reconcile_deleting_unit_without_effect_leases_runs_teardown(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "acquire_effect_lease",
        lambda *_args, **_kwargs: pytest.fail("effect leases are disabled"),
    )
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, _context: TeardownResult(details={"destroyed": True}),
    )

    assert controller.command_reconcile(_reconcile_args()) is True
    assert not (desired / "units/application.yaml").exists()
    evidence = controller.load_teardown_evidence(observed, "application", "d1-application", 1)
    assert evidence is not None
    assert evidence.effect_lease_ref is None


def test_reconcile_retry_releases_separate_store_lease_after_desired_removal(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    controller.write_resource_incarnation_tombstone(
        desired,
        controller.ResourceIncarnationTombstone(
            api_version=controller.UNIT_API_VERSION,
            kind="Terraform",
            name="application",
            uid="d1-application",
            deletion_generation=1,
            effect_lease_ref="gitopsctr/leases",
        ),
    )
    controller.write_resource_incarnation_tombstone(
        desired,
        controller.ResourceIncarnationTombstone(
            api_version=controller.UNIT_API_VERSION,
            kind="Terraform",
            name="application",
            uid="d1-older-application",
            deletion_generation=1,
        ),
    )
    controller.write_effect_lease(
        lease_root,
        controller.EffectLease(
            unit_name="application",
            uid="d1-application",
            token="lease-deletion",
            owner="test",
            desired_revision="c" * 40,
        ),
    )
    released: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: "gitopsctr/leases")
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(controller, "resolve_ref", lambda *_args, **_kwargs: "c" * 40)
    monkeypatch.setattr(controller, "materialize_revision", lambda _revision, output: shutil.copytree(desired, output))
    monkeypatch.setattr(
        controller,
        "observed_tree",
        lambda _ref, output: (shutil.copytree(desired, output), "c" * 40)[1],
    )
    monkeypatch.setattr(controller, "_effect_lease_store_root", lambda *_args, **_kwargs: (lease_root, "e" * 40))
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda desired_ref, name, token, _uid=None, **kwargs: released.append(
            (desired_ref, name, kwargs.get("lease_ref"))
        ),
    )

    assert controller.command_reconcile(_reconcile_args()) is True
    assert released == [("deploy/dev", "application", "gitopsctr/leases")]


def test_reconcile_retries_from_observed_evidence_without_repeating_teardown(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    teardown_calls = []
    lease_configuration = {"value": "gitopsctr/old-leases"}
    lease_refs: list[str | None] = []
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: lease_configuration["value"])
    acquire = controller.acquire_effect_lease

    def record_acquire(*args, **kwargs):
        lease_refs.append(kwargs.get("lease_ref"))
        return acquire(*args, **kwargs)

    monkeypatch.setattr(controller, "acquire_effect_lease", record_acquire)

    def teardown(_driver, context):
        teardown_calls.append(context)
        return TeardownResult(details={"attempt": len(teardown_calls)})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)
    original_publish = controller.publish_desired_change
    monkeypatch.setattr(
        controller,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash after evidence")),
    )
    with pytest.raises(RuntimeError, match="crash after evidence"):
        controller.command_reconcile(_reconcile_args())
    assert len(teardown_calls) == 1
    evidence = controller.load_teardown_evidence(observed, "application", "d1-application", 1)
    assert evidence is not None
    assert evidence.effect_lease_ref == "gitopsctr/old-leases"

    lease_configuration["value"] = "gitopsctr/new-leases"
    monkeypatch.setattr(controller, "publish_desired_change", original_publish)
    assert controller.command_reconcile(_reconcile_args()) is True
    assert len(teardown_calls) == 1
    assert lease_refs == ["gitopsctr/old-leases", "gitopsctr/old-leases"]
    tombstone = controller.load_resource_incarnation_evidence(desired)[0]
    assert tombstone.effect_lease_ref == "gitopsctr/old-leases"
    assert not (desired / "units/application.yaml").exists()


def test_reconcile_rejects_changed_retained_resource_digest(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)
    changed = controller.load_desired_unit(desired / "units/application.yaml", "application")
    changed_document = controller.serialize_unit_document(changed, profile="desired")
    changed_document["spec"]["terraform"]["variables"] = {"changed": True}  # type: ignore[index]
    _write(desired / "units/application.yaml", changed_document)

    with pytest.raises(OperationError, match="changed after deletion started"):
        controller.command_reconcile(_reconcile_args())


def test_teardown_evidence_requires_uid_and_generation_in_filename(tmp_path):
    evidence = controller.TeardownEvidence(
        unit_name="application",
        uid="d1-application",
        deletion_generation=1,
        desired_revision="a" * 40,
        effect_lease_ref=None,
        details={},
    )
    _write(tmp_path / ".gitopsctr/teardowns/units/application.json", evidence.document())

    with pytest.raises(OperationError, match="filename does not match its fence"):
        controller.load_teardown_evidence(tmp_path, "application", "d1-application", 1)


def test_teardown_evidence_requires_explicit_nullable_effect_lease_ref():
    evidence = controller.TeardownEvidence(
        unit_name="application",
        uid="d1-application",
        deletion_generation=1,
        desired_revision="a" * 40,
    ).document()
    del evidence["effectLeaseRef"]

    with pytest.raises(ValueError, match="invalid teardown evidence envelope"):
        controller.TeardownEvidence.from_document(evidence, "application")


def test_reconcile_blocks_owned_children_and_dependency_dependents(tmp_path, monkeypatch):
    parent = _terraform_unit("parent", "d1-parent")
    child = _terraform_unit(
        "child",
        "d1-child",
        owner=DesiredOwnerReference(
            apiVersion="unit.gitopsctr.io/v1", kind="Terraform", name="parent", uid="d1-parent"
        ),
    )
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(
        tmp_path, monkeypatch, _mark(parent, "parent")
    )
    _write(desired / "units/child.yaml", _mark(child, "child"))
    assert controller.command_reconcile(_reconcile_args(name="parent")) is False
    assert (desired / "units/parent.yaml").exists()


def test_reconcile_blocks_observation_dependents_before_teardown(tmp_path, monkeypatch):
    parent = _mark(_terraform_unit("parent", "d1-parent"), "parent")
    child = _terraform_unit("child", "d1-child")
    child["spec"]["resolvedInputs"] = {"receipts": {"parent": "receipt-parent"}}  # type: ignore[index]
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, parent)
    _write(desired / "units/child.yaml", child)
    assert controller.command_reconcile(_reconcile_args(name="parent")) is False
    assert (desired / "units/parent.yaml").exists()


def test_reconcile_unsupported_teardown_remains_wait_with_desired_intact(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)

    class NoTeardownCapability:
        pass

    monkeypatch.setattr(controller, "TeardownCapability", NoTeardownCapability)
    assert controller.command_reconcile(_reconcile_args()) is False
    assert (desired / "units/application.yaml").exists()


def test_reconcile_delivery_without_teardown_remains_wait_with_desired_intact(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    desired, _observed, _publications, _teardown_publications = _prepare_finalization(tmp_path, monkeypatch, document)

    def teardown(_driver, _context):
        raise TeardownUnsupported("delivery mode has no controller-owned teardown")

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    assert controller.command_reconcile(_reconcile_args()) is False
    assert (desired / "units/application.yaml").exists()


def test_opaque_cleanup_resolution_remains_supported(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    uid = "d1-opaque-application"
    controller.write_opaque_cleanup_root(
        current,
        "application",
        controller.mark_opaque_cleanup_for_deletion(
            controller.OpaqueCleanupRoot(
                path=current / ".gitopsctr/cleanup/units/application.json",
                payload="unparseable",
                metadata=controller.ResourceMetadata(
                    name="application",
                    uid=uid,
                ).with_partition("application"),
                source=None,
            )
        ),
    )
    published: list[Path] = []

    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(
        controller, "observed_tree", lambda _ref, output: (shutil.copytree(current, output), "c" * 40)[1]
    )
    monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment, candidate, *_args, **_kwargs):
        snapshot = tmp_path / "published"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(controller, "publish_desired_change", publish)
    args = Namespace(
        environment="dev",
        unit="application",
        uid=uid,
        deletion_generation=1,
        reason="external cleanup confirmed",
        confirm_external_cleanup=True,
        desired_ref=None,
        candidate_ref=None,
        dry=False,
    )
    assert controller.command_resolve_opaque_unit(args) is True
    assert controller.load_desired_cleanup_roots(published[0]) == {}
    assert not any("deletion" in path.name for path in published[0].rglob("*"))
