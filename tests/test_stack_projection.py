from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gitopsctr import controller
from gitopsctr.adapters.git.apply import publish_durable_candidate
from gitopsctr.application.model import ChannelId
from gitopsctr.contracts import (
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    StackProjection,
    StackProjectionUnit,
    StackTemplateReference,
    StackTemplateSpec,
    StackTemplateUnitTemplate,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, StackResource
from gitopsctr.state import GitStateStore
from gitopsctr.templates import TemplateObject
from tests.conftest import receipt_resource
from tests.stack_support import cloned_project_repository as _repository
from tests.stack_support import commit, git, project_repository
from tests.test_apply import _apply, _apply_worktree, _authored_source_less_unit, _authored_unit


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


def write_revision_template(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [{"name": "workload-revision", "type": "string"}],
                    "unitTemplates": {
                        "app": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {
                                    "path": ".",
                                    "inputs": ["workload.txt"],
                                    "revision": {"fromParameter": {"name": "workload-revision"}},
                                }
                            },
                        }
                    },
                },
            },
            sort_keys=False,
        )
    )
    return path


def test_stack_workload_checkout_cache_is_fenced_to_acquired_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "b" * 40
    first_history = "c" * 40
    second_history = "a" * 40
    calls: list[tuple[str, str, str | None]] = []

    class Store:
        def resolve_source(self, repository: str, ref: str, revision: str | None = None) -> object:
            calls.append((repository, ref, revision))
            return SimpleNamespace(revision=revision)

        def materialize_source(self, _source: object, output: Path) -> None:
            output.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(controller, "state_store", lambda: Store())
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: False)
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    cache: dict[tuple[str, str, str], Path] = {}

    controller._materialize_stack_workload_revision(
        "https://example.invalid/repository.git",
        revision,
        inherited,
        first_history,
        candidate,
        cache,
        transport=controller.StackUnitSourceTransport("transport", first_history),
    )
    controller._materialize_stack_workload_revision(
        "https://example.invalid/repository.git",
        revision,
        inherited,
        second_history,
        candidate,
        cache,
        transport=controller.StackUnitSourceTransport("transport", second_history),
    )

    assert calls == [
        ("transport", first_history, revision),
        ("transport", second_history, revision),
    ]


def test_stack_workload_local_object_requires_repository_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "b" * 40
    inherited_revision = "c" * 40

    class Store:
        def resolve_source(self, _repository: str, _revision: str) -> object:
            raise OperationError("repository does not contain revision")

    monkeypatch.setattr(controller, "state_store", lambda: Store())
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(controller, "commit_is_ancestor", lambda _revision, _inherited: True)
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    with pytest.raises(OperationError, match="is unavailable in repository"):
        controller._materialize_stack_workload_revision(
            "https://repository-a.invalid/source.git",
            revision,
            inherited,
            inherited_revision,
            candidate,
            {},
            authenticated=False,
        )


def test_acquired_template_context_authenticates_a_local_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = "https://private.example/repository.git"
    revision = "b" * 40
    inherited_revision = "c" * 40

    class Store:
        def resolve_source(self, _repository: str, _revision: str) -> object:
            pytest.fail("authenticated local ancestor should not require origin access")

    monkeypatch.setattr(controller, "state_store", lambda: Store())
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(controller, "commit_is_ancestor", lambda _revision, _inherited: True)
    monkeypatch.setattr(
        controller,
        "materialize_revision",
        lambda _revision, output: output.mkdir(parents=True),
    )
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    checkout = controller._materialize_stack_workload_revision(
        repository,
        revision,
        inherited,
        inherited_revision,
        candidate,
        {},
        authenticated_context=controller.AuthenticatedStackTemplateContext(repository, inherited_revision),
    )

    assert checkout.is_dir()


def test_stack_workload_pin_hydration_still_enforces_acquired_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "b" * 40
    inherited_revision = "a" * 40

    class Store:
        def resolve_source(self, _repository: str, _revision: str) -> object:
            raise OperationError("origin unavailable")

        def hydrate_source_revision(self, _name: str, _revision: str) -> None:
            return None

    monkeypatch.setattr(controller, "state_store", lambda: Store())
    monkeypatch.setattr(controller, "commit_is_available", lambda _revision: True)
    monkeypatch.setattr(controller, "commit_is_ancestor", lambda _revision, _inherited: False)
    monkeypatch.setattr(
        controller,
        "materialize_revision",
        lambda _revision, _output: pytest.fail("out-of-history workload revision was materialized"),
    )
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    with pytest.raises(OperationError, match="is unavailable in repository"):
        controller._materialize_stack_workload_revision(
            "https://example.invalid/repository.git",
            revision,
            inherited,
            inherited_revision,
            candidate,
            {},
            retention_pin_name="stack-templates/dev/preview/template/stacks/web/stack/revision",
        )


def write_revision_stack(path: Path, *, name: str, revision: str) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": name},
                "spec": {"template": "preview", "parameters": {"workload-revision": revision}},
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


def write_internal_receipt_template(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "parameters": [],
                    "unitTemplates": {
                        "image": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {"source": {"path": "image", "inputs": ["**/*"]}},
                        },
                        "deploy": {
                            "apiVersion": "unit.gitopsctr.io/v1",
                            "kind": "Terraform",
                            "spec": {
                                "source": {"path": "deploy", "inputs": ["**/*"]},
                                "terraform": {
                                    "variables": {
                                        "image": {
                                            "fromReceipt": {
                                                "unit": "image",
                                                "pointer": "/outputs/value",
                                            }
                                        }
                                    }
                                },
                            },
                            "dependsOn": ["image"],
                        },
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
    assert not controller.unit_document_path(_root, "web/b").exists()
    assert not controller.unit_document_path(_root, "web/c").exists()


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
    assert desired_template.spec.acquisition.requestedSource.fromInput.__class__.__name__ == "StackTemplateFromInput"
    assert desired_template.spec.acquisition.documentDigest == (
        "sha256:" + hashlib.sha256(template.read_bytes()).hexdigest()
    )
    assert desired_template.metadata.partition == "application"
    assert desired_stack.spec.templateRef.uid == desired_template.metadata.uid
    assert desired_stack.spec.templateRef.contentDigest == desired_template.spec.contentDigest
    assert desired_stack.spec.structuralProjection.identity.stackUid == desired_stack.metadata.uid
    assert desired_stack.spec.structuralProjection.identity.templateUid == desired_template.metadata.uid

    generated = controller.load_desired_unit(controller.unit_document_path(root, "web/app"), "web/app")
    owner = controller.resource_owner_reference(generated)
    assert owner is not None
    assert owner.uid == desired_stack.metadata.uid
    assert generated.metadata.uid is not None


def test_template_update_preserves_identities_and_fans_out_to_two_stacks_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml", description="v1")
    web = write_inline_stack(source / "web.yaml", name="web")
    worker = write_inline_stack(source / "worker.yaml", name="worker")
    first_source_revision = commit(source, "publish two inline Stacks")
    first_published = _apply(source, first_source_revision, template, web, worker)
    assert first_published is not None
    first_root, first_resources = desired_stack_resources(store, first_published, tmp_path / "fanout-first")
    first_template = first_resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    assert isinstance(first_template.spec, DesiredStackTemplateSpec)
    first_units = {
        name: controller.load_desired_unit(controller.unit_document_path(first_root, f"{name}/app"), f"{name}/app")
        for name in ("web", "worker")
    }

    write_inline_template(template, description="v2")
    second_source_revision = commit(source, "fan out template update")
    second_published = _apply(source, second_source_revision, template)
    assert second_published is not None
    root, resources = desired_stack_resources(store, second_published, tmp_path / "fanout")
    second_template = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    assert isinstance(second_template.spec, DesiredStackTemplateSpec)
    assert second_template.metadata.uid == first_template.metadata.uid
    assert second_template.spec.contentDigest != first_template.spec.contentDigest
    for name in ("web", "worker"):
        stack = resources[("gitopsctr.io/v1", "Stack", name)]
        first_stack = first_resources[("gitopsctr.io/v1", "Stack", name)]
        assert isinstance(stack.spec, DesiredStackSpec)
        assert stack.metadata.uid == first_stack.metadata.uid
        assert stack.spec.templateRef.contentDigest == second_template.spec.contentDigest
        assert stack.spec.structuralProjection.identity.templateContentDigest == second_template.spec.contentDigest
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}/app"), f"{name}/app")
        assert unit.spec != first_units[name].spec
        assert unit.spec.terraform.variables == {"description": "v2"}  # type: ignore[union-attr]
        assert stack.spec.activeProjection is not None
        assert set(stack.spec.activeProjection.units) == {"app"}
        owner = controller.resource_owner_reference(unit)
        assert owner is not None
        assert owner.uid == first_stack.metadata.uid

    write_inline_stack(worker, template="missing", name="worker")
    invalid_revision = commit(source, "break second referrer")
    before = store.fetch("deploy/dev").revision
    with pytest.raises(OperationError, match="missing desired StackTemplate 'missing'"):
        _apply(source, invalid_revision, template, worker)
    assert store.fetch("deploy/dev").revision == before


def test_stacks_select_and_retain_exact_workload_revisions_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_revision_template(source / "template.yaml")
    (source / "workload.txt").write_text("identical workload bytes\n")
    first_revision = commit(source, "publish revision-selecting template")
    (source / "workload.txt").write_text("identical workload bytes\n")
    (source / "revision-marker.txt").write_text("second revision\n")
    second_revision = commit(source, "publish second workload revision")
    web = write_revision_stack(source / "web.yaml", name="web", revision=first_revision)
    worker = write_revision_stack(source / "worker.yaml", name="worker", revision=second_revision)
    source_revision = commit(source, "select independent workload revisions")

    published = _apply(source, source_revision, template, web, worker)
    assert published is not None
    root, resources = desired_stack_resources(store, published, tmp_path / "selected-revisions")
    revisions = {}
    input_hashes = {}
    pins = {pin.name for pin in store.list_controller_pins()}
    for name in ("web", "worker"):
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}/app"), f"{name}/app")
        revisions[name] = unit.spec.source.revision  # type: ignore[union-attr]
        input_hashes[name] = unit.spec.source.inputHash  # type: ignore[union-attr]
        stack = resources[("gitopsctr.io/v1", "Stack", name)]
        assert isinstance(stack.spec, DesiredStackSpec)
        assert stack.spec.structuralProjection.units["app"].spec["source"]["revision"] == revisions[name]
        assert any(pin.endswith(f"/stacks/{name}/{stack.metadata.uid}/{revisions[name]}") for pin in pins)
    assert revisions == {"web": first_revision, "worker": second_revision}
    assert input_hashes["web"] == input_hashes["worker"]

    (source / "workload.txt").write_text("revision-c\n")
    third_revision = commit(source, "advance inherited template context")
    # The explicit per-Stack revisions remain stable when the template context
    # advances; an omitted revision is covered by the next assertion.
    published = _apply(source, third_revision, template)
    assert published is not None
    root, _resources = desired_stack_resources(store, published, tmp_path / "retained-overrides")
    for name, expected in (("web", first_revision), ("worker", second_revision)):
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}/app"), f"{name}/app")
        assert unit.spec.source.revision == expected  # type: ignore[union-attr]


def test_source_revision_selection_is_not_part_of_unit_input_hash(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "workload.txt").write_text("same bytes\n")
    (root_b / "workload.txt").write_text("same bytes\n")

    def parse(revision: str | None):
        source: dict[str, Any] = {"path": "."}
        if revision is not None:
            source["revision"] = revision
        return controller.RESOURCE_CATALOG.parse_unit(
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": {"name": "app"},
                "spec": {"source": source},
            },
            profile="authored",
            expected_name="app",
        )

    revision_a = "a" * 40
    revision_b = "b" * 40
    hash_a = controller.unit_input_hash(parse(revision_a), root_a)
    hash_b = controller.unit_input_hash(parse(revision_b), root_b)
    legacy_hash = controller.unit_input_hash(parse(None), root_a)
    assert hash_a == hash_b == legacy_hash


def test_direct_authored_unit_rejects_stack_only_source_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, _store, _initial = _repository(tmp_path, monkeypatch)
    unit = _authored_unit(source / "direct.yaml", "direct")
    document = yaml.safe_load(unit.read_text())
    document["spec"]["source"]["revision"] = "a" * 40
    unit.write_text(yaml.safe_dump(document, sort_keys=False))
    source_revision = commit(source, "direct revision must remain unsupported")

    with pytest.raises(OperationError, match="only in a StackTemplate projection"):
        _apply(source, source_revision, unit)


def test_inherited_workload_revision_advances_with_template_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_inline_template(source / "template.yaml")
    stack = write_inline_stack(source / "stack.yaml")
    (source / "workload.txt").write_text("revision-a\n")
    first_revision = commit(source, "publish inherited workload")
    first_published = _apply(source, first_revision, template, stack)
    assert first_published is not None
    first_root, _resources = desired_stack_resources(store, first_published, tmp_path / "inherited-first")
    first_unit = controller.load_desired_unit(controller.unit_document_path(first_root, "web/app"), "web/app")
    assert first_unit.spec.source.revision == first_revision  # type: ignore[union-attr]

    (source / "workload.txt").write_text("revision-b\n")
    second_revision = commit(source, "advance inherited workload")
    second_published = _apply(source, second_revision, template)
    assert second_published is not None
    second_root, _resources = desired_stack_resources(store, second_published, tmp_path / "inherited-second")
    second_unit = controller.load_desired_unit(controller.unit_document_path(second_root, "web/app"), "web/app")
    assert second_unit.spec.source.revision == second_revision  # type: ignore[union-attr]
    assert second_unit.spec.source.inputHash != first_unit.spec.source.inputHash  # type: ignore[union-attr]


def test_unavailable_exact_stack_revision_fails_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_revision_template(source / "template.yaml")
    stack = write_revision_stack(source / "stack.yaml", name="web", revision="f" * 40)
    source_revision = commit(source, "publish invalid workload selection")
    before = store.fetch("deploy/dev").revision
    with pytest.raises(OperationError, match="unavailable in repository"):
        _apply(source, source_revision, template, stack)
    assert store.fetch("deploy/dev").revision == before


def test_stack_projection_wait_stages_ready_units_then_durable_evidence_switches_blocked_units(
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
    first_a_bytes = controller.unit_document_path(first_root, "web/a").read_bytes()
    first_b_bytes = controller.unit_document_path(first_root, "web/b").read_bytes()

    write_atomic_template(template, version="v2", blocked_b=True)
    second_source_revision = commit(source, "block one updated Stack child")
    second_published = _apply(source, second_source_revision, template)
    assert second_published is not None
    second_root, second_resources = desired_stack_resources(store, second_published, tmp_path / "atomic-second")
    second_stack = second_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(second_stack.spec, DesiredStackSpec)
    assert second_stack.spec.activeProjection is not None
    assert (
        second_stack.spec.activeProjection.sourceProjectionDigest
        == second_stack.spec.structuralProjection.identity.projectionDigest
    )
    assert (
        second_stack.spec.activeProjection.units["a"].sourceProjectionDigest
        == second_stack.spec.structuralProjection.identity.projectionDigest
    )
    assert (
        second_stack.spec.activeProjection.units["b"].sourceProjectionDigest
        == first_stack.spec.activeProjection.units["b"].sourceProjectionDigest
    )
    assert controller.unit_document_path(second_root, "web/a").read_bytes() != first_a_bytes
    assert controller.unit_document_path(second_root, "web/b").read_bytes() == first_b_bytes
    assert controller.load_desired_unit(
        controller.unit_document_path(second_root, "web/a"), "web/a"
    ).spec.terraform.variables == {"version": "v2"}  # type: ignore[union-attr]
    assert controller.load_desired_transition_blocks(second_root)["web/b"]

    producer_path = controller.unit_document_path(second_root, "producer")
    observed = tmp_path / "observed"
    observed.mkdir()
    receipt = receipt_resource(
        "terraform",
        "producer",
        {"revision": first_revision, "unitContentId": controller.unit_content_id(second_root, producer_path)},
        result={"applied": {"sourceRevision": first_revision}, "outputs": {"value": "evidence"}},
    )
    controller.write_document(
        observed / "units/producer.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish("observed/dev", observed, None, "publish producer evidence", expected_publication_head=None)

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
        controller.unit_document_path(final_root, "web/a"), "web/a"
    ).spec.terraform.variables == {"version": "v2"}  # type: ignore[union-attr]
    assert controller.load_desired_unit(
        controller.unit_document_path(final_root, "web/b"), "web/b"
    ).spec.terraform.variables == {"producer": "evidence"}  # type: ignore[union-attr]


def test_stack_source_update_stages_changed_producer_before_its_stale_receipt_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    template = write_internal_receipt_template(source / "template.yaml")
    stack = write_inline_stack(source / "stack.yaml")
    (source / "image").mkdir()
    (source / "image/main.tf").write_text("image-v1\n")
    (source / "deploy").mkdir()
    (source / "deploy/main.tf").write_text("deploy-v1\n")
    first_revision = commit(source, "publish receipt-dependent Stack")
    first_published = _apply(source, first_revision, template, stack)
    assert first_published is not None
    first_root, first_resources = desired_stack_resources(store, first_published, tmp_path / "receipt-first")
    first_stack = first_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(first_stack.spec, DesiredStackSpec)
    assert first_stack.spec.activeProjection is not None
    assert set(first_stack.spec.activeProjection.units) == {"image"}

    image_path = controller.unit_document_path(first_root, "web/image")
    observed = tmp_path / "receipt-observed"
    observed.mkdir()
    receipt = receipt_resource(
        "terraform",
        "web/image",
        {"revision": first_revision, "unitContentId": controller.unit_content_id(first_root, image_path)},
        result={"applied": {"sourceRevision": first_revision}, "outputs": {"value": "image-v1"}},
    )
    controller.write_document(
        observed / "units/web/image.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish("observed/dev", observed, None, "publish image receipt", expected_publication_head=None)
    progressed = controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev")
    assert progressed is not None

    (source / "image/main.tf").write_text("image-v2\n")
    second_revision = commit(source, "change only the Stack producer inputs")
    second_published = _apply(source, second_revision, template, stack)
    assert second_published is not None
    second_root, second_resources = desired_stack_resources(store, second_published, tmp_path / "receipt-second")
    second_stack = second_resources[("gitopsctr.io/v1", "Stack", "web")]
    assert isinstance(second_stack.spec, DesiredStackSpec)
    assert second_stack.spec.activeProjection is not None
    assert set(second_stack.spec.activeProjection.units) == {"deploy", "image"}
    assert controller.load_desired_transition_blocks(second_root)["web/deploy"].startswith("receipt is stale")
    image = controller.load_desired_unit(controller.unit_document_path(second_root, "web/image"), "web/image")
    deploy = controller.load_desired_unit(controller.unit_document_path(second_root, "web/deploy"), "web/deploy")
    assert image.spec.source.revision == second_revision  # type: ignore[union-attr]
    assert deploy.spec.source.revision == first_revision  # type: ignore[union-attr]
    assert (
        second_stack.spec.activeProjection.units["image"].sourceProjectionDigest
        == second_stack.spec.structuralProjection.identity.projectionDigest
    )
    assert (
        second_stack.spec.activeProjection.units["deploy"].sourceProjectionDigest
        != second_stack.spec.structuralProjection.identity.projectionDigest
    )


def test_durable_projection_progresses_saved_context_groups_cumulatively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, published, root, resources = _two_context_blocked_stacks(tmp_path, monkeypatch)
    digests = {
        name: resources[("gitopsctr.io/v1", "Stack", name)].spec.structuralProjection.identity.projectionContextDigest
        for name in ("web", "worker")
    }
    earlier = min(digests, key=digests.__getitem__)
    later = "worker" if earlier == "web" else "web"

    def reset_refs() -> None:
        current_revision = store.fetch("deploy/dev").revision
        assert current_revision is not None
        reset = publish_durable_candidate(
            source,
            "dev",
            ChannelId("deploy/dev"),
            current_revision,
            root,
        )
        assert reset.snapshot_id is not None
        if store.fetch("observed/dev").revision is not None:
            git(source, "push", "origin", "--delete", "observed/dev")

    def publish_receipts(*stack_names: str) -> None:
        observed = tmp_path / ("observed-" + "-".join(stack_names))
        observed.mkdir()
        for stack_name in stack_names:
            producer = f"{stack_name}-producer"
            producer_path = controller.unit_document_path(root, producer)
            receipt = receipt_resource(
                "terraform",
                producer,
                {"revision": published, "unitContentId": controller.unit_content_id(root, producer_path)},
                result={"applied": {"sourceRevision": published}, "outputs": {"value": producer}},
            )
            controller.write_document(
                observed / "units" / f"{producer}.json",
                controller.RESOURCE_CATALOG.serialize_receipt(receipt),
                format=controller.DocumentFormat.JSON,
            )
        store.publish(
            "observed/dev",
            observed,
            None,
            "publish Stack projection inputs",
            expected_publication_head=None,
        )

    @dataclass(frozen=True)
    class ProgressedProjection:
        root: Path
        resources: dict[tuple[str, str, str], Any]

    def progress(label: str) -> ProgressedProjection:
        assert controller.progress_durable_stack_projection("dev", "deploy/dev", "observed/dev") is not None
        revision = store.fetch("deploy/dev").revision
        assert revision is not None
        final_root, final_resources = desired_stack_resources(store, revision, tmp_path / label)
        return ProgressedProjection(final_root, final_resources)

    def assert_projection_state(final_root: Path, final_resources: dict, *ready_names: str) -> None:
        ready = set(ready_names)
        blocks = controller.load_desired_transition_blocks(final_root)
        for stack_name in ("web", "worker"):
            stack = final_resources[("gitopsctr.io/v1", "Stack", stack_name)]
            assert isinstance(stack.spec, DesiredStackSpec)
            unit_path = controller.unit_document_path(final_root, f"{stack_name}/app")
            if stack_name in ready:
                assert stack.spec.activeProjection is not None
                assert set(stack.spec.activeProjection.units) == {"app"}
                assert unit_path.is_file()
                assert f"{stack_name}/app" not in blocks
            else:
                assert not unit_path.is_file()
                assert f"{stack_name}/app" in blocks

    # A non-minimum context must progress without consulting the deleted live
    # Project/Environment files. This catches starvation by digest ordering.
    publish_receipts(later)
    (source / "deployment/environments/dev/environment.json").unlink()
    (source / "gitopsctr.yaml").unlink()
    final = progress("later-ready")
    assert_projection_state(final.root, final.resources, later)

    # Multiple ready groups must accumulate into one candidate instead of a
    # later group restoring an earlier group's transition block.
    reset_refs()
    publish_receipts("web", "worker")
    final = progress("both-ready")
    assert_projection_state(final.root, final.resources, "web", "worker")

    # A ready earlier group must survive evaluation of a later waiting group.
    reset_refs()
    publish_receipts(earlier)
    final = progress("earlier-ready")
    assert_projection_state(final.root, final.resources, earlier)


@pytest.mark.parametrize("policy_difference", ["changeGate", "effectLease"])
def test_durable_projection_rejects_incompatible_context_publication_policy(
    tmp_path: Path,
    policy_difference: str,
):
    first_digest = "sha256:" + "a" * 64
    second_digest = "sha256:" + "b" * 64
    template_digest = "sha256:" + "c" * 64

    def stack(name: str, context_digest: str) -> StackResource:
        uid = f"d1-{name}"
        projection = StackProjection.build(
            stack_uid=uid,
            template_uid="d1-template",
            template_content_digest=template_digest,
            context_digest=context_digest,
            units={},
        )
        return StackResource(
            controller.GVK(controller.CORE_API_VERSION, "Stack"),
            ResourceMetadata(name=name, uid=uid),
            DesiredStackSpec(
                templateRef=StackTemplateReference(
                    name="preview",
                    uid="d1-template",
                    contentDigest=template_digest,
                ),
                parameters=JsonObjectValue({}),
                structuralProjection=projection,
            ),
        )

    policy_resources = {
        ("gitopsctr.io/v1", "Stack", "web"): stack("web", first_digest),
        ("gitopsctr.io/v1", "Stack", "worker"): stack("worker", second_digest),
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
    assert not controller.unit_document_path(root, "web/c").exists()
    assert controller.stack_dependency_edges(resources)["web/b"] == ()


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
        sourceContext=first_template.spec.sourceContext,
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
        unit = controller.load_desired_unit(controller.unit_document_path(root, f"{name}/app"), f"{name}/app")
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

    (source / "partition-removal.txt").write_text("advance the authoritative source snapshot\n")
    second_revision = commit(source, "remove partitioned Stack at a later source revision")
    second_published = _apply(source, second_revision, template, partition="application")
    assert second_published is not None
    _root, resources = desired_stack_resources(store, second_published, tmp_path / "template-only")
    desired_template = resources[("gitopsctr.io/v1", "StackTemplate", "preview")]
    desired_stack = resources[("gitopsctr.io/v1", "Stack", "web")]
    assert controller.resource_deletion(desired_template) is None
    assert controller.resource_deletion(desired_stack) is not None
    owned = resources[("unit.gitopsctr.io/v1", "Terraform", "web/app")]
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
            (first / "units", "web/app"),
            (first / "units", "worker/app"),
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
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web/app"), "web/app")
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
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web/app"), "web/app")
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
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web/app"), "web/app")
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
    unit_path = controller.unit_document_path(desired, "web/app")
    document = controller.RESOURCE_CATALOG.load_document(unit_path)
    document["spec"]["terraform"]["variables"] = {"description": "tampered"}  # type: ignore[index]
    unit_path.write_text(json.dumps(document))

    with pytest.raises(OperationError, match="does not authenticate Unit 'app'"):
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
    assert not controller.unit_document_path(root, "web/app").exists()
    assert "web/app" in controller.load_desired_transition_blocks(root)


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
    generated = controller.load_desired_unit(controller.unit_document_path(root, "web/app"), "web/app")
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
