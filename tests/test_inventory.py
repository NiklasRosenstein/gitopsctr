from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from gitopsctr.api import GVK
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    DesiredResourceMetadata,
    DesiredStackDocument,
    DesiredStackSpec,
    StackActiveProjection,
    StackProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackTemplateReference,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.inventory import (
    InventoryError,
    InventoryObservationState,
    InventorySession,
    ReconciliationState,
    evaluate_relationships,
)
from gitopsctr.operational import materialization_tree_digest
from gitopsctr.plane_repositories import PlaneRepositorySession
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import ObservationDefinition, ResourcePlane, ResourceRegistry
from gitopsctr.resources import desired_unit_binding_digest
from gitopsctr.state import GitRefSnapshot, GitStateStore


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        (
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            *args,
        ),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def project_document() -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "inventory-test"},
        "spec": {"effectLease": None},
    }


def environment_document(name: str) -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Environment",
        "metadata": {"name": name},
        "spec": {},
    }


def desired_terraform(name: str) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "labels": {"gitopsctr.io/partition": "application"},
        },
        "spec": {"source": {"path": ".", "revision": "a" * 40, "inputHash": "sha256:inputs"}},
    }


def stack_template(name: str, *, desired: bool) -> dict[str, object]:
    metadata: dict[str, object] = {"name": name}
    authored = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": metadata,
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "application": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": "."}},
                }
            },
        },
    }
    if not desired:
        return authored
    parsed = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    metadata.update({"uid": f"uid-{name}", "labels": {"gitopsctr.io/partition": "application"}})
    specification = cast(dict[str, object], authored["spec"])
    specification.update(
        {
            "contentDigest": parsed.spec.semantic_content_digest(),
            "acquisition": {
                "documentDigest": "sha256:" + "b" * 64,
                "requestedSource": {"fromInput": {}},
                "resolvedSource": {"fromInput": {}},
            },
            "sourceContext": {"repository": ".", "revision": "a" * 40},
        }
    )
    return authored


def stack(
    name: str,
    template: str,
    *,
    desired: bool,
    template_document: dict[str, object] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"name": name}
    if desired:
        metadata.update({"uid": f"uid-{name}", "labels": {"gitopsctr.io/partition": "application"}})
    specification: dict[str, object] = {"template": template, "parameters": {}}
    if desired:
        assert template_document is not None
        template_metadata = cast(dict[str, object], template_document["metadata"])
        template_spec = cast(dict[str, object], template_document["spec"])
        template_uid = cast(str, template_metadata["uid"])
        content_digest = cast(str, template_spec["contentDigest"])
        projection = StackProjection.build(
            stack_uid=cast(str, metadata["uid"]),
            template_uid=template_uid,
            template_content_digest=content_digest,
            context_digest="sha256:" + "c" * 64,
            units={
                "application": StackProjectionUnit(
                    apiVersion="unit.gitopsctr.io/v1",
                    kind="Terraform",
                    spec=JsonObjectValue({"source": {"path": "."}}),
                    dependsOn=[],
                )
            },
        )
        desired = DesiredStackDocument(
            apiVersion="gitopsctr.io/v1",
            kind="Stack",
            metadata=DesiredResourceMetadata(
                name=name,
                uid=cast(str, metadata["uid"]),
                labels={"gitopsctr.io/partition": "application"},
            ),
            spec=DesiredStackSpec(
                templateRef=StackTemplateReference(
                    name=template,
                    uid=template_uid,
                    contentDigest=content_digest,
                ),
                structuralProjection=projection,
            ),
        )
        return CORE_CONTRACTS["stack-desired"].dump(desired)
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": metadata,
        "spec": specification,
    }


def receipt(name: str, unit_blob: str) -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Receipt",
        "metadata": {"name": name},
        "spec": {
            "subject": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "name": name},
            "desired": {"unitBlob": unit_blob},
        },
        "status": {
            "controller": {},
            "result": {"applied": {"sourceRevision": "a" * 40}, "outputs": {}},
        },
    }


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    working = tmp_path / "working"
    git(tmp_path, "init", "--bare", str(remote))
    working.mkdir()
    git(working, "init", "-b", "main")
    git(working, "remote", "add", "origin", str(remote))
    write_json(working / "gitopsctr.yaml", project_document())
    for name in ("dev", "staging"):
        write_json(working / f"deployment/environments/{name}/environment.yaml", environment_document(name))
        write_json(
            working / f"deployment/environments/{name}/units/shared.yaml",
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": {"name": "shared"},
                "spec": {"source": {"path": "."}},
            },
        )
        write_json(working / f"deployment/environments/{name}/stacks/web.yaml", stack("web", "web", desired=False))
    write_json(working / "deployment/stack-templates/web.yaml", stack_template("web", desired=False))
    commit(working, "source")
    git(working, "push", "-u", "origin", "main")

    git(working, "checkout", "-b", "desired")
    write_json(working / "units/application.yaml", desired_terraform("application"))
    deleting = desired_terraform("deleting")
    deleting["metadata"]["deletion"] = {"generation": 1, "resourceDigest": "sha256:" + "b" * 64}  # type: ignore[index]
    write_json(working / "units/deleting.yaml", deleting)
    write_json(working / "materialized/external/manifest.yaml", {"apiVersion": "v1", "kind": "ConfigMap"})
    external_materialization_digest = materialization_tree_digest(working / "materialized/external")
    write_json(
        working / "units/external.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "KubernetesManifests",
            "metadata": {
                "name": "external",
                "uid": "uid-external",
                "labels": {"gitopsctr.io/partition": "application"},
            },
            "spec": {
                "source": {"path": ".", "revision": "a" * 40, "inputHash": "sha256:inputs"},
                "materialize": {"type": "plain"},
                "delivery": {"mode": "external"},
                "materialization": {
                    "path": "materialized/external",
                    "digest": external_materialization_digest,
                    "mediaType": "application/yaml",
                    "metadata": {"renderer": "plain", "inventory": []},
                },
            },
        },
    )
    desired_template = stack_template("web", desired=True)
    write_json(working / "stack-templates/web.yaml", desired_template)
    write_json(working / "stacks/web.yaml", stack("web", "web", desired=True, template_document=desired_template))
    write_json(
        working / "promotion.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Promotion",
            "metadata": {"name": "dev"},
            "spec": {
                "source": {
                    "environment": "dev",
                    "desiredRef": "gitopsctr/desired/dev",
                    "desiredRevision": "a" * 40,
                    "observedRef": "gitopsctr/observed/dev",
                    "observedRevision": None,
                },
                "specificationRevision": "a" * 40,
            },
        },
    )
    desired_revision = commit(working, "desired")
    git(working, "push", "origin", f"{desired_revision}:refs/heads/gitopsctr/desired/dev")
    git(working, "push", "origin", f"{desired_revision}:refs/heads/gitopsctr/desired/staging")
    desired_blob = git(working, "rev-parse", f"{desired_revision}:units/application.yaml")

    git(working, "checkout", "-b", "observed")
    for path in (working / "units").glob("*"):
        path.unlink()
    write_json(working / "units/application.yaml", receipt("application", desired_blob))
    observed_revision = commit(working, "observed")
    git(working, "push", "origin", f"{observed_revision}:refs/heads/gitopsctr/observed/dev")
    git(working, "push", "origin", f"{observed_revision}:refs/heads/gitopsctr/observed/staging")
    git(working, "checkout", "main")
    return working


def test_source_environment_discovery_preserves_raw_document_and_provenance(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        records = inventory.resources("environments")

    assert [record.name for record in records] == ["dev", "staging"]
    assert all(record.plane is ResourcePlane.SOURCE for record in records)
    assert all(record.ref is None and record.revision is None for record in records)
    assert records[0].document["metadata"] == {"name": "dev"}
    assert records[0].path.as_posix() == "deployment/environments/dev/environment.yaml"


def test_inventory_discovers_every_registered_initial_placement(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        source_project = inventory.resources("project", plane=ResourcePlane.SOURCE)
        source_units = inventory.resources("unit", environment="dev", plane=ResourcePlane.SOURCE)
        source_stacks = inventory.resources("stack", environment="dev", plane=ResourcePlane.SOURCE)
        source_templates = inventory.resources("stacktemplate", plane=ResourcePlane.SOURCE)
        desired_units = inventory.resources("unit", environment="dev", ref="gitopsctr/desired/dev")
        desired_stacks = inventory.resources("stack", environment="dev", ref="gitopsctr/desired/dev")
        desired_templates = inventory.resources("stacktemplate", environment="dev", ref="gitopsctr/desired/dev")
        promotions = inventory.resources("promotion", environment="dev", ref="gitopsctr/desired/dev")
        receipts = inventory.resources("receipt", environment="dev", ref="gitopsctr/observed/dev")
        artifacts = inventory.resources(
            "artifact", environment="dev", plane=ResourcePlane.OBSERVED, ref="gitopsctr/observed/dev"
        )

    assert [item.name for item in source_project] == ["inventory-test"]
    assert [item.name for item in source_units] == ["shared"]
    assert [item.name for item in source_stacks] == ["web"]
    assert [item.name for item in source_templates] == ["web"]
    assert {item.name for item in desired_units} == {"application", "deleting", "external"}
    assert [item.name for item in desired_stacks] == ["web"]
    assert [item.name for item in desired_templates] == ["web"]
    assert [item.name for item in promotions] == ["dev"]
    assert [item.name for item in receipts] == ["application"]
    assert artifacts == ()


def test_stack_summary_uses_exact_active_bindings_and_surfaces_mismatch(repository: Path):
    git(repository, "checkout", "desired")
    stack_document = json.loads((repository / "stacks/web.yaml").read_text())
    application = desired_terraform("application")
    application["metadata"]["ownerReferences"] = [  # type: ignore[index]
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "name": "web",
            "uid": "uid-web",
        }
    ]
    application["metadata"].pop("labels")  # type: ignore[union-attr]
    unrelated = desired_terraform("unrelated")
    unrelated["metadata"]["ownerReferences"] = application["metadata"]["ownerReferences"]  # type: ignore[index]
    unrelated["metadata"].pop("labels")  # type: ignore[union-attr]
    application_resource = RESOURCE_REGISTRY.contract(GVK("unit.gitopsctr.io/v1", "Terraform"), "desired").parse(
        application
    )
    application_digest = desired_unit_binding_digest(application_resource)
    parsed_stack = CORE_CONTRACTS["stack-desired"].parse(stack_document)
    active = StackActiveProjection.build(
        source_projection_digest=parsed_stack.spec.structuralProjection.identity.projectionDigest,
        projection_context_digest=parsed_stack.spec.structuralProjection.identity.projectionContextDigest,
        units={
            "application": StackProjectionUnitBinding(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="application",
                uid="uid-application",
                desiredDigest=application_digest,
            )
        },
    )
    active_stack = replace(parsed_stack, spec=replace(parsed_stack.spec, activeProjection=active))
    write_json(repository / "units/application.yaml", application)
    write_json(repository / "units/unrelated.yaml", unrelated)
    write_json(repository / "stacks/web.yaml", CORE_CONTRACTS["stack-desired"].dump(active_stack))
    exact_revision = commit(repository, "active Stack child bindings")
    git(repository, "push", "origin", f"{exact_revision}:refs/heads/gitopsctr/desired/active-bindings")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        stack_record = inventory.resources(
            "stack",
            environment="dev",
            ref="gitopsctr/desired/active-bindings",
        )[0]
        summary = inventory.stack_inspection_summary(stack_record)
    assert summary.child_observations == ("STALE",)

    git(repository, "checkout", "desired")
    broken_binding = replace(active.units["application"], desiredDigest="sha256:" + "d" * 64)
    broken_active = StackActiveProjection.build(
        source_projection_digest=active.sourceProjectionDigest,
        projection_context_digest=active.projectionContextDigest,
        units={"application": broken_binding},
    )
    broken_stack = replace(parsed_stack, spec=replace(parsed_stack.spec, activeProjection=broken_active))
    write_json(repository / "stacks/web.yaml", CORE_CONTRACTS["stack-desired"].dump(broken_stack))
    broken_revision = commit(repository, "broken active Stack child binding")
    git(repository, "push", "origin", f"{broken_revision}:refs/heads/gitopsctr/desired/broken-bindings")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        stack_record = inventory.resources(
            "stack",
            environment="dev",
            ref="gitopsctr/desired/broken-bindings",
        )[0]
        summary = inventory.stack_inspection_summary(stack_record)
    assert summary.child_observations == ("BROKEN(application:mismatch)",)


def test_environment_local_stacktemplate_is_not_a_registered_source_representation(repository: Path):
    write_json(
        repository / "deployment/environments/dev/stack-templates/local.yaml",
        stack_template("local", desired=False),
    )
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        templates = inventory.resources("stacktemplate", plane=ResourcePlane.SOURCE)

    assert {template.name for template in templates} == {"web"}


def test_inventory_explicit_revision_and_duplicate_names_across_environments(repository: Path):
    old_revision = git(repository, "rev-parse", "refs/remotes/origin/gitopsctr/desired/dev")
    git(repository, "checkout", "desired")
    write_json(repository / "units/new.yaml", desired_terraform("new"))
    new_revision = commit(repository, "new desired")
    git(repository, "push", "origin", f"{new_revision}:refs/heads/gitopsctr/desired/dev")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        historical = inventory.resources("unit", environment="dev", ref="gitopsctr/desired/dev", revision=old_revision)
        current = inventory.resources("unit", environment="dev", ref="gitopsctr/desired/dev")
        dev = inventory.resources("unit", environment="dev", ref="gitopsctr/desired/dev", revision=old_revision)
        staging = inventory.resources("unit", environment="staging", ref="gitopsctr/desired/staging")

    assert "new" not in {item.name for item in historical}
    assert "new" in {item.name for item in current}
    applications = tuple(item for item in (*dev, *staging) if item.name == "application")
    assert [item.environment for item in applications] == ["dev", "staging"]
    assert tuple(item.name for item in historical) == tuple(
        item.name for item in sorted(historical, key=lambda item: (str(item.gvk), item.name, item.path))
    )


def test_git_plane_session_caches_ref_resolution_materialization_and_blob_ids(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    real_fetch = GitStateStore.fetch
    fetched: dict[str, int] = {}

    def moving_fetch(store: GitStateStore, ref: str) -> GitRefSnapshot:
        fetched[ref] = fetched.get(ref, 0) + 1
        if fetched[ref] == 1:
            return real_fetch(store, ref)
        return GitRefSnapshot(ref, "f" * 40)

    monkeypatch.setattr(GitStateStore, "fetch", moving_fetch)
    with PlaneRepositorySession(repository) as planes:
        first = planes.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev", allow_missing=True)
        repeated = planes.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/dev", allow_missing=False)
        shared_revision = planes.snapshot(ResourcePlane.DESIRED, "gitopsctr/desired/staging")

        assert first is repeated
        assert first.root == shared_revision.root
        assert first.revision == shared_revision.revision
        assert first.blob_ids[next(path for path in first.blob_ids if path.name == "application.yaml")]
        assert fetched["gitopsctr/desired/dev"] == 1


@pytest.mark.parametrize("strict_first", [False, True])
def test_git_plane_session_caches_missing_ref_independently_of_error_policy(
    repository: Path, monkeypatch: pytest.MonkeyPatch, strict_first: bool
):
    fetched: list[str] = []

    def missing_fetch(_store: GitStateStore, ref: str) -> GitRefSnapshot:
        fetched.append(ref)
        return GitRefSnapshot(ref, None)

    monkeypatch.setattr(GitStateStore, "fetch", missing_fetch)
    with PlaneRepositorySession(repository) as planes:
        if strict_first:
            with pytest.raises(OperationError, match="does not exist"):
                planes.snapshot(ResourcePlane.OBSERVED, "gitopsctr/observed/missing")
            empty = planes.snapshot(
                ResourcePlane.OBSERVED,
                "gitopsctr/observed/missing",
                allow_missing=True,
            )
        else:
            empty = planes.snapshot(
                ResourcePlane.OBSERVED,
                "gitopsctr/observed/missing",
                allow_missing=True,
            )
            with pytest.raises(OperationError, match="does not exist"):
                planes.snapshot(ResourcePlane.OBSERVED, "gitopsctr/observed/missing")

    assert empty.revision is None
    assert fetched == ["gitopsctr/observed/missing"]


def test_inventory_discovers_desired_units_and_evaluates_current_receipts(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        units = inventory.resources("units", environment="dev", ref="gitopsctr/desired/dev")
        receipts = inventory.resources("receipts", environment="dev", ref="gitopsctr/observed/dev")
        evaluation = inventory.evaluate_environment("dev")

    assert len(units) == 3
    assert len(receipts) == 1
    application = next(item for item in units if item.name == "application")
    application_state = next(item for item in evaluation.units if item.unit.name == "application")
    assert application.blob_id is not None
    assert application.document["kind"] == "Terraform"
    assert application_state.observation is InventoryObservationState.CURRENT
    assert application_state.reconciliation is ReconciliationState.CLEAN
    assert evaluation.receipts[0].observation is InventoryObservationState.CURRENT
    assert evaluation.receipts[0].unit == application

    by_name = {item.unit.name: item for item in evaluation.units}
    assert by_name["external"].observation is InventoryObservationState.NOT_APPLICABLE
    assert by_name["external"].reconciliation is ReconciliationState.MATERIALIZED
    assert by_name["deleting"].reconciliation is ReconciliationState.WAIT


def test_transition_block_takes_precedence_over_a_current_receipt(repository: Path):
    git(repository, "checkout", "desired")
    write_json(
        repository / ".gitopsctr/transition-blocks.json",
        {"schema": 1, "blocks": {"application": "driver transition requires cleanup"}},
    )
    revision = commit(repository, "retain blocked transition")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/dev")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        evaluation = inventory.evaluate_environment("dev")

    application = next(item for item in evaluation.units if item.unit.name == "application")
    assert application.observation is InventoryObservationState.CURRENT
    assert application.reconciliation is ReconciliationState.WAIT
    assert application.reason == "driver transition requires cleanup"


@pytest.mark.parametrize("failure", ("missing", "corrupt"))
def test_materialized_state_fails_closed_for_invalid_payload(repository: Path, failure: str):
    git(repository, "checkout", "desired")
    payload = repository / "materialized/external/manifest.yaml"
    if failure == "missing":
        payload.unlink()
    else:
        write_json(payload, {"apiVersion": "v1", "kind": "Secret"})
    revision = commit(repository, f"{failure} materialization")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/dev")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="materialization output|does not match its digest"):
            inventory.evaluate_environment("dev")


def test_environment_counts_do_not_treat_unapplied_authored_units_as_desired(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        counts = inventory.reconciliation_counts("dev")

    assert counts[ReconciliationState.WAIT] == 1


def test_environment_counts_include_retained_opaque_cleanup_roots(repository: Path):
    git(repository, "checkout", "desired")
    write_json(
        repository / ".gitopsctr/cleanup/units/unavailable.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "OpaqueCleanupRoot",
            "metadata": {"name": "unavailable", "uid": "uid-unavailable"},
            "payload": {"unavailable": True},
        },
    )
    revision = commit(repository, "retained opaque cleanup root")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/dev")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        counts = inventory.reconciliation_counts("dev")

    assert counts[ReconciliationState.WAIT] == 2


def test_relationship_evaluation_distinguishes_stale_missing_and_orphan(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        units, receipts, artifacts = inventory.environment_inventory("dev")
        stale_document = json.loads(json.dumps(receipts[0].document))
        stale_document["spec"]["desired"]["unitBlob"] = "stale"  # type: ignore[index]
        stale_receipt = replace(receipts[0], document=stale_document)
        stale = evaluate_relationships(RESOURCE_REGISTRY, units, (stale_receipt,), artifacts)
        missing = evaluate_relationships(RESOURCE_REGISTRY, units, (), artifacts)
        orphan = evaluate_relationships(RESOURCE_REGISTRY, (), receipts, artifacts)

    stale_application = next(item for item in stale.units if item.unit.name == "application")
    missing_application = next(item for item in missing.units if item.unit.name == "application")
    assert stale_application.observation is InventoryObservationState.STALE
    assert stale_application.reconciliation is ReconciliationState.READY
    assert missing_application.observation is InventoryObservationState.MISSING
    assert missing_application.reason == "no observation receipt"
    assert orphan.receipts[0].observation is InventoryObservationState.ORPHAN


def test_relationship_evaluation_selects_applicable_registered_definition(repository: Path):
    base = RESOURCE_REGISTRY.observations[0]
    additional = ObservationDefinition(
        "additional-receipt-observation",
        observer_family=base.observer_family,
        observer_plane=base.observer_plane,
        subject_family=base.subject_family,
        subject_plane=base.subject_plane,
        cardinality=base.cardinality,
        binding=base.binding,
    )
    registry = ResourceRegistry(
        RESOURCE_REGISTRY.api_kinds,
        RESOURCE_REGISTRY.collections,
        RESOURCE_REGISTRY.families,
        (*RESOURCE_REGISTRY.observations, additional),
        RESOURCE_REGISTRY.artifact_descriptions,
    )
    with InventorySession(repository, registry) as inventory:
        units, receipts, artifacts = inventory.environment_inventory("dev")
        evaluation = evaluate_relationships(registry, units, receipts, artifacts)
    assert (
        next(item for item in evaluation.units if item.unit.name == "application").reconciliation
        is ReconciliationState.CLEAN
    )


def test_relationship_evaluation_rejects_duplicate_and_malformed_receipts(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        units, receipts, artifacts = inventory.environment_inventory("dev")
        duplicate = replace(receipts[0], path=receipts[0].path.with_name("duplicate.yaml"))
        with pytest.raises(InventoryError, match="both observe"):
            evaluate_relationships(RESOURCE_REGISTRY, units, (receipts[0], duplicate), artifacts)

        invalid_document = json.loads(json.dumps(receipts[0].document))
        invalid_document["spec"]["subject"]["name"] = "different"  # type: ignore[index]
        with pytest.raises(InventoryError, match="subject name must match"):
            evaluate_relationships(
                RESOURCE_REGISTRY, units, (replace(receipts[0], document=invalid_document),), artifacts
            )


def test_inventory_validates_complete_receipt_artifact_relationship(repository: Path):
    git(repository, "checkout", "desired")
    write_json(
        repository / "units/images.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "metadata": {
                "name": "images",
                "uid": "uid-images",
                "labels": {"gitopsctr.io/partition": "application"},
            },
            "spec": {
                "source": {
                    "path": ".",
                    "revision": "a" * 40,
                    "driverVersion": 1,
                    "inputHash": "sha256:inputs",
                }
            },
        },
    )
    desired_revision = commit(repository, "desired artifact producer")
    git(repository, "push", "origin", f"{desired_revision}:refs/heads/gitopsctr/desired/artifacts")
    unit_blob = git(repository, "rev-parse", f"{desired_revision}:units/images.yaml")

    git(repository, "checkout", "observed")
    artifact_path = repository / "artifacts/images/containers.yaml"
    write_json(
        artifact_path,
        {
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
                "inputHash": "sha256:inputs",
            },
            "images": {},
        },
    )
    digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    image_receipt = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Receipt",
        "metadata": {"name": "images"},
        "spec": {
            "subject": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "OciImages", "name": "images"},
            "desired": {"unitBlob": unit_blob},
        },
        "status": {
            "controller": {},
            "result": {},
            "artifacts": {
                "containers": {
                    "apiVersion": "artifact.gitopsctr.io/v1",
                    "kind": "ContainerImages",
                    "path": "artifacts/images/containers.yaml",
                    "digest": digest,
                    "mediaType": "application/vnd.gitopsctr.container-images.v1+yaml",
                }
            },
        },
    }
    write_json(repository / "units/images.yaml", image_receipt)
    observed_revision = commit(repository, "observed artifact")
    git(repository, "push", "origin", f"{observed_revision}:refs/heads/gitopsctr/observed/artifacts")
    git(repository, "checkout", "main")

    inventory = InventorySession(repository, RESOURCE_REGISTRY)
    units, receipts, artifacts = inventory.environment_inventory(
        "dev", desired_ref="gitopsctr/desired/artifacts", observed_ref="gitopsctr/observed/artifacts"
    )
    evaluation = evaluate_relationships(RESOURCE_REGISTRY, units, receipts, artifacts)
    images = next(item for item in evaluation.units if item.unit.name == "images")
    assert images.observation is InventoryObservationState.CURRENT
    assert images.artifacts[0].name == "containers"

    image_artifact = next(item for item in artifacts if item.name == "containers")
    mismatched_pin_document = json.loads(json.dumps(image_artifact.document))
    mismatched_pin_document["producer"]["sourceRevision"] = "b" * 40
    mismatched_pin_artifacts = tuple(
        replace(item, document=mismatched_pin_document) if item.name == "containers" else item for item in artifacts
    )
    with pytest.raises(InventoryError, match="stale producer source pin"):
        evaluate_relationships(RESOURCE_REGISTRY, units, receipts, mismatched_pin_artifacts)

    advanced_document = json.loads(json.dumps(images.unit.document))
    advanced_document["spec"]["source"]["revision"] = "b" * 40
    advanced_document["spec"]["source"]["inputHash"] = "sha256:advanced-inputs"
    advanced_unit = replace(
        images.unit,
        document=advanced_document,
        parsed=RESOURCE_REGISTRY.contract(images.unit.gvk, "desired").parse(advanced_document),
        blob_id="advanced-unit-blob",
    )
    advanced_units = tuple(advanced_unit if item.name == "images" else item for item in units)
    stale = evaluate_relationships(RESOURCE_REGISTRY, advanced_units, receipts, artifacts)
    stale_images = next(item for item in stale.units if item.unit.name == "images")
    assert stale_images.observation is InventoryObservationState.STALE
    assert stale_images.artifacts == ()
    stale_receipt = next(item for item in stale.receipts if item.receipt.name == "images")
    assert stale_receipt.artifact_count == 1

    invalid_document = json.loads(json.dumps(next(item for item in receipts if item.name == "images").document))
    invalid_document["status"]["artifacts"]["containers"]["digest"] = "sha256:wrong"  # type: ignore[index]
    invalid_receipts = tuple(
        replace(item, document=invalid_document) if item.name == "images" else item for item in receipts
    )
    with pytest.raises(InventoryError, match="wrong digest"):
        evaluate_relationships(RESOURCE_REGISTRY, units, invalid_receipts, artifacts)
    inventory.close()


def test_missing_observed_ref_is_an_empty_inventory_but_named_ref_errors(repository: Path):
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        empty = inventory.resources(
            "receipts",
            environment="dev",
            ref="gitopsctr/observed/missing",
            allow_missing_ref=True,
        )
        assert empty == ()
        with pytest.raises(InventoryError, match="does not exist"):
            inventory.resources("receipts", environment="dev", ref="gitopsctr/observed/missing")


def test_inventory_architecture_does_not_import_controller():
    root = Path(__file__).parents[1] / "src/gitopsctr"
    for module in ("inventory.py", "plane_repositories.py", "resource_model.py"):
        assert "gitopsctr.controller" not in (root / module).read_text()


def test_invalid_resource_error_contains_environment_plane_and_path(repository: Path):
    git(repository, "checkout", "desired")
    write_json(
        repository / "units/invalid.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "invalid"},
            "spec": {},
        },
    )
    revision = commit(repository, "invalid desired")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/invalid")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError) as error:
            inventory.resources("units", environment="dev", ref="gitopsctr/desired/invalid")
    message = str(error.value)
    assert "environment 'dev'" in message
    assert "desired" in message
    assert "invalid.yaml" in message


def test_artifact_inventory_identity_is_qualified_by_producer(repository: Path):
    git(repository, "checkout", "observed")
    for producer in ("images-one", "images-two"):
        write_json(
            repository / f"artifacts/{producer}/containers.yaml",
            {
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "metadata": {"name": "containers"},
                "producer": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "OciImages",
                    "name": producer,
                    "driverVersion": 1,
                    "sourceRevision": "a" * 40,
                    "inputHashVersion": 1,
                    "inputHash": "sha256:inputs",
                },
                "images": {},
            },
        )
    revision = commit(repository, "two artifact producers")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/observed/artifact-identities")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        artifacts = inventory.resources(
            "artifact",
            environment="dev",
            plane=ResourcePlane.OBSERVED,
            ref="gitopsctr/observed/artifact-identities",
        )

    assert len(artifacts) == 2
    assert {(item.gvk, item.name) for item in artifacts} == {
        (artifacts[0].gvk, "containers"),
    }
    assert {item.identity_qualifier[-1] for item in artifacts} == {"images-one", "images-two"}
    assert len({item.logical_identity for item in artifacts}) == 2


@pytest.mark.parametrize(
    ("relative_path", "document", "message"),
    [
        (
            "deployment/environments/dev/units/not-shared.json",
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": {"name": "shared"},
                "spec": {"source": {"path": "."}},
            },
            "must match filename stem",
        ),
        (
            "deployment/environments/dev/units/broken.yaml",
            {
                "apiVersion": "v1",
                "kind": "Terraform",
                "metadata": {"name": "broken"},
                "spec": {"source": {"path": "."}},
            },
            "invalid API kind",
        ),
    ],
)
def test_source_collection_rejects_path_identity_and_malformed_gvk(
    repository: Path,
    relative_path: str,
    document: object,
    message: str,
):
    write_json(repository / relative_path, document)

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match=message) as error:
            inventory.resources("units", environment="dev", plane=ResourcePlane.SOURCE)

    rendered = str(error.value)
    assert "environment 'dev'" in rendered
    assert "source" in rendered
    assert Path(relative_path).name in rendered


def test_collection_rejects_duplicate_document_formats_for_one_logical_resource(repository: Path):
    source = repository / "deployment/environments/dev/units/shared.yaml"
    (source.with_suffix(".json")).write_text(source.read_text())

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="duplicate logical") as error:
            inventory.resources("units", environment="dev", plane=ResourcePlane.SOURCE)

    assert "shared.json" in str(error.value)
    assert "shared.yaml" in str(error.value)


def test_collection_rejects_same_stem_with_different_unit_gvks(repository: Path):
    write_json(
        repository / "deployment/environments/dev/units/shared.json",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "metadata": {"name": "shared"},
            "spec": {"source": {"path": "."}},
        },
    )

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="duplicate logical"):
            inventory.resources("units", environment="dev", plane=ResourcePlane.SOURCE)


def test_owned_unit_inherits_partition_from_uid_fenced_stack(repository: Path):
    git(repository, "checkout", "desired")
    unit = desired_terraform("web--application")
    unit["metadata"].pop("labels")  # type: ignore[union-attr]
    unit["metadata"]["ownerReferences"] = [  # type: ignore[index]
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "name": "web",
            "uid": "uid-web",
        }
    ]
    write_json(repository / "units/web--application.yaml", unit)
    revision = commit(repository, "owned desired Unit")
    git(repository, "push", "origin", f"{revision}:refs/heads/gitopsctr/desired/owned")
    git(repository, "checkout", "main")

    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        record = inventory.resources(
            "unit",
            environment="dev",
            ref="gitopsctr/desired/owned",
            names=frozenset(("web--application",)),
        )[0]
        assert inventory.resource_partition(record) == "application"


def test_environment_collection_requires_canonical_identity_and_document(repository: Path):
    environment_path = repository / "deployment/environments/dev/environment.yaml"
    write_json(environment_path, environment_document("other"))
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="must match directory"):
            inventory.resources("environments")

    environment_path.unlink()
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="has no environment") as error:
            inventory.resources("environments")
    assert "deployment/environments/dev" in str(error.value)


def test_collection_wraps_digest_read_failures_with_inventory_context(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target = repository / "deployment/environments/dev/units/shared.yaml"
    original = Path.read_bytes

    def fail_target(path: Path) -> bytes:
        if path == target:
            raise OSError("simulated read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_target)
    with InventorySession(repository, RESOURCE_REGISTRY) as inventory:
        with pytest.raises(InventoryError, match="simulated read failure") as error:
            inventory.resources("units", environment="dev", plane=ResourcePlane.SOURCE)
    rendered = str(error.value)
    assert "environment 'dev'" in rendered
    assert "source" in rendered
    assert "shared.yaml" in rendered
