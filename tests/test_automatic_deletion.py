"""Acceptance coverage for controller-owned deletion progression."""

import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredOwnerReference
from gitopsctr.driver import TeardownResult
from gitopsctr.errors import OperationError
from tests.conftest import write_test_document
from tests.stack_deletion_support import stack_tree
from tests.test_finalization import _mark, _terraform_unit
from tests.test_rollback import _materialized_desired_unit


class _DeletionHarness:
    """Small mutable desired/observed ref double for public converge tests."""

    def __init__(self, tmp_path: Path, initial: Path):
        self.desired = tmp_path / "desired"
        self.observed = tmp_path / "observed"
        shutil.copytree(initial, self.desired)
        self.observed.mkdir()
        self.desired_revision = "c" * 40
        self.observed_revision: str | None = None
        self.desired_publications: list[Path] = []
        self.observed_publications: list[Path] = []
        self.reconcile_outputs: list[bool] = []

    def install(self, monkeypatch: pytest.MonkeyPatch, *, change_gate: str = "none") -> None:
        class EmptyPinStore:
            def list_controller_pins(self):
                return ()

            def release_controller_pin(self, *_args):
                return True

        def observed_tree(ref: str, output: Path):
            source = self.desired if ref == "deploy/dev" else self.observed
            shutil.copytree(source, output)
            return self.desired_revision if ref == "deploy/dev" else self.observed_revision

        def materialize(revision: str, output: Path):
            if revision == self.desired_revision:
                shutil.copytree(self.desired, output)
            else:
                output.mkdir(parents=True)

        def publish_observed(_ref: str, directory: Path, _parent: str | None, _message: str, **_kwargs: object):
            snapshot = self.observed.parent / f"observed-{len(self.observed_publications)}"
            shutil.copytree(directory, snapshot)
            self.observed_publications.append(snapshot)
            shutil.rmtree(self.observed)
            shutil.copytree(directory, self.observed)
            self.observed_revision = f"e{len(self.observed_publications):039x}"
            return self.observed_revision

        def publish_desired(_environment: str, candidate: Path, *_args: object, **_kwargs: object):
            snapshot = self.desired.parent / f"desired-{len(self.desired_publications)}"
            shutil.copytree(candidate, snapshot)
            self.desired_publications.append(snapshot)
            if change_gate == "pullRequest":
                return f"g{len(self.desired_publications):039x}", object()
            shutil.rmtree(self.desired)
            shutil.copytree(candidate, self.desired)
            self.desired_revision = f"d{len(self.desired_publications):039x}"
            return self.desired_revision, None

        monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
        monkeypatch.setattr(controller, "fetch_ref", lambda ref: self.desired_revision if ref == "deploy/dev" else None)
        monkeypatch.setattr(controller, "resolve_ref", lambda _ref, revision=None: revision or self.desired_revision)
        monkeypatch.setattr(controller, "observed_tree", observed_tree)
        monkeypatch.setattr(controller, "materialize_revision", materialize)
        monkeypatch.setattr(controller, "publish_tree", publish_observed)
        monkeypatch.setattr(controller, "publish_desired_change", publish_desired)
        monkeypatch.setattr(controller, "write_change_outputs", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(controller, "write_reconcile_outputs", self.reconcile_outputs.append)
        monkeypatch.setattr(controller, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")
        monkeypatch.setattr(controller, "change_gate", lambda *_args, **_kwargs: change_gate)
        monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(controller, "progress_durable_stack_projection", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(controller, "state_store", EmptyPinStore)

    def converge_args(self, **overrides: object) -> Namespace:
        values = {
            "environment": "dev",
            "source_revision": None,
            "unit": None,
            "partition": None,
            "files": [],
            "desired_ref": "deploy/dev",
            "observed_ref": "observed/dev",
            "candidate_ref": None,
            "max_steps": 20,
            "fail_on_repeat": False,
            "yes": True,
            "verbose": False,
        }
        values.update(overrides)
        return Namespace(**values)


def _unit_tree(tmp_path: Path, *documents: dict[str, object]) -> Path:
    root = tmp_path / "initial"
    (root / "units").mkdir(parents=True)
    for document in documents:
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        name = metadata["name"]
        assert isinstance(name, str)
        write_test_document(root / "units" / f"{name}.yaml", document)
    return root


def test_converge_writes_one_true_output_when_projection_progress_is_followed_by_clean_state(tmp_path, monkeypatch):
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path))
    harness.install(monkeypatch)
    projections = iter(["d" * 40, None])
    monkeypatch.setattr(controller, "progress_durable_stack_projection", lambda *_args: next(projections))

    controller.command_converge(harness.converge_args())

    assert harness.reconcile_outputs == [True]


def test_converge_tears_down_deleting_dependency_closure_consumer_before_producer(tmp_path, monkeypatch):
    producer = _mark(_terraform_unit("producer", "d1-producer"), "producer")
    consumer = _terraform_unit("consumer", "d1-consumer")
    consumer["spec"]["resolvedInputs"] = {"receipts": {"producer": "receipt-producer"}}  # type: ignore[index]
    consumer = _mark(consumer, "consumer")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, producer, consumer))
    harness.install(monkeypatch)
    calls: list[str] = []

    def teardown(_driver, context):
        calls.append(context.unit_name)
        return TeardownResult(details={"unit": context.unit_name})

    monkeypatch.setattr(type(controller.UNIT_DRIVERS["terraform"]), "teardown", teardown)

    controller.command_converge(harness.converge_args(unit=["producer"]))

    assert calls == ["consumer", "producer"]
    assert not (harness.desired / "units/consumer.yaml").exists()
    assert not (harness.desired / "units/producer.yaml").exists()


def test_converge_tears_down_owned_unit_parent_after_child(tmp_path, monkeypatch):
    parent = _mark(_terraform_unit("parent", "d1-parent"), "parent")
    child = _mark(
        _terraform_unit(
            "child",
            "d1-child",
            owner=DesiredOwnerReference(
                apiVersion=controller.UNIT_API_VERSION,
                kind="Terraform",
                name="parent",
                uid="d1-parent",
            ),
        ),
        "child",
    )
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, parent, child))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    controller.command_converge(harness.converge_args(unit=["parent"]))

    assert calls == ["child", "parent"]


def test_converge_partition_includes_deleting_units_in_scope(tmp_path, monkeypatch):
    document = _mark(_terraform_unit("application", "d1-application"))
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, document))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    controller.command_converge(harness.converge_args(partition="application"))

    assert calls == ["application"]


def test_converge_finalization_removes_a_completed_unit_materialization(tmp_path, monkeypatch):
    document = _materialized_desired_unit("application", "a" * 40, "1" * 64)
    initial = _unit_tree(tmp_path, document)
    materialized = initial / "materialized/application"
    materialized.mkdir(parents=True)
    (materialized / "manifest.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
    document["spec"]["materialization"] = {  # type: ignore[index]
        "path": "materialized/application",
        "digest": controller.materialization_tree_digest(initial / "materialized/application"),
        "mediaType": "application/vnd.gitopsctr.kubernetes-manifests.v1",
        "metadata": {"renderer": "plain", "inventory": []},
    }
    marked = _mark(document)
    write_test_document(initial / "units/application.yaml", marked)
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["kubernetes-manifests"]),
        "teardown",
        lambda _driver, _context: TeardownResult(details={"destroyed": True}),
    )

    controller.command_converge(harness.converge_args())

    assert not (harness.desired / "units/application.yaml").exists()
    assert not (harness.desired / "materialized/application").exists()
    assert harness.reconcile_outputs == [True]


def test_converge_stack_deletion_is_child_first_and_writes_one_progressive_cleanup_candidate(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    stack_uid, child_name = stack_tree(initial)
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(harness.desired / "stacks/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(harness.desired / "stack-templates/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    for resource, directory, filename in (
        (stack, "stacks", "preview.json"),
        (template, "stack-templates", "preview.json"),
    ):
        marked = controller.mark_resource_for_deletion(resource)
        write_test_document(
            harness.desired / directory / filename,
            controller.RESOURCE_CATALOG.serialize_stack_resource(marked, profile="desired"),
        )
    child = controller.load_desired_unit(harness.desired / f"units/{child_name}.json", child_name)
    write_test_document(
        harness.desired / f"units/{child_name}.json",
        controller.serialize_unit_document(controller.mark_resource_for_deletion(child), profile="desired"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    controller.command_converge(harness.converge_args(unit=[child_name]))

    assert calls == [child_name]
    assert not (harness.desired / "units" / f"{child_name}.json").exists()
    assert not (harness.desired / "stacks/preview.json").exists()
    assert not (harness.desired / "stack-templates/preview.json").exists()
    tombstones = controller.load_resource_incarnation_evidence(harness.desired)
    assert {(tombstone.api_version, tombstone.kind, tombstone.name, tombstone.uid) for tombstone in tombstones} == {
        (controller.CORE_API_VERSION, "Stack", "preview", "d1-stack-preview"),
        (controller.CORE_API_VERSION, "StackTemplate", "preview", "d1-template"),
        (controller.UNIT_API_VERSION, "Terraform", child_name, "d1-preview-app"),
    }
    assert len(harness.desired_publications) == 1
    assert harness.reconcile_outputs == [True]
    cleanup = harness.desired_publications[0]
    assert not (cleanup / "units" / f"{child_name}.json").exists()
    assert not (cleanup / "stacks/preview.json").exists()
    assert not (cleanup / "stack-templates/preview.json").exists()


def test_converge_retries_template_pin_cleanup_after_desired_removal(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    _stack_uid, child_name = stack_tree(initial)
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)
    for path, loader in (
        (
            harness.desired / "stacks/preview.json",
            lambda path: controller.RESOURCE_CATALOG.parse_stack(
                controller.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name="preview"
            ),
        ),
        (
            harness.desired / "stack-templates/preview.json",
            lambda path: controller.RESOURCE_CATALOG.parse_stack_template(
                controller.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name="preview"
            ),
        ),
    ):
        write_test_document(
            path,
            controller.RESOURCE_CATALOG.serialize_stack_resource(
                controller.mark_resource_for_deletion(loader(path)), profile="desired"
            ),
        )
    child_path = harness.desired / f"units/{child_name}.json"
    child = controller.load_desired_unit(child_path, child_name)
    write_test_document(
        child_path,
        controller.serialize_unit_document(controller.mark_resource_for_deletion(child), profile="desired"),
    )
    revision = "a" * 40
    pin_name = f"stack-templates/dev/preview/d1-template/{revision}"
    pin = controller.ControllerPin(pin_name, f"refs/heads/gitopsctr/pins/{pin_name}", revision)
    attempts = 0
    pins = [pin]

    class FlakyPinStore:
        def list_controller_pins(self):
            return tuple(pins)

        def release_controller_pin(self, name, pin_revision):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary pin-store failure")
            pins[:] = [item for item in pins if (item.name, item.revision) != (name, pin_revision)]
            return True

    monkeypatch.setattr(controller, "state_store", FlakyPinStore)
    teardown_calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: teardown_calls.append(context.unit_name) or TeardownResult(),
    )

    with pytest.raises(RuntimeError, match="temporary pin-store failure"):
        controller.command_converge(harness.converge_args(unit=[child_name]))
    assert not (harness.desired / "stack-templates/preview.json").exists()

    controller.command_converge(harness.converge_args(unit=[child_name]))

    assert attempts == 2
    assert pins == []
    assert teardown_calls == [child_name]
    assert harness.reconcile_outputs == [True]


def test_converge_finalizes_a_standalone_deleting_stacktemplate(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    stack_tree(initial)
    (initial / "stacks/preview.json").unlink()
    (initial / "units/preview--preview-app.json").unlink()
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(initial / "stack-templates/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    write_test_document(
        initial / "stack-templates/preview.json",
        controller.RESOURCE_CATALOG.serialize_stack_resource(
            controller.mark_resource_for_deletion(template), profile="desired"
        ),
    )
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)

    class Store:
        def list_controller_pins(self):
            return ()

        def release_controller_pin(self, *_args):
            return True

    monkeypatch.setattr(controller, "state_store", Store)

    controller.command_converge(harness.converge_args())

    assert len(harness.desired_publications) == 1
    assert not (harness.desired / "stack-templates/preview.json").exists()
    assert controller.load_resource_incarnation_evidence(harness.desired) == (
        controller.ResourceIncarnationTombstone(
            api_version=controller.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid="d1-template",
            deletion_generation=1,
            partition="preview",
        ),
    )


def test_converge_missing_stack_child_without_tombstone_fails_closed(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    _stack_uid, child_name = stack_tree(initial)
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)
    child = controller.load_desired_unit(harness.desired / f"units/{child_name}.json", child_name)
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(harness.desired / "stacks/preview.json"),
        profile="desired",
        expected_name="preview",
    )
    write_test_document(
        harness.desired / "stacks/preview.json",
        controller.RESOURCE_CATALOG.serialize_stack_resource(
            controller.mark_resource_for_deletion(stack), profile="desired"
        ),
    )
    (harness.desired / f"units/{child_name}.json").unlink()
    controller.write_resource_incarnation_tombstone(
        harness.desired,
        controller.ResourceIncarnationTombstone(
            api_version=child.gvk.api_version,
            kind=child.gvk.kind,
            name=child.name,
            uid="d1-old-preview-app",
            deletion_generation=1,
        ),
    )

    with pytest.raises(OperationError):
        controller.command_converge(harness.converge_args())

    assert (harness.desired / "stacks/preview.json").exists()
    assert not harness.desired_publications
    assert child.metadata.uid is not None


def test_deleting_stack_source_retention_accepts_exact_finalized_child_tombstone(tmp_path):
    initial = tmp_path / "initial"
    _stack_uid, child_name = stack_tree(initial)
    child = controller.load_desired_unit(initial / f"units/{child_name}.json", child_name)
    stack_path = initial / "stacks/preview.json"
    stack = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(stack_path),
        profile="desired",
        expected_name="preview",
    )
    write_test_document(
        stack_path,
        controller.RESOURCE_CATALOG.serialize_stack_resource(
            controller.mark_resource_for_deletion(stack), profile="desired"
        ),
    )
    (initial / f"units/{child_name}.json").unlink()
    assert child.metadata.uid is not None
    controller.write_resource_incarnation_tombstone(
        initial,
        controller.ResourceIncarnationTombstone(
            api_version=child.gvk.api_version,
            kind=child.gvk.kind,
            name=child.name,
            uid=child.metadata.uid,
            deletion_generation=1,
        ),
    )

    assert controller._required_stack_template_source_pins("dev", initial) == ()


def test_gated_deletion_candidate_cannot_teardown_before_live_ref(tmp_path, monkeypatch):
    active = _terraform_unit("application", "d1-application")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, active))
    harness.install(monkeypatch, change_gate="pullRequest")
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    delete_args = Namespace(
        environment="dev",
        kind="Unit",
        name="application",
        uid="d1-application",
        desired_ref="deploy/dev",
        observed_ref="observed/dev",
        candidate_ref="candidate/dev",
        dry=False,
    )
    controller.command_delete_resource(delete_args)
    assert not controller.resource_deletion(
        controller.load_desired_unit(harness.desired / "units/application.yaml", "application")
    )

    monkeypatch.setattr(controller, "reconciliation_statuses", lambda *_args: [("application", "CLEAN", "")])
    monkeypatch.setattr(controller, "command_reconcile", lambda _args: False)
    controller.command_converge(harness.converge_args())
    assert calls == []


def test_converge_deletion_respects_max_steps(tmp_path, monkeypatch):
    a = _mark(_terraform_unit("a", "d1-a"), "a")
    b = _mark(_terraform_unit("b", "d1-b"), "b")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, a, b))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    with pytest.raises(OperationError, match="within 1 reconciliation steps"):
        controller.command_converge(harness.converge_args(max_steps=1))

    assert calls == ["a"]
    assert (harness.desired / "units/b.yaml").exists()


def test_gated_cleanup_attempts_count_toward_max_steps(tmp_path, monkeypatch):
    a = _mark(_terraform_unit("a", "d1-a"), "a")
    b = _mark(_terraform_unit("b", "d1-b"), "b")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, a, b))
    harness.install(monkeypatch, change_gate="pullRequest")
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    with pytest.raises(OperationError, match="within 1 reconciliation steps"):
        controller.command_converge(harness.converge_args(max_steps=1))

    assert calls == ["a"]


def test_converge_tracks_multiple_selected_deletions_after_each_name_disappears(tmp_path, monkeypatch):
    a = _mark(_terraform_unit("a", "d1-a"), "a")
    b = _mark(_terraform_unit("b", "d1-b"), "b")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, a, b))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    controller.command_converge(harness.converge_args(unit=["a", "b"]))

    assert calls == ["a", "b"]
    assert not (harness.desired / "units/a.yaml").exists()
    assert not (harness.desired / "units/b.yaml").exists()


def test_selected_active_unit_ignores_unrelated_deletion(tmp_path, monkeypatch):
    a = _terraform_unit("a", "d1-a")
    b = _mark(_terraform_unit("b", "d1-b"), "b")
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, a, b))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        controller,
        "reconciliation_statuses",
        lambda *_args: [("a", "CLEAN", ""), ("b", "WAIT", "deleting")],
    )
    monkeypatch.setattr(controller, "command_reconcile", lambda args: calls.append(args.unit) or False)

    controller.command_converge(harness.converge_args(unit=["a"]))

    assert calls == []
    assert (harness.desired / "units/b.yaml").exists()


def test_partition_deletion_waits_for_dependent_in_another_partition(tmp_path, monkeypatch):
    producer = _mark(_terraform_unit("producer", "d1-producer"), "producer")
    consumer = _terraform_unit("consumer", "d1-consumer")
    consumer["spec"]["resolvedInputs"] = {"receipts": {"producer": "receipt-producer"}}  # type: ignore[index]
    consumer = _mark(consumer, "consumer")
    producer["metadata"]["labels"] = {"gitopsctr.io/partition": "p"}  # type: ignore[index]
    consumer["metadata"]["labels"] = {"gitopsctr.io/partition": "q"}  # type: ignore[index]
    harness = _DeletionHarness(tmp_path, _unit_tree(tmp_path, producer, consumer))
    harness.install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, context: calls.append(context.unit_name) or TeardownResult(),
    )

    controller.command_converge(harness.converge_args(partition="p"))

    assert calls == []
    assert (harness.desired / "units/producer.yaml").exists()
    assert (harness.desired / "units/consumer.yaml").exists()


def test_partition_deletion_does_not_cascade_to_template_in_another_partition(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    _stack_uid, child_name = stack_tree(initial)
    for relative, partition in (("stacks/preview.json", "p"), ("stack-templates/preview.json", "q")):
        path = initial / relative
        document = controller.RESOURCE_CATALOG.load_document(path)
        document["metadata"]["labels"] = {"gitopsctr.io/partition": partition}  # type: ignore[index]
        if relative.startswith("stacks/"):
            resource = controller.RESOURCE_CATALOG.parse_stack(document, profile="desired", expected_name="preview")
        else:
            resource = controller.RESOURCE_CATALOG.parse_stack_template(
                document, profile="desired", expected_name="preview"
            )
        write_test_document(
            path,
            controller.RESOURCE_CATALOG.serialize_stack_resource(
                controller.mark_resource_for_deletion(resource), profile="desired"
            ),
        )
    child_path = initial / f"units/{child_name}.json"
    child = controller.load_desired_unit(child_path, child_name)
    write_test_document(
        child_path,
        controller.serialize_unit_document(controller.mark_resource_for_deletion(child), profile="desired"),
    )
    harness = _DeletionHarness(tmp_path, initial)
    harness.install(monkeypatch)
    monkeypatch.setattr(
        type(controller.UNIT_DRIVERS["terraform"]),
        "teardown",
        lambda _driver, _context: TeardownResult(),
    )

    controller.command_converge(harness.converge_args(partition="p"))

    assert not (harness.desired / "units" / f"{child_name}.json").exists()
    assert not (harness.desired / "stacks/preview.json").exists()
    assert (harness.desired / "stack-templates/preview.json").exists()


def test_finalized_cleanup_rejects_unaccepted_desired_ref(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    tombstone = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="Terraform",
        name="application",
        uid="d1-application",
        deletion_generation=1,
        effect_lease_ref="gitopsctr/leases",
    )
    controller.write_resource_incarnation_tombstone(desired, tombstone)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        controller,
        "_release_finalized_unit_lease",
        lambda *_args, **_kwargs: pytest.fail("unaccepted cleanup must remain inert"),
    )

    with pytest.raises(OperationError, match="live desired ref"):
        controller._retry_finalized_cleanup(
            tombstone,
            environment="dev",
            desired_ref="candidate/dev",
            current_revision="c" * 40,
            desired_root=desired,
        )


def test_finalized_cleanup_rejects_historical_revision_of_live_ref(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    tombstone = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="Terraform",
        name="application",
        uid="d1-application",
        deletion_generation=1,
        effect_lease_ref=None,
    )
    controller.write_resource_incarnation_tombstone(desired, tombstone)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "d" * 40)

    with pytest.raises(OperationError, match="accepted desired head"):
        controller._retry_finalized_cleanup(
            tombstone,
            environment="dev",
            desired_ref="deploy/dev",
            current_revision="c" * 40,
            desired_root=desired,
        )


def test_resource_incarnation_tombstone_requires_effect_lease_ref():
    document = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="Terraform",
        name="application",
        uid="d1-application",
        deletion_generation=1,
    ).document()
    del document["resource"]["effectLeaseRef"]  # type: ignore[index]

    with pytest.raises(ValueError, match="invalid resource incarnation identity"):
        controller.ResourceIncarnationTombstone.from_document(document)


def test_same_name_recreation_cleanup_retries_against_tombstone_lease_store(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    write_test_document(desired / "units/application.yaml", _terraform_unit("application", "d2-application"))
    tombstone = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="Terraform",
        name="application",
        uid="d1-application",
        deletion_generation=1,
        effect_lease_ref="gitopsctr/historical-leases",
    )
    controller.write_resource_incarnation_tombstone(desired, tombstone)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        controller,
        "effect_lease_ref",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not synthesize a current-config lease ref"),
    )
    lease_root = tmp_path / "historical-leases"
    lease_root.mkdir()
    controller.write_effect_lease(
        lease_root,
        controller.EffectLease(
            unit_name="application",
            uid="d1-application",
            token="lease-old",
            owner="test",
            desired_revision="c" * 40,
        ),
    )
    monkeypatch.setattr(controller, "_effect_lease_store_root", lambda *_args, **_kwargs: (lease_root, "e" * 40))
    calls: list[str | None] = []
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda _ref, _name, _token, _uid, **kwargs: calls.append(kwargs.get("lease_ref")),
    )

    assert (
        controller._retry_finalized_cleanup(
            tombstone,
            environment="dev",
            desired_ref="deploy/dev",
            current_revision="c" * 40,
            desired_root=desired,
        )
        is True
    )
    assert calls == ["gitopsctr/historical-leases"]


def test_finalized_cleanup_fails_closed_on_ambiguous_driver_tombstones(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    desired.mkdir()
    for kind, lease_ref in (("Terraform", "gitopsctr/terraform-leases"), ("OciImages", "gitopsctr/oci-leases")):
        controller.write_resource_incarnation_tombstone(
            desired,
            controller.ResourceIncarnationTombstone(
                api_version=controller.UNIT_API_VERSION,
                kind=kind,
                name="application",
                uid="d1-application",
                deletion_generation=1,
                effect_lease_ref=lease_ref,
            ),
        )
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(
        controller,
        "release_effect_lease",
        lambda *_args, **_kwargs: pytest.fail("ambiguous tombstones must not release a lease"),
    )

    assert (
        controller._retry_finalized_cleanup(
            controller.ResourceIncarnationTombstone(
                api_version=controller.UNIT_API_VERSION,
                kind="Terraform",
                name="application",
                uid="d1-application",
                deletion_generation=1,
                effect_lease_ref="gitopsctr/terraform-leases",
            ),
            environment="dev",
            desired_ref="deploy/dev",
            current_revision="c" * 40,
            desired_root=desired,
        )
        is False
    )
