from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from gitopsctr import controller
from gitopsctr.contracts import (
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    StackProjection,
    StackProjectionUnit,
    StackTemplateSpec,
    StackTemplateUnitTemplate,
)
from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore
from gitopsctr.templates import TemplateObject
from tests.conftest import receipt_resource
from tests.stack_support import commit, project_repository
from tests.test_apply import _apply, _apply_worktree, _authored_source_less_unit, _authored_unit, _repository


def write_inline_template(
    path: Path,
    *,
    description: str | None = None,
    dynamic: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    unit_spec: dict[str, Any] = {"source": {"path": "."}}
    if description is not None:
        unit_spec["terraform"] = {"variables": {"description": description}}
    if dynamic:
        unit_spec["terraform"] = {
            "variables": {
                "image": {
                    "fromArtifact": {
                        "unit": "image",
                        "name": "containers",
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                    }
                }
            }
        }
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": unit_spec,
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    return path


def write_inline_stack(path: Path, *, template: str = "preview", name: str = "web") -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": name},
                "spec": {"template": template},
            },
            sort_keys=False,
        )
    )
    return path


def write_atomic_template(path: Path, *, version: str, blocked_b: bool) -> Path:
    b_spec: dict[str, Any] = {"source": {"path": "."}}
    if blocked_b:
        b_spec["terraform"] = {
            "variables": {
                "producer": {
                    "fromReceipt": {
                        "unit": "producer",
                        "pointer": "/outputs/value",
                    }
                }
            }
        }
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "a": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {"variables": {"version": version}},
                            },
                        },
                        "b": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": b_spec,
                        },
                    },
                },
            },
            sort_keys=False,
        )
    )
    return path


def write_receipt_template(path: Path, *, template_name: str, producer: str) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": template_name},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {
                                    "variables": {
                                        "value": {
                                            "fromReceipt": {
                                                "unit": producer,
                                                "pointer": "/outputs/value",
                                            }
                                        }
                                    }
                                },
                            },
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    return path


def write_topology_template(path: Path, *, version: str, blocked_b: bool, include_c: bool) -> Path:
    write_atomic_template(path, version=version, blocked_b=blocked_b)
    document = yaml.safe_load(path.read_text())
    document["spec"]["unitTemplates"]["b"]["dependsOn"] = ["a"] if version == "v2" else []
    if include_c:
        document["spec"]["unitTemplates"]["c"] = {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "spec": {"source": {"path": "."}, "terraform": {"variables": {"version": version}}},
            "dependsOn": ["a"],
        }
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def materialize(store: GitStateStore, revision: str, destination: Path) -> Path:
    store.materialize(revision, destination)
    return destination


def desired_stack_resources(store: GitStateStore, revision: str, destination: Path):
    root = materialize(store, revision, destination)
    return root, controller.load_desired_resource_graph(root)


def _two_context_blocked_stacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    web_template = write_receipt_template(
        source / "web-template.yaml", template_name="web-template", producer="web-producer"
    )
    worker_template = write_receipt_template(
        source / "worker-template.yaml", template_name="worker-template", producer="worker-producer"
    )
    web_stack = write_inline_stack(source / "web.yaml", template="web-template", name="web")
    web_producer = _authored_unit(source / "web-producer.yaml", "web-producer")
    first_revision = commit(source, "publish first blocked Stack context")
    first_published = _apply(source, first_revision, web_template, web_stack, web_producer)
    assert first_published is not None

    environment = source / "deployment/environments/dev/environment.json"
    environment_document = json.loads(environment.read_text())
    environment_document["spec"] = {"refs": {"desired": "context-two", "observed": "observed/dev"}}
    environment.write_text(json.dumps(environment_document))
    worker_stack = write_inline_stack(source / "worker.yaml", template="worker-template", name="worker")
    worker_producer = _authored_unit(source / "worker-producer.yaml", "worker-producer")
    second_revision = commit(source, "publish second blocked Stack context")
    published = _apply(source, second_revision, worker_template, worker_stack, worker_producer)
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "two-context-blocked")
    return source, store, published, root, resources


def test_projection_context_rejects_traversal_and_document_byte_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, _store, _revision = _repository(tmp_path, monkeypatch)
    context = controller.capture_projection_context(source, "dev")
    desired = tmp_path / "context-records"
    controller.write_projection_context(desired, context)

    traversal = dict(context)
    traversal["projectFile"] = "../escape.yaml"
    traversal["digest"] = controller._projection_context_digest(traversal)
    traversal_path = controller._projection_context_path(desired, traversal["digest"])
    traversal_path.parent.mkdir(parents=True, exist_ok=True)
    traversal_path.write_text(json.dumps(traversal))
    with pytest.raises(OperationError, match="safe basename"):
        controller.load_projection_context(desired, traversal["digest"], "dev")

    mismatch = dict(context)
    mismatch["projectDocument"] = dict(context["environmentDocument"])
    mismatch["digest"] = controller._projection_context_digest(mismatch)
    mismatch_path = controller._projection_context_path(desired, mismatch["digest"])
    mismatch_path.write_text(json.dumps(mismatch))
    with pytest.raises(OperationError, match="bytes do not match"):
        controller.load_projection_context(desired, mismatch["digest"], "dev")

    byte_mismatch = dict(context)
    byte_mismatch["projectBytes"] = base64.b64encode(b"not the project document").decode("ascii")
    byte_mismatch["digest"] = controller._projection_context_digest(byte_mismatch)
    byte_mismatch_path = controller._projection_context_path(desired, byte_mismatch["digest"])
    byte_mismatch_path.write_text(json.dumps(byte_mismatch))
    with pytest.raises(OperationError, match="document bytes are invalid"):
        controller.load_projection_context(desired, byte_mismatch["digest"], "dev")


def test_first_projection_omits_transitively_blocked_dependency_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_topology_template(source / "template.yaml", version="v2", blocked_b=True, include_c=True)
    document = yaml.safe_load(template.read_text())
    document["spec"]["unitTemplates"]["c"]["dependsOn"] = ["b"]
    template.write_text(yaml.safe_dump(document, sort_keys=False))
    stack = write_inline_stack(source / "stack.yaml")
    first_revision = commit(source, "publish initially blocked topology")

    published = _apply(source, first_revision, template, stack)

    assert published is not None
    _root, resources = desired_stack_resources(store, published, tmp_path / "blocked-first")
    stack_resource = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(stack_resource.spec, DesiredStackSpec)
    assert stack_resource.spec.activeProjection is not None
    assert set(stack_resource.spec.activeProjection.units) == {"a"}
    assert not controller.unit_document_path(_root, "web--b").exists()
    assert not controller.unit_document_path(_root, "web--c").exists()


def test_apply_template_and_stack_together_publishes_one_fenced_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "add inline StackTemplate and Stack")

    published = _apply(source, revision, template, stack, partition="application")
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "desired")

    desired_template = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    desired_stack = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(desired_template.spec, DesiredStackTemplateSpec)
    assert isinstance(desired_stack.spec, DesiredStackSpec)
    assert desired_template.spec.unitTemplates["app"].spec["source"] == {"path": "."}
    assert desired_template.spec.acquisition.fromInput.__class__.__name__ == "StackTemplateFromInput"
    assert desired_template.spec.acquisition.documentDigest == (
        "sha256:" + hashlib.sha256(template.read_bytes()).hexdigest()
    )
    assert desired_template.metadata.partition == "application"
    assert desired_stack.spec.templateRef.uid == desired_template.metadata.uid
    assert desired_stack.spec.templateRef.contentDigest == desired_template.spec.contentDigest
    assert desired_stack.spec.structuralProjection.identity.stackUid == desired_stack.metadata.uid
    assert desired_stack.spec.structuralProjection.identity.templateUid == desired_template.metadata.uid

    generated = controller.load_desired_unit(controller.unit_document_path(root, "web--app"), "web--app")
    owner = controller.resource_owner_reference(generated)
    assert owner is not None
    assert owner.uid == desired_stack.metadata.uid
    assert generated.metadata.uid is not None


def test_updating_template_preserves_template_uid_and_reprojects_all_referrers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    first_source_revision = commit(source, "publish initial inline stack")
    first_published = _apply(source, first_source_revision, template, stack)
    assert first_published is not None
    first_root, first_resources = desired_stack_resources(store, first_published, tmp_path / "first")
    first_template = first_resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    first_stack = first_resources[("gitopsctr.io/v1", "Stack", "web")]
    first_unit = controller.load_desired_unit(controller.unit_document_path(first_root, "web--app"), "web--app")

    write_inline_template(template, description="v2")
    second_source_revision = commit(source, "change inline StackTemplate")
    second_published = _apply(source, second_source_revision, template)
    assert second_published is not None
    second_root, second_resources = desired_stack_resources(store, second_published, tmp_path / "second")
    second_template = second_resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    second_stack = second_resources[("gitopsctr.io/v1", "Stack", "web")]
    second_unit = controller.load_desired_unit(controller.unit_document_path(second_root, "web--app"), "web--app")

    assert second_template.metadata.uid == first_template.metadata.uid
    assert second_template.spec.contentDigest != first_template.spec.contentDigest
    assert second_stack.metadata.uid == first_stack.metadata.uid
    assert second_stack.spec.templateRef.contentDigest == second_template.spec.contentDigest
    assert second_stack.spec.structuralProjection.identity.templateContentDigest == second_template.spec.contentDigest
    assert second_unit.spec != first_unit.spec
    assert controller.resource_owner_reference(second_unit).uid == first_stack.metadata.uid  # type: ignore[union-attr]


def test_template_update_fans_out_to_two_stacks_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    web = write_inline_stack(source / "web.yaml", name="web")
    worker = write_inline_stack(source / "worker.yaml", name="worker")
    first_source_revision = commit(source, "publish two inline Stacks")
    first_published = _apply(source, first_source_revision, template, web, worker)
    assert first_published is not None

    write_inline_template(template, description="v2")
    second_source_revision = commit(source, "fan out template update")
    second_published = _apply(source, second_source_revision, template)
    assert second_published is not None
    root, resources = desired_stack_resources(store, second_published, tmp_path / "fanout")
    for name in ("web", "worker"):
        stack = resources[("gitopsctr.io/v1", "Stack", name)]
        assert isinstance(stack.spec, DesiredStackSpec)
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}--app"), f"{name}--app")
        assert unit.spec.terraform.variables == {"description": "v2"}  # type: ignore[union-attr]
        assert stack.spec.activeProjection is not None
        assert set(stack.spec.activeProjection.units) == {"app"}

    write_inline_stack(worker, template="missing", name="worker")
    invalid_revision = commit(source, "break second referrer")
    before = store.fetch("deploy/dev").revision
    with pytest.raises(OperationError, match="missing desired StackTemplate 'missing'"):
        _apply(source, invalid_revision, template, worker)
    assert store.fetch("deploy/dev").revision == before


def test_stack_projection_wait_retains_prior_active_set_then_durable_evidence_switches_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_atomic_template(source / "template.yaml", version="v1", blocked_b=False)
    stack = write_inline_stack(source / "stack.yaml")
    producer = _authored_unit(source / "producer.yaml", "producer")
    first_revision = commit(source, "publish atomic Stack projection")
    first_published = _apply(source, first_revision, template, stack, producer)
    assert first_published is not None
    first_root, first_resources = desired_stack_resources(store, first_published, tmp_path / "atomic-first")
    first_stack = first_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(first_stack.spec, DesiredStackSpec)
    assert first_stack.spec.activeProjection is not None
    assert set(first_stack.spec.activeProjection.units) == {"a", "b"}
    first_a_bytes = controller.unit_document_path(first_root, "web--a").read_bytes()
    first_b_bytes = controller.unit_document_path(first_root, "web--b").read_bytes()

    write_atomic_template(template, version="v2", blocked_b=True)
    second_source_revision = commit(source, "block one updated Stack child")
    second_published = _apply(source, second_source_revision, template)
    assert second_published is not None
    second_root, second_resources = desired_stack_resources(store, second_published, tmp_path / "atomic-second")
    second_stack = second_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(second_stack.spec, DesiredStackSpec)
    assert second_stack.spec.activeProjection == first_stack.spec.activeProjection
    assert controller.unit_document_path(second_root, "web--a").read_bytes() == first_a_bytes
    assert controller.unit_document_path(second_root, "web--b").read_bytes() == first_b_bytes
    assert controller.load_desired_transition_blocks(second_root)["web--b"]

    producer_path = controller.unit_document_path(second_root, "producer")
    observed = tmp_path / "observed"
    observed.mkdir()
    receipt = receipt_resource(
        "terraform",
        "producer",
        {"revision": first_revision, "unitBlob": controller.file_blob(producer_path)},
        result={"applied": {"sourceRevision": first_revision}, "outputs": {"value": "evidence"}},
    )
    controller.write_document(
        observed / "units/producer.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish("observed/dev", observed, None, "publish producer evidence")

    # Durable progression must use the reviewed Project/Environment context,
    # not a later dirty worktree (including a tracked Environment deletion).
    (source / "deployment/environments/dev/environment.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": "wrong-environment"},
                "spec": {},
            }
        )
    )
    (source / "deployment/environments/dev/environment.json").unlink()
    (source / "gitopsctr.yaml").unlink()

    progressed_revision = controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    assert progressed_revision is not None

    progressed = store.fetch("deploy/dev").revision
    assert progressed is not None
    final_root, final_resources = desired_stack_resources(store, progressed, tmp_path / "atomic-final")
    final_stack = final_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(final_stack.spec, DesiredStackSpec)
    assert final_stack.spec.activeProjection is not None
    assert (
        final_stack.spec.activeProjection.sourceProjectionDigest
        == final_stack.spec.structuralProjection.identity.projectionDigest
    )
    assert set(final_stack.spec.activeProjection.units) == {"a", "b"}
    assert controller.load_desired_unit(
        controller.unit_document_path(final_root, "web--a"), "web--a"
    ).spec.terraform.variables == {"version": "v2"}  # type: ignore[union-attr]
    assert controller.load_desired_unit(
        controller.unit_document_path(final_root, "web--b"), "web--b"
    ).spec.terraform.variables == {"producer": "evidence"}  # type: ignore[union-attr]


def test_durable_projection_evaluates_every_saved_context_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    web_template = write_inline_template(source / "web-template.yaml", description="web")
    web_stack = write_inline_stack(source / "web.yaml", name="web")
    producer = _authored_unit(source / "producer.yaml", "producer")
    worker_template = source / "worker-template.yaml"
    worker_template.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "worker-template"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {
                                    "variables": {
                                        "value": {
                                            "fromReceipt": {
                                                "unit": "producer",
                                                "pointer": "/outputs/value",
                                            }
                                        }
                                    }
                                },
                            },
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    worker_stack = write_inline_stack(source / "worker.yaml", template="worker-template", name="worker")
    first_revision = commit(source, "publish first Stack context")
    first_published = _apply(source, first_revision, web_template, web_stack, producer)
    assert first_published is not None

    # Make the second Stack bind to a distinct immutable Project/Environment
    # context.  The explicit refs keep this source change from affecting the
    # test's desired/observed branches.
    worker_context_revision = first_revision
    worker_digest = ""
    web_digest = ""
    for attempt in range(4):
        environment_document = json.loads((source / "deployment/environments/dev/environment.json").read_text())
        environment_document["spec"] = {"refs": {"desired": f"ignored-desired-{attempt}", "observed": "observed/dev"}}
        (source / "deployment/environments/dev/environment.json").write_text(json.dumps(environment_document))
        worker_context_revision = commit(source, f"publish second Stack context {attempt}")
        worker_published = _apply(source, worker_context_revision, worker_template, worker_stack)
        assert worker_published is not None
        _current_root, current_resources = desired_stack_resources(
            store, worker_published, tmp_path / f"two-context-{attempt}"
        )
        web = current_resources[("gitopsctr.io/v1", "Stack", "web")]
        worker = current_resources[("gitopsctr.io/v1", "Stack", "worker")]
        assert isinstance(web.spec, DesiredStackSpec)
        assert isinstance(worker.spec, DesiredStackSpec)
        web_digest = web.spec.structuralProjection.identity.projectionContextDigest
        worker_digest = worker.spec.structuralProjection.identity.projectionContextDigest
        if worker_digest > web_digest:
            break
    assert worker_digest > web_digest

    desired_root, _resources = desired_stack_resources(store, worker_published, tmp_path / "two-context-observed")
    producer_path = controller.unit_document_path(desired_root, "producer")
    observed = tmp_path / "observed-two-contexts"
    observed.mkdir()
    receipt = receipt_resource(
        "terraform",
        "producer",
        {"revision": worker_published, "unitBlob": controller.file_blob(producer_path)},
        result={"applied": {"sourceRevision": worker_context_revision}, "outputs": {"value": "evidence"}},
    )
    controller.write_document(
        observed / "units/producer.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish("observed/dev", observed, None, "publish non-minimum context evidence")

    # Durable progression must use the saved context group roots, even when
    # the live source configuration has disappeared.
    (source / "deployment/environments/dev/environment.json").unlink()
    (source / "gitopsctr.yaml").unlink()
    progressed = controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    assert progressed is not None
    final_revision = store.fetch("deploy/dev").revision
    assert final_revision is not None
    final_root, final_resources = desired_stack_resources(store, final_revision, tmp_path / "two-context-final")
    final_web = final_resources[("gitopsctr.io/v1", "Stack", "web")]
    final_worker = final_resources[("gitopsctr.io/v1", "Stack", "worker")]
    assert isinstance(final_web.spec, DesiredStackSpec)
    assert isinstance(final_worker.spec, DesiredStackSpec)
    assert final_worker.spec.activeProjection is not None
    assert final_worker.spec.activeProjection.sourceProjectionDigest == (
        final_worker.spec.structuralProjection.identity.projectionDigest
    )
    assert controller.load_desired_unit(
        controller.unit_document_path(final_root, "worker--app"), "worker--app"
    ).spec.terraform.variables == {"value": "evidence"}  # type: ignore[union-attr]
    assert controller.load_desired_unit(
        controller.unit_document_path(final_root, "web--app"), "web--app"
    ).spec.terraform.variables == {"description": "web"}  # type: ignore[union-attr]


def test_durable_projection_cumulates_two_ready_context_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, published, root, resources = _two_context_blocked_stacks(tmp_path, monkeypatch)
    observed = tmp_path / "observed-both"
    observed.mkdir()
    for producer in ("web-producer", "worker-producer"):
        producer_path = controller.unit_document_path(root, producer)
        receipt = receipt_resource(
            "terraform",
            producer,
            {"revision": published, "unitBlob": controller.file_blob(producer_path)},
            result={"applied": {"sourceRevision": published}, "outputs": {"value": producer}},
        )
        controller.write_document(
            observed / "units" / f"{producer}.json",
            controller.RESOURCE_CATALOG.serialize_receipt(receipt),
            format=controller.DocumentFormat.JSON,
        )
    store.publish("observed/dev", observed, None, "publish both Stack inputs")

    controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    final_revision = store.fetch("deploy/dev").revision
    assert final_revision is not None
    final_root, final_resources = desired_stack_resources(store, final_revision, tmp_path / "both-ready")
    for stack_name in ("web", "worker"):
        stack = final_resources[("gitopsctr.io/v1", "Stack", stack_name)]
        assert isinstance(stack.spec, DesiredStackSpec)
        assert stack.spec.activeProjection is not None
        assert set(stack.spec.activeProjection.units) == {"app"}
        assert controller.unit_document_path(final_root, f"{stack_name}--app").is_file()
    assert controller.load_desired_transition_blocks(final_root) == {}


def test_durable_projection_preserves_an_earlier_ready_group_when_later_group_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _source, store, published, root, resources = _two_context_blocked_stacks(tmp_path, monkeypatch)
    digests = {
        name: resources[("gitopsctr.io/v1", "Stack", name)].spec.structuralProjection.identity.projectionContextDigest
        for name in ("web", "worker")
    }
    earlier = min(digests, key=digests.__getitem__)
    producer = f"{earlier}-producer"
    observed = tmp_path / "observed-one"
    observed.mkdir()
    producer_path = controller.unit_document_path(root, producer)
    receipt = receipt_resource(
        "terraform",
        producer,
        {"revision": published, "unitBlob": controller.file_blob(producer_path)},
        result={"applied": {"sourceRevision": published}, "outputs": {"value": "earlier-ready"}},
    )
    controller.write_document(
        observed / "units" / f"{producer}.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish("observed/dev", observed, None, "publish only earlier Stack input")

    controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    final_revision = store.fetch("deploy/dev").revision
    assert final_revision is not None
    final_root, final_resources = desired_stack_resources(store, final_revision, tmp_path / "one-ready")
    ready_stack = final_resources[("gitopsctr.io/v1", "Stack", earlier)]
    waiting = "worker" if earlier == "web" else "web"
    waiting_stack = final_resources[("gitopsctr.io/v1", "Stack", waiting)]
    assert isinstance(ready_stack.spec, DesiredStackSpec)
    assert isinstance(waiting_stack.spec, DesiredStackSpec)
    assert ready_stack.spec.activeProjection is not None
    assert set(ready_stack.spec.activeProjection.units) == {"app"}
    assert controller.unit_document_path(final_root, f"{earlier}--app").is_file()
    assert not controller.unit_document_path(final_root, f"{waiting}--app").is_file()
    assert f"{waiting}--app" in controller.load_desired_transition_blocks(final_root)


@pytest.mark.parametrize("policy_difference", ["changeGate", "effectLease"])
def test_durable_projection_rejects_incompatible_context_publication_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_difference: str,
):
    _source, _store, _published, _root, resources = _two_context_blocked_stacks(tmp_path, monkeypatch)
    first = resources[("gitopsctr.io/v1", "Stack", "web")]
    second = resources[("gitopsctr.io/v1", "Stack", "worker")]
    first_digest = "sha256:" + "a" * 64
    second_digest = "sha256:" + "b" * 64
    first_projection = StackProjection.build(
        stack_uid=first.spec.structuralProjection.identity.stackUid,
        template_uid=first.spec.structuralProjection.identity.templateUid,
        template_content_digest=first.spec.structuralProjection.identity.templateContentDigest,
        context_digest=first_digest,
        units=first.spec.structuralProjection.units,
    )
    second_projection = StackProjection.build(
        stack_uid=second.spec.structuralProjection.identity.stackUid,
        template_uid=second.spec.structuralProjection.identity.templateUid,
        template_content_digest=second.spec.structuralProjection.identity.templateContentDigest,
        context_digest=second_digest,
        units=second.spec.structuralProjection.units,
    )
    first = replace(first, spec=replace(first.spec, structuralProjection=first_projection))
    second = replace(second, spec=replace(second.spec, structuralProjection=second_projection))
    policy_resources = {
        ("gitopsctr.io/v1", "Stack", "web"): first,
        ("gitopsctr.io/v1", "Stack", "worker"): second,
    }
    first_root = tmp_path / "policy-first"
    second_root = tmp_path / "policy-second"
    project_repository(first_root)
    project_repository(second_root)
    if policy_difference == "changeGate":
        environment = second_root / "deployment/environments/dev/environment.json"
        document = json.loads(environment.read_text())
        document["spec"] = {"changeGate": "pullRequest"}
        environment.write_text(json.dumps(document))
    else:
        project = second_root / "gitopsctr.yaml"
        document = json.loads(project.read_text())
        document["spec"]["effectLease"] = {"store": {"branch": {"ref": "leases/other-{environment}"}}}
        project.write_text(json.dumps(document))

    with pytest.raises(OperationError, match="incompatible publication policies"):
        controller._validate_durable_publication_policies(
            "dev",
            "deploy/dev",
            policy_resources,
            {first_digest: first_root, second_digest: second_root},
        )


def test_blocked_topology_switch_drops_new_resolved_children_until_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_topology_template(source / "template.yaml", version="v1", blocked_b=False, include_c=False)
    stack = write_inline_stack(source / "stack.yaml")
    producer = _authored_unit(source / "producer.yaml", "producer")
    first_revision = commit(source, "publish initial active topology")
    first_published = _apply(source, first_revision, template, stack, producer)
    assert first_published is not None

    write_topology_template(template, version="v2", blocked_b=True, include_c=True)
    second_revision = commit(source, "publish changed topology with new child")
    second_published = _apply(source, second_revision, template)
    assert second_published is not None
    root, resources = desired_stack_resources(store, second_published, tmp_path / "topology-switch")
    stack_resource = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(stack_resource.spec, DesiredStackSpec)
    assert stack_resource.spec.activeProjection is not None
    assert set(stack_resource.spec.activeProjection.units) == {"a", "b"}
    assert stack_resource.spec.activeProjection.units["b"].dependsOn == []
    assert not controller.unit_document_path(root, "web--c").exists()
    assert controller.stack_dependency_edges(resources)["web--b"] == ()


def test_canonical_stacktemplate_update_recomputes_fanout_and_ignores_caller_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    web = write_inline_stack(source / "web.yaml", name="web")
    worker = write_inline_stack(source / "worker.yaml", name="worker")
    first_source_revision = commit(source, "publish canonical update fixture")
    first_published = _apply(source, first_source_revision, template, web, worker)
    assert first_published is not None
    _first_root, first_resources = desired_stack_resources(store, first_published, tmp_path / "canonical-first")
    first_template = first_resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    first_web = first_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(first_template.spec, DesiredStackTemplateSpec)
    assert isinstance(first_web.spec, DesiredStackSpec)

    content = StackTemplateUnitTemplate(
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        spec=TemplateObject({"source": {"path": "."}, "terraform": {"variables": {"description": "v2"}}}),
    )
    updated_spec_content = {"app": content}
    updated_content = StackTemplateSpec(
        parameters=list(first_template.spec.parameters),
        unitTemplates=updated_spec_content,
    )
    updated_spec = DesiredStackTemplateSpec(
        parameters=updated_content.parameters,
        unitTemplates=updated_content.unitTemplates,
        contentDigest=updated_content.semantic_content_digest(),
        acquisition=first_template.spec.acquisition,
    )
    canonical_template = replace(
        first_template,
        metadata=replace(first_template.metadata, uid="caller-template"),
        spec=updated_spec,
    )
    canonical_web_projection = StackProjection.build(
        stack_uid="caller-stack",
        template_uid="caller-template",
        template_content_digest=updated_spec.contentDigest,
        context_digest=first_web.spec.structuralProjection.identity.projectionContextDigest,
        units={
            "app": StackProjectionUnit(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                spec=TemplateObject(dict(content.spec)),
                dependsOn=[],
            )
        },
    )
    canonical_web = replace(
        first_web,
        metadata=replace(first_web.metadata, uid="caller-stack"),
        spec=replace(
            first_web.spec,
            templateRef=replace(
                first_web.spec.templateRef,
                uid="caller-template",
                contentDigest=updated_spec.contentDigest,
            ),
            structuralProjection=canonical_web_projection,
        ),
    )
    canonical_template_path = source / "canonical-template.yaml"
    canonical_stack_path = source / "canonical-stack.yaml"
    canonical_template_path.write_text(
        yaml.safe_dump(controller.RESOURCE_CATALOG.serialize_stack_resource(canonical_template, profile="desired"))
    )
    canonical_stack_path.write_text(
        yaml.safe_dump(controller.RESOURCE_CATALOG.serialize_stack_resource(canonical_web, profile="desired"))
    )
    second_source_revision = commit(source, "apply canonical controller resources")

    second_published = _apply(source, second_source_revision, canonical_template_path, canonical_stack_path)
    assert second_published is not None
    root, resources = desired_stack_resources(store, second_published, tmp_path / "canonical-second")
    template_resource = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    assert template_resource.metadata.uid == first_template.metadata.uid
    for name in ("web", "worker"):
        stack_resource = resources[("gitopsctr.io/v1", "Stack", name)]
        assert stack_resource.metadata.uid == first_resources[("gitopsctr.io/v1", "Stack", name)].metadata.uid
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}--app"), f"{name}--app")
        assert unit.spec.terraform.variables == {"description": "v2"}  # type: ignore[union-attr]

    invalid_stack = replace(
        canonical_web,
        spec=replace(canonical_web.spec, templateRef=replace(canonical_web.spec.templateRef, name="missing")),
    )
    invalid_path = source / "invalid-stack.yaml"
    invalid_path.write_text(
        yaml.safe_dump(controller.RESOURCE_CATALOG.serialize_stack_resource(invalid_stack, profile="desired"))
    )
    invalid_revision = commit(source, "reject invalid canonical referrer")
    before = store.fetch("deploy/dev").revision
    with pytest.raises(OperationError, match="missing desired StackTemplate 'missing'"):
        _apply(source, invalid_revision, invalid_path)
    assert store.fetch("deploy/dev").revision == before


def test_partitioned_stack_requires_its_same_partition_template_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    first_revision = commit(source, "publish partitioned Stack aggregate")
    assert _apply(source, first_revision, template, stack, partition="application") is not None

    stack_only = write_inline_stack(source / "stack-only.yaml")
    second_revision = commit(source, "apply Stack without its partition template")
    before = store.fetch("deploy/dev").revision
    with pytest.raises(OperationError, match="references deleting StackTemplate 'preview'"):
        _apply(source, second_revision, stack_only, partition="application")
    assert store.fetch("deploy/dev").revision == before


def test_template_only_partition_apply_prunes_stack_fanout_but_keeps_template_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    first_revision = commit(source, "publish partitioned template and Stack")
    first_published = _apply(source, first_revision, template, stack, partition="application")
    assert first_published is not None

    second_revision = first_revision
    second_published = _apply(source, second_revision, template, partition="application")
    assert second_published is not None
    _root, resources = desired_stack_resources(store, second_published, tmp_path / "template-only")
    desired_template = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    desired_stack = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert controller.resource_deletion(desired_template) is None
    assert controller.resource_deletion(desired_stack) is not None
    owned = resources[("unit.gitopsctr.io/v1", "Terraform", "web--app")]
    assert controller.resource_deletion(owned) is not None


def test_unrelated_stack_roots_and_owned_closures_are_carried_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    web = write_inline_stack(source / "web.yaml", name="web")
    worker = write_inline_stack(source / "worker.yaml", name="worker")
    first_revision = commit(source, "publish unrelated Stack roots")
    first_published = _apply(source, first_revision, template, web, worker)
    assert first_published is not None
    first = tmp_path / "first"
    store.materialize(first_published, first)
    preserved_paths = tuple(
        controller.document_candidates(directory, name)[0]
        for directory, name in (
            (first / "stack-templates", "preview"),
            (first / "stacks", "web"),
            (first / "stacks", "worker"),
            (first / "units", "web--app"),
            (first / "units", "worker--app"),
        )
    )
    preserved = {path.relative_to(first): path.read_bytes() for path in preserved_paths}

    unrelated = _authored_source_less_unit(source / "unrelated.yaml", "unrelated")
    second_revision = commit(source, "apply unrelated root")
    second_published = _apply(source, second_revision, unrelated)
    assert second_published is not None
    second = tmp_path / "second"
    store.materialize(second_published, second)
    assert {relative: (second / relative).read_bytes() for relative in preserved} == preserved


def test_later_stack_apply_uses_persisted_template_source_context_without_a_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "deployment/stack-templates/preview.yaml")
    template_revision = commit(source, "add source-backed inline template")
    template_published = _apply(source, template_revision, template)
    assert template_published is not None

    stack = write_inline_stack(source / "stack.yaml")
    commit(source, "add Stack after template publication")
    published = _apply_worktree(stack)
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "desired")

    desired_template = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    assert isinstance(desired_template.spec, DesiredStackTemplateSpec)
    assert desired_template.spec.sourceContext is not None
    assert desired_template.spec.sourceContext.revision == template_revision
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web--app"), "web--app")
    assert generated.spec.source.revision == template_revision  # type: ignore[union-attr]


def test_revision_backed_stacktemplate_preserves_support_payload_under_template_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = source / "deployment/stack-templates/preview.yaml"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {"source": {"path": "deployment/stack-templates/support"}},
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    support = template.parent / "support"
    support.mkdir()
    (support / "backend.tf").write_text('terraform { backend "local" {} }\n')
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "publish template with supporting payload")

    published = _apply(source, revision, template, stack)
    assert published is not None
    root, _resources = desired_stack_resources(store, published, tmp_path / "support-payload")
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web--app"), "web--app")
    assert generated.spec.source.path == "deployment/stack-templates/support"  # type: ignore[union-attr]
    assert generated.spec.source.revision == revision  # type: ignore[union-attr]


def test_object_parameter_source_context_is_pinned_for_later_stack_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = source / "template.yaml"
    template.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [{"name": "whole-source", "type": "object"}],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {"source": {"fromParameter": {"name": "whole-source"}}},
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    template_revision = commit(source, "pin parameter-created source context")
    assert _apply(source, template_revision, template) is not None

    stack = write_inline_stack(source / "stack.yaml")
    stack.write_text(
        stack.read_text().replace(
            "spec:\n  template: preview", "spec:\n  template: preview\n  parameters:\n    whole-source:\n      path: ."
        )
    )
    commit(source, "apply Stack with object source parameter")
    published = _apply_worktree(stack)
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "parameter-source")
    template_resource = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    assert isinstance(template_resource.spec, DesiredStackTemplateSpec)
    assert template_resource.spec.sourceContext is not None
    assert template_resource.spec.sourceContext.revision == template_revision
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web--app"), "web--app")
    assert generated.spec.source.revision == template_revision  # type: ignore[union-attr]


def test_concrete_stack_unit_tampering_breaks_active_projection_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "publish bound stack unit")
    published = _apply(source, revision, template, stack)
    assert published is not None
    desired = tmp_path / "tampered"
    store.materialize(published, desired)
    unit_path = controller.unit_document_path(desired, "web--app")
    document = controller.RESOURCE_CATALOG.load_document(unit_path)
    document["spec"]["terraform"]["variables"] = {"description": "tampered"}  # type: ignore[index]
    unit_path.write_text(json.dumps(document))

    with pytest.raises(OperationError, match="does not authenticate Unit 'web--app'"):
        controller.load_desired_resource_graph(desired)


def test_missing_template_fails_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _initial_revision = _repository(tmp_path, monkeypatch)
    before = store.fetch("deploy/dev").revision
    stack = write_inline_stack(source / "stack.yaml", template="missing")
    revision = commit(source, "add Stack with missing template")

    with pytest.raises(OperationError, match="missing desired StackTemplate 'missing'"):
        _apply(source, revision, stack)

    assert store.fetch("deploy/dev").revision == before


def test_dynamic_unit_projection_waits_without_active_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _initial_revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", dynamic=True)
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "add Stack with unresolved artifact projection")
    published = _apply(source, revision, template, stack)
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "desired")
    desired_stack = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(desired_stack.spec, DesiredStackSpec)
    assert desired_stack.spec.structuralProjection.units["app"].spec["terraform"]
    assert desired_stack.spec.activeProjection is not None
    assert desired_stack.spec.activeProjection.units == {}
    assert not controller.unit_document_path(root, "web--app").exists()
    assert "web--app" in controller.load_desired_transition_blocks(root)


def test_durable_projection_does_not_create_a_provenance_only_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _initial_revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "publish durable provenance fixture")
    published = _apply(source, revision, template, stack)
    assert published is not None

    progressed = controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    assert progressed == published
    assert store.fetch("deploy/dev").revision == published


def test_projected_unit_from_environment_reference_resolves_through_normal_unit_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _initial_revision = _repository(tmp_path, monkeypatch)
    template = source / "template.yaml"
    template.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "."},
                                "terraform": {"variables": {"environment": {"fromEnvironment": {"pointer": "/name"}}}},
                            },
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    stack = write_inline_stack(source / "stack.yaml")
    revision = commit(source, "resolve projected environment reference")
    published = _apply(source, revision, template, stack)
    assert published is not None
    root, _resources = desired_stack_resources(store, published, tmp_path / "environment-reference")
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web--app"), "web--app")
    assert generated.spec.terraform.variables == {"environment": "dev"}  # type: ignore[union-attr]


def test_unreferenced_template_recreation_after_tombstone_gets_a_new_uid(tmp_path: Path):
    source = tmp_path / "source"
    project_repository(source)
    write_inline_template(source / "deployment/stack-templates/preview.yaml")
    first_candidate = tmp_path / "first"
    controller.project_stack_resources(source, "dev", "a" * 40, first_candidate, source)
    first_resource = controller.load_desired_resource_graph(first_candidate)[
        ("gitopsctr.io/v1", "StackTemplate", "preview")
    ]
    first_uid = first_resource.metadata.uid
    assert first_uid is not None
    current = tmp_path / "current"
    current.mkdir()
    controller.write_resource_incarnation_tombstone(
        current,
        controller.ResourceIncarnationTombstone(
            api_version="gitopsctr.io/v1",
            kind="StackTemplate",
            name="preview",
            uid=first_uid,
            deletion_generation=1,
        ),
    )
    recreated = tmp_path / "recreated"
    controller.project_stack_resources(source, "dev", "a" * 40, recreated, source, current)
    recreated_resource = controller.load_desired_resource_graph(recreated)[
        ("gitopsctr.io/v1", "StackTemplate", "preview")
    ]
    assert recreated_resource.metadata.uid != first_uid


def test_three_stacktemplate_incarnations_remain_distinct_and_rollback_is_fenced(tmp_path: Path):
    source = tmp_path / "source"
    project_repository(source)
    write_inline_template(source / "deployment/stack-templates/preview.yaml")
    first = tmp_path / "first"
    controller.project_stack_resources(source, "dev", "a" * 40, first, source)
    first_resource = controller.load_desired_resource_graph(first)[("gitopsctr.io/v1", "StackTemplate", "preview")]
    first_uid = first_resource.metadata.uid
    assert first_uid is not None

    second_current = tmp_path / "second-current"
    second_current.mkdir()
    controller.write_resource_incarnation_tombstone(
        second_current,
        controller.ResourceIncarnationTombstone(
            api_version="gitopsctr.io/v1",
            kind="StackTemplate",
            name="preview",
            uid=first_uid,
            deletion_generation=1,
        ),
    )
    second = tmp_path / "second"
    controller.project_stack_resources(source, "dev", "a" * 40, second, source, second_current)
    second_resource = controller.load_desired_resource_graph(second)[("gitopsctr.io/v1", "StackTemplate", "preview")]
    second_uid = second_resource.metadata.uid
    assert second_uid is not None and second_uid != first_uid

    third_current = tmp_path / "third-current"
    third_current.mkdir()
    controller.write_resource_incarnation_tombstone(
        third_current,
        controller.ResourceIncarnationTombstone(
            api_version="gitopsctr.io/v1",
            kind="StackTemplate",
            name="preview",
            uid=second_uid,
            deletion_generation=1,
        ),
    )
    third = tmp_path / "third"
    controller.project_stack_resources(source, "dev", "a" * 40, third, source, third_current)
    third_resource = controller.load_desired_resource_graph(third)[("gitopsctr.io/v1", "StackTemplate", "preview")]
    third_uid = third_resource.metadata.uid
    assert third_uid not in {first_uid, second_uid}

    with pytest.raises(OperationError, match="cross the current StackTemplate .* incarnation"):
        controller.validate_full_rollback_stack_aggregate(third, first)
