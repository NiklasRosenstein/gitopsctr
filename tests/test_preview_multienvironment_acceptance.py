"""One temporary-repository acceptance story for source and direct Stack lifecycles."""

from __future__ import annotations

import hashlib
import json
import shutil
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredSource
from gitopsctr.state import GitStateStore
from tests.conftest import receipt_resource
from tests.stack_support import commit, git

DESIRED_DEV = "deploy/dev"
OBSERVED_DEV = "observed/dev"
DESIRED_STAGING = "deploy/staging"
OBSERVED_STAGING = "observed/staging"
DESIRED_PREVIEW = "deploy/preview"
OBSERVED_PREVIEW = "observed/preview"

DESIRED_REFS = (DESIRED_DEV, DESIRED_STAGING, DESIRED_PREVIEW)
OBSERVED_REFS = (OBSERVED_DEV, OBSERVED_STAGING, OBSERVED_PREVIEW)


class FakeInventory:
    """Deterministic observation ledger used instead of Docker or Terraform."""

    def __init__(self) -> None:
        self.receipts: dict[str, set[str]] = {}
        self.artifacts: dict[tuple[str, str], str] = {}

    def record(self, environment: str, unit: str, artifact_uri: str | None = None) -> None:
        self.receipts.setdefault(environment, set()).add(unit)
        if artifact_uri is not None:
            self.artifacts[(environment, unit)] = artifact_uri

    def assert_clean(self, environment: str, units: set[str]) -> None:
        assert self.receipts.get(environment) == units


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True))


def _write_project(source: Path) -> None:
    _write_json(
        source / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "multi-environment-acceptance"},
            "spec": {
                "environmentDefaults": {
                    "refs": {
                        "desired": "deploy/{environment}",
                        "observed": "observed/{environment}",
                    }
                }
            },
        },
    )
    _write_json(
        source / "deployment/stack-templates/application.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "StackTemplate",
            "metadata": {"name": "application"},
            "spec": {
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
                }
            },
        },
    )
    for environment, spec in (
        ("dev", {}),
        ("staging", {"promotion": {"allowedSources": ["dev"]}}),
        ("preview", {}),
    ):
        root = source / "deployment/environments" / environment
        _write_json(
            root / "environment.json",
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": environment},
                "spec": spec,
            },
        )
        (root / "stacks").mkdir(parents=True, exist_ok=True)
    (source / "application-version.txt").write_text("r1\n")


def _write_stack(source: Path, environment: str, *, units: list[str] | None = None, promoted: bool = False) -> None:
    spec: dict[str, Any] = {
        "template": (
            {"name": "application", "source": {"fromPromotion": {"stack": "application"}}}
            if promoted
            else "application"
        ),
    }
    if units is not None:
        spec["units"] = units
    if promoted:
        spec["artifactImports"] = [
            {
                "unit": "image",
                "name": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "fromPromotion": {"stack": "application"},
            }
        ]
    _write_json(
        source / "deployment/environments" / environment / "stacks/application.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": spec,
        },
    )


def _source_repository(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "init", "--bare", str(remote))
    source.mkdir()
    _write_project(source)
    _write_stack(source, "dev", units=["image", "deploy"])
    _write_stack(source, "staging", units=["deploy"], promoted=True)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    r1 = commit(source, "define dev and staging Stacks")
    git(source, "push", "-u", "origin", "main")
    return source, r1, str(remote)


def _store(source: Path) -> GitStateStore:
    controller._state_store.cache_clear()
    controller.REPOSITORY_ROOT = source
    return GitStateStore(source)


def _materialize(store: GitStateStore, ref: str, output: Path) -> str:
    revision = store.fetch(ref).revision
    assert revision is not None, f"expected local test ref {ref!r}"
    store.materialize(revision, output)
    return revision


def _desired_unit(root: Path, name: str):
    return controller.load_desired_unit(controller.unit_document_path(root, name), name)


def _stack(root: Path, name: str = "application"):
    path = controller.document_candidates(root / "stacks", name)[0]
    return controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name=name
    )


def _projected_units(source: Path, environment: str, revision: str, tmp_path: Path):
    projection = controller.project_stack_resources(source, environment, revision, tmp_path, source)
    assert sorted(projection.generated_units) == ["application--deploy", "application--image"]
    assert projection.dependencies["application--deploy"] == ()
    assert projection.owners["application--deploy"].name == "application"
    return projection


def _seed_projected_image(source: Path, projection: Any, revision: str, desired: Path) -> None:
    """Seed the producer Unit so its deterministic fake artifact can be observed."""

    image = projection.generated_units["application--image"]
    driver = controller.UNIT_DRIVERS["oci-images"]
    source_identity = DesiredSource(
        path=".",
        revision=revision,
        inputHash=controller.unit_input_hash(image, source),
        driverVersion=driver.version,
    )
    resolved = driver.resolve_unit(
        image.spec,
        controller.UnitResolutionContext(
            source=source_identity,
            resolve_template=lambda _value, _pointer: pytest.fail("image template unexpectedly resolved an input"),
        ),
    ).unit
    controller.write_desired_candidate_unit(
        desired / "units/application--image.json",
        image.with_spec(resolved).with_metadata(
            controller._stack_owned_metadata("application--image", projection.owners["application--image"])
        ),
        source,
    )


def _advance(source: Path, environment: str, source_revision: str) -> str:
    revision, changed = controller.advance_desired(
        environment,
        source_revision,
        desired_ref=f"deploy/{environment}",
        observed_ref=f"observed/{environment}",
        summarize=False,
        verbose=False,
    )
    assert revision is not None
    assert changed
    return revision


def _artifact_document(unit: Any, uri: str) -> dict[str, Any]:
    source = unit.spec.source
    return {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": unit.name,
            "driverVersion": 1,
            "sourceRevision": source.revision,
            "inputHashVersion": 1,
            "inputHash": source.inputHash,
        },
        "images": {"application": {"uri": uri}},
    }


def _publish_observations(
    store: GitStateStore,
    environment: str,
    desired: Path,
    units: list[str],
    inventory: FakeInventory,
    *,
    image_uri: str,
) -> str:
    observed_revision = store.fetch(f"observed/{environment}").revision
    observed = desired.parent / f"observed-{environment}-{observed_revision or 'initial'}"
    observed.mkdir()
    image_name = "application--image"
    for name in units:
        unit = _desired_unit(desired, name)
        if name == image_name:
            descriptors = controller.write_artifact_documents(
                observed,
                name,
                "oci-images",
                {"containers": _artifact_document(unit, image_uri)},
            )
            receipt = receipt_resource(
                "oci-images",
                name,
                {"unitBlob": controller.file_blob(controller.unit_document_path(desired, name))},
                artifacts=descriptors,
            )
            inventory.record(environment, name, image_uri)
        else:
            receipt = receipt_resource(
                "terraform",
                name,
                {"unitBlob": controller.file_blob(controller.unit_document_path(desired, name))},
            )
            inventory.record(environment, name)
        controller.write_document(
            observed / "units" / f"{name}.json",
            controller.RESOURCE_CATALOG.serialize_receipt(receipt),
            format=controller.DocumentFormat.JSON,
        )
    return store.publish(
        f"observed/{environment}",
        observed,
        store.fetch(f"observed/{environment}").revision,
        f"observe deterministic {environment}",
    ).revision


def _stack_lineage(stack: Any) -> tuple[str, str, str]:
    assert isinstance(stack.spec, controller.DesiredStackSpec)
    assert stack.spec.resolvedSource is not None
    source = stack.spec.resolvedSource.fromGit
    assert source is not None
    return source.commit, source.resourcePath, source.digest


def _fake_preview_image_observation(
    source: Path,
    source_revision: str,
    tmp_path: Path,
    store: GitStateStore,
    uri: str,
) -> None:
    source_snapshot = tmp_path / f"source-snapshot-{source_revision[:8]}"
    store.materialize(source_revision, source_snapshot)
    probe_source = tmp_path / f"preview-probe-{source_revision[:8]}"
    shutil.copytree(source_snapshot, probe_source)
    _write_stack(probe_source, "preview", units=["image", "deploy"])
    probe = tmp_path / f"preview-probe-desired-{source_revision[:8]}"
    projection = _projected_units(probe_source, "preview", source_revision, probe)
    _seed_projected_image(probe_source, projection, source_revision, probe)
    inventory = FakeInventory()
    _publish_observations(store, "preview", probe, ["application--image"], inventory, image_uri=uri)


def _command_args(**values: Any) -> Namespace:
    arguments = {
        "environment": "preview",
        "stack": "application",
        "template": "application",
        "source_revision": None,
        "parameters": "{}",
        "request_id": "github:example/application#123",
        "desired_ref": DESIRED_PREVIEW,
        "observed_ref": OBSERVED_PREVIEW,
        "candidate_ref": None,
        "uid": None,
        "deletion_generation": None,
        "dry": False,
    }
    arguments.update(values)
    return Namespace(**arguments)


def _print_ref_histories(store: GitStateStore) -> dict[str, int]:
    """Print the final desired and observed ref histories and return commit counts."""

    counts = {"desired": 0, "observed": 0}
    print("\nAcceptance ref history:")
    for category, refs in (("desired", DESIRED_REFS), ("observed", OBSERVED_REFS)):
        for ref in refs:
            snapshot = store.fetch(ref)
            assert snapshot.revision is not None, f"expected final ref {ref!r}"
            history = store.git(
                "log",
                "--oneline",
                "--reverse",
                f"refs/remotes/origin/{ref}",
            ).stdout.splitlines()
            counts[category] += len(history)
            print(f"{ref} ({len(history)} advancements):")
            for line in history:
                print(f"  {line}")
    print(f"Totals: desired={counts['desired']}, observed={counts['observed']}, total={sum(counts.values())}")
    return counts


def test_multi_environment_stack_story_in_temporary_repository(tmp_path: Path) -> None:
    source, r1, _remote = _source_repository(tmp_path)
    store = _store(source)
    inventory = FakeInventory()

    # R1: source projection and dev become fully materialized after its fake image observation.
    dev_projection_root = tmp_path / "dev-projection-r1"
    source_r1 = tmp_path / "source-r1"
    store.materialize(r1, source_r1)
    dev_projection = _projected_units(source_r1, "dev", r1, dev_projection_root)
    _seed_projected_image(source_r1, dev_projection, r1, dev_projection_root)
    _publish_observations(
        store,
        "dev",
        dev_projection_root,
        ["application--image"],
        inventory,
        image_uri="registry.invalid/application:dev-r1@sha256:" + "1" * 64,
    )
    dev_desired_r1_revision = _advance(source, "dev", r1)
    dev_r1 = tmp_path / "dev-r1-final"
    _materialize(store, DESIRED_DEV, dev_r1)
    _publish_observations(
        store,
        "dev",
        dev_r1,
        ["application--image", "application--deploy"],
        inventory,
        image_uri="registry.invalid/application:dev-r1@sha256:" + "1" * 64,
    )
    inventory.assert_clean("dev", {"application--image", "application--deploy"})
    dev_stack = _stack(dev_r1)
    assert controller.desired_unit_names(dev_r1) == ("application--deploy", "application--image")
    assert _stack_lineage(dev_stack) == (
        r1,
        "deployment/stack-templates/application.json",
        hashlib.sha256((source / "deployment/stack-templates/application.json").read_bytes()).hexdigest(),
    )
    assert isinstance(dev_stack.spec, controller.DesiredStackSpec)
    assert set(dev_stack.spec.resolvedProjection["units"]) == {"image", "deploy"}
    assert {
        _desired_unit(dev_r1, name).metadata.lifecycle.owner.uid
        for name in ("application--image", "application--deploy")
    } == {dev_stack.metadata.uid}
    assert _desired_unit(dev_r1, "application--image").spec.source.revision == r1
    assert _desired_unit(dev_r1, "application--deploy").spec.source.revision == r1

    # Promotion carries only deploy and records exact dev desired/observed lineage.
    controller.command_promote(
        Namespace(
            from_environment="dev",
            to_environment="staging",
            source_desired_revision=dev_desired_r1_revision,
            specification_revision=r1,
            candidate_ref=None,
        )
    )
    staging_r1 = tmp_path / "staging-r1"
    staging_desired_r1_revision = _materialize(store, DESIRED_STAGING, staging_r1)
    assert controller.desired_unit_names(staging_r1) == ("application--deploy",)
    staging_stack = _stack(staging_r1)
    assert isinstance(staging_stack.spec, controller.DesiredStackSpec)
    assert staging_stack.spec.resolvedSource == dev_stack.spec.resolvedSource
    evidence = staging_stack.spec.resolvedArtifactImports["image/containers"]
    assert evidence.sourceStack == "application"
    assert evidence.sourceUnit == "application--image"
    assert evidence.sourceDesiredRevision == dev_desired_r1_revision
    assert evidence.sourceObservedRevision == store.fetch(OBSERVED_DEV).revision
    assert evidence.targetStackUid == staging_stack.metadata.uid
    assert _desired_unit(staging_r1, "application--deploy").spec.terraform.variables["image"].endswith("1" * 64)
    _publish_observations(
        store,
        "staging",
        staging_r1,
        ["application--deploy"],
        inventory,
        image_uri="registry.invalid/application:dev-r1@sha256:" + "1" * 64,
    )
    inventory.assert_clean("staging", {"application--deploy"})

    # Preview starts template-only, then is directly instantiated at R1 with its own artifact lineage.
    preview_initial = tmp_path / "preview-initial"
    controller.project_stack_resources(source_r1, "preview", r1, preview_initial, source_r1)
    preview_initial_revision = store.publish(DESIRED_PREVIEW, preview_initial, None, "initialize preview").revision
    assert preview_initial_revision
    assert controller.command_instantiate_stack(
        _command_args(source_revision=r1, template="application", stack="application")
    )
    preview_r1 = tmp_path / "preview-r1"
    preview_r1_initial_revision = _materialize(store, DESIRED_PREVIEW, preview_r1)
    preview_stack = _stack(preview_r1)
    assert preview_stack.metadata.lifecycle.management.mode == "direct"
    assert preview_stack.metadata.uid is not None
    assert isinstance(preview_stack.spec, controller.DesiredStackSpec)
    assert preview_stack.spec.provenance.templateRevision == r1
    assert preview_stack.spec.provenance.requestIdentity == "github:example/application#123"
    assert controller.desired_unit_names(preview_r1) == ("application--image",)
    _publish_observations(
        store,
        "preview",
        preview_r1,
        ["application--image"],
        inventory,
        image_uri="registry.invalid/application:preview-r1@sha256:" + "2" * 64,
    )
    assert controller.command_update_direct_stack(
        _command_args(
            source_revision=r1,
            uid=preview_stack.metadata.uid,
            desired_revision=preview_r1_initial_revision,
            request_id="github:example/application#123/resolve-r1",
        )
    )
    preview_r1_resolved = tmp_path / "preview-r1-resolved"
    preview_r1_revision = _materialize(store, DESIRED_PREVIEW, preview_r1_resolved)
    preview_stack = _stack(preview_r1_resolved)
    assert controller.desired_unit_names(preview_r1_resolved) == ("application--deploy", "application--image")
    assert {
        _desired_unit(preview_r1_resolved, name).metadata.lifecycle.owner.uid
        for name in ("application--image", "application--deploy")
    } == {preview_stack.metadata.uid}
    _publish_observations(
        store,
        "preview",
        preview_r1_resolved,
        ["application--image", "application--deploy"],
        inventory,
        image_uri="registry.invalid/application:preview-r1@sha256:" + "2" * 64,
    )
    preview_observed_r1_revision = store.fetch(OBSERVED_PREVIEW).revision
    assert preview_observed_r1_revision is not None
    inventory.assert_clean("preview", {"application--image", "application--deploy"})
    preview_unit_uids_r1 = {
        name: _desired_unit(preview_r1_resolved, name).metadata.uid
        for name in ("application--image", "application--deploy")
    }
    with pytest.raises(controller.OperationError, match="different instantiation request"):
        controller.command_instantiate_stack(
            _command_args(source_revision=r1, template="application", stack="application")
        )

    # R2 advances only dev. Staging and direct preview retain their desired and observed snapshots.
    staging_files_r1 = controller.directory_files(staging_r1)
    preview_files_r1 = controller.directory_files(preview_r1_resolved)
    preview_image_r1 = inventory.artifacts[("preview", "application--image")]
    (source / "application-version.txt").write_text("r2\n")
    r2 = commit(source, "change application source")
    source_r2 = tmp_path / "source-r2"
    store.materialize(r2, source_r2)
    _projected_units(source_r2, "dev", r2, tmp_path / "dev-projection-r2")
    dev_r2_projection = controller.project_stack_resources(
        source_r2,
        "dev",
        r2,
        tmp_path / "dev-projection-r2-current",
        source_r2,
        dev_r1,
    )
    assert dev_r2_projection.owners["application--deploy"].uid == _stack(dev_r1).metadata.uid
    _advance(source, "dev", r2)
    dev_r2_probe = tmp_path / "dev-r2-probe"
    _materialize(store, DESIRED_DEV, dev_r2_probe)
    assert _desired_unit(dev_r2_probe, "application--image").spec.source.revision == r2
    _publish_observations(
        store,
        "dev",
        dev_r2_probe,
        ["application--image"],
        inventory,
        image_uri="registry.invalid/application:dev-r2@sha256:" + "3" * 64,
    )
    dev_desired_r2_revision = _advance(source, "dev", r2)
    dev_r2 = tmp_path / "dev-r2"
    _materialize(store, DESIRED_DEV, dev_r2)
    assert _desired_unit(dev_r2, "application--image").spec.source.revision == r2
    assert (
        _desired_unit(dev_r2, "application--image").spec.source.inputHash
        != _desired_unit(dev_r1, "application--image").spec.source.inputHash
    )
    _publish_observations(
        store,
        "dev",
        dev_r2,
        ["application--image", "application--deploy"],
        inventory,
        image_uri="registry.invalid/application:dev-r2@sha256:" + "3" * 64,
    )
    assert controller.directory_files(staging_r1) == staging_files_r1
    assert controller.directory_files(preview_r1_resolved) == preview_files_r1
    assert inventory.artifacts[("preview", "application--image")] == preview_image_r1
    assert store.fetch(DESIRED_STAGING).revision == staging_desired_r1_revision
    assert store.fetch(DESIRED_PREVIEW).revision == preview_r1_revision
    assert store.fetch(OBSERVED_PREVIEW).revision == preview_observed_r1_revision

    # This is the agreed next API boundary. Keep the assertion strict and diagnostic until it lands.
    update = getattr(controller, "command_update_direct_stack", None)
    if not callable(update):
        pytest.fail(
            "multi-environment Stack acceptance is blocked: controller.command_update_direct_stack "
            "(the update-direct-stack API) is not implemented"
        )

    update_args = _command_args(
        source_revision=r2,
        uid=preview_stack.metadata.uid,
        desired_revision=preview_r1_revision,
        desired_ref=DESIRED_PREVIEW,
        observed_ref=OBSERVED_PREVIEW,
    )
    assert update(update_args) is True
    preview_r2 = tmp_path / "preview-r2"
    _materialize(store, DESIRED_PREVIEW, preview_r2)
    updated_preview_stack = _stack(preview_r2)
    assert updated_preview_stack.metadata.uid == preview_stack.metadata.uid
    assert updated_preview_stack.metadata.lifecycle.management.mode == "direct"
    assert _desired_unit(preview_r2, "application--image").spec.source.revision == r2
    assert {
        name: _desired_unit(preview_r2, name).metadata.uid for name in ("application--image", "application--deploy")
    } == preview_unit_uids_r1
    assert store.fetch(DESIRED_DEV).revision == dev_desired_r2_revision
    assert store.fetch(DESIRED_STAGING).revision == staging_desired_r1_revision
    assert controller.directory_files(preview_r2) != preview_files_r1
    _publish_observations(
        store,
        "preview",
        preview_r2,
        ["application--image", "application--deploy"],
        inventory,
        image_uri="registry.invalid/application:preview-r2@sha256:" + "4" * 64,
    )
    assert inventory.artifacts[("preview", "application--image")].endswith("4" * 64)
    assert update(update_args) is False

    # Deletion request retains the direct root and UID-fenced owned closure for cleanup.
    assert controller.command_request_delete_direct_stack(
        _command_args(uid=updated_preview_stack.metadata.uid, source_revision=None)
    )
    deleting = tmp_path / "preview-deleting"
    deleting_revision = _materialize(store, DESIRED_PREVIEW, deleting)
    intent = controller.load_desired_stack_deletion_intents(deleting)["application"]
    assert intent.uid == updated_preview_stack.metadata.uid
    assert intent.deletion_generation == 1
    assert {item.unit_name for item in intent.owned_unit_closure} == {
        "application--image",
        "application--deploy",
    }
    assert all(item.uid for item in intent.owned_unit_closure)
    assert _stack(deleting).metadata.uid == updated_preview_stack.metadata.uid
    assert {
        _desired_unit(deleting, name).metadata.lifecycle.owner.uid
        for name in ("application--image", "application--deploy")
    } == {updated_preview_stack.metadata.uid}
    assert controller.load_desired_transition_blocks(deleting)["application"]
    retained = tmp_path / "preview-retained"
    controller.build_desired_candidate(
        "preview",
        source,
        r2,
        deleting,
        tmp_path / "preview-observed-current",
        None,
        retained,
        verbose=False,
    )
    assert controller.load_desired_transition_blocks(retained)["application"]
    assert controller.load_desired_stack_deletion_intents(retained)["application"] == intent
    assert _stack(retained).metadata.uid == updated_preview_stack.metadata.uid
    assert controller.desired_unit_names(retained) == ("application--deploy", "application--image")
    with pytest.raises(controller.OperationError, match="active owned Units"):
        controller.command_finalize_stack(_command_args(uid=updated_preview_stack.metadata.uid, deletion_generation=1))
    assert store.fetch(DESIRED_PREVIEW).revision == deleting_revision
    assert _print_ref_histories(store) == {"desired": 9, "observed": 8}
