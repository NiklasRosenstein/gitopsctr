from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore
from tests.stack_support import commit, git, project_repository, write_stack_source


def _repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, GitStateStore, str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    git(tmp_path, "init", "--bare", str(remote))
    project_repository(source)
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", str(remote))
    revision = commit(source, "initialize source")
    git(source, "push", "-u", "origin", "main")
    store = GitStateStore(source)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / ".gitkeep").write_text("")
    store.publish("deploy/dev", baseline, None, "initialize desired state")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    return source, store, revision


def _authored_unit(path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "apiVersion: unit.gitopsctr.io/v1",
                "kind: Terraform",
                "metadata:",
                f"  name: {name}",
                "spec:",
                "  source:",
                "    path: .",
                "",
            )
        )
    )
    return path


def _apply(source: Path, revision: str, *files: Path, partition: str | None = None):
    arguments = [
        "apply",
        "--environment",
        "dev",
        "--source-revision",
        revision,
        "--desired-ref",
        "deploy/dev",
        "--observed-ref",
        "observed/dev",
    ]
    if partition is not None:
        arguments.extend(("--partition", partition))
    for path in files:
        arguments.extend(("-f", str(path)))
    return controller.command_apply(controller.build_parser().parse_args(arguments))


def _desired(store: GitStateStore, root: Path) -> dict[str, Any]:
    revision = store.fetch("deploy/dev").revision
    assert revision is not None
    store.materialize(revision, root)
    return controller.load_json(next((root / "units").glob("application.*")))


def test_apply_resolves_authored_unit_and_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")

    first = _apply(source, revision, authored, partition="application")
    assert first is not None
    document = _desired(store, tmp_path / "first")
    assert document["metadata"]["labels"] == {"gitopsctr.io/partition": "application"}  # type: ignore[index]
    assert document["spec"]["source"]["revision"] == revision  # type: ignore[index]

    second = _apply(source, revision, authored, partition="application")
    assert second == first


def test_first_apply_initializes_an_unpublished_desired_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    git(source, "push", "origin", ":refs/heads/deploy/dev")
    authored = _authored_unit(source / "application.yaml", "application")

    published = _apply(source, revision, authored, partition="application")

    assert published is not None
    assert store.fetch("deploy/dev").revision == published


def test_unpartitioned_apply_preserves_existing_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    _apply(source, revision, authored, partition="application")

    _apply(source, revision, authored)

    document = _desired(store, tmp_path / "preserved")
    assert document["metadata"]["labels"] == {"gitopsctr.io/partition": "application"}  # type: ignore[index]


def test_apply_rejects_cross_partition_transfer_and_duplicate_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    _apply(source, revision, authored, partition="application")

    with pytest.raises(OperationError, match="belongs to partition 'application'"):
        _apply(source, revision, authored, partition="other")
    with pytest.raises(OperationError, match="duplicate apply resource"):
        _apply(source, revision, authored, authored)

    same_physical_unit = source / "same-name-other-kind.yaml"
    same_physical_unit.write_text(authored.read_text().replace("kind: Terraform", "kind: OciImages"))
    with pytest.raises(OperationError, match="duplicate apply resource"):
        _apply(source, revision, authored, same_physical_unit)


def test_apply_source_snapshot_keeps_workload_payload_without_discovering_other_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    workload = source / "workload/main.tf"
    workload.parent.mkdir()
    workload.write_text('output "message" { value = "retained" }\n')
    implicit = _authored_unit(source / "deployment/environments/dev/units/implicit.yaml", "implicit")
    authored = _authored_unit(source / "application.yaml", "application")
    authored.write_text(authored.read_text().replace("path: .", "path: workload"))
    revision = commit(source, "add workload and implicit unit")

    _apply(source, revision, authored)

    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None
    desired = tmp_path / "payload-desired"
    store.materialize(desired_revision, desired)
    assert controller.unit_document_path(desired, "application").is_file()
    assert not controller.unit_document_path(desired, "implicit").is_file()
    document = _desired(store, tmp_path / "payload-document")
    assert document["spec"]["source"]["inputHash"].startswith("sha256:")  # type: ignore[index]
    assert implicit.is_file()


def test_apply_stack_persists_only_selected_template_and_deletes_removed_owned_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    environment = source / "deployment/environments/dev"
    write_stack_source(
        environment,
        unit_templates={
            "preview-app": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            },
            "preview-db": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "spec": {"source": {"path": "."}},
            },
        },
    )
    selected_template = source / "deployment/stack-templates/preview.json"
    unused_template = source / "deployment/stack-templates/unused.json"
    unused = yaml.safe_load(selected_template.read_text())
    unused["metadata"]["name"] = "unused"
    unused_template.write_text(json.dumps(unused))
    stack = environment / "stacks/web.json"
    revision = commit(source, "add stack catalog")

    _apply(source, revision, stack, partition="application")
    first_revision = store.fetch("deploy/dev").revision
    assert first_revision is not None
    first = tmp_path / "stack-first"
    store.materialize(first_revision, first)
    assert controller.document_candidates(first / "stack-templates", "preview")
    assert not controller.document_candidates(first / "stack-templates", "unused")

    authored_stack = yaml.safe_load(stack.read_text())
    authored_stack["spec"]["units"] = ["preview-app"]
    stack.write_text(json.dumps(authored_stack))
    next_revision = commit(source, "shrink stack projection")
    _apply(source, next_revision, stack)

    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None
    desired = tmp_path / "stack-shrunk"
    store.materialize(desired_revision, desired)
    removed = controller.load_desired_unit(controller.unit_document_path(desired, "web--preview-db"), "web--preview-db")
    assert controller.resource_owner_reference(removed) is not None
    assert controller.resource_deletion(removed) is not None


def test_first_gated_apply_initializes_target_and_publishes_child_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    git(source, "push", "origin", ":deploy/dev")
    environment = source / "deployment/environments/dev/environment.json"
    document = yaml.safe_load(environment.read_text())
    document["spec"]["changeGate"] = "pullRequest"
    environment.write_text(json.dumps(document))
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "gate development changes")

    candidate_revision = _apply(source, revision, authored)

    target_revision = store.fetch("deploy/dev").revision
    assert target_revision is not None
    assert candidate_revision is not None and candidate_revision != target_revision
    assert store.verify_gated_candidate(candidate_revision, target_revision).parent == target_revision


def test_partition_apply_prunes_omitted_roots_and_retains_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    first = _authored_unit(source / "first.yaml", "first")
    second = _authored_unit(source / "second.yaml", "second")
    _apply(source, revision, first, second, partition="application")

    _apply(source, revision, second, partition="application")

    head = store.fetch("deploy/dev").revision
    assert head is not None
    desired = tmp_path / "pruned"
    store.materialize(head, desired)
    omitted = controller.load_desired_unit(controller.unit_document_path(desired, "first"), "first")
    assert omitted.metadata.partition == "application"
    assert controller.resource_deletion(omitted) is not None


def test_shared_partition_pruning_marks_omitted_promotion_roots_for_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Promotion and apply share omission-based partition pruning."""

    source, store, revision = _repository(tmp_path, monkeypatch)
    first = _authored_unit(source / "first.yaml", "first")
    second = _authored_unit(source / "second.yaml", "second")
    _apply(source, revision, first, second, partition="application")
    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None
    current = tmp_path / "promotion-current"
    store.materialize(desired_revision, current)
    candidate = tmp_path / "promotion-candidate"
    candidate.mkdir()
    applied = frozenset({("unit.gitopsctr.io/v1", "Terraform", "second")})

    controller._copy_unrelated_desired_resources(current, candidate, applied, "application")
    controller._prune_omitted_partition_resources(current, candidate, applied, "application")

    omitted = controller.load_desired_unit(controller.unit_document_path(candidate, "first"), "first")
    assert omitted.metadata.partition == "application"
    assert controller.resource_deletion(omitted) is not None


def test_canonical_desired_input_is_preserved_but_controller_owns_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    _apply(source, revision, authored)
    initial = _desired(store, tmp_path / "canonical-initial")
    initial_uid = initial["metadata"]["uid"]  # type: ignore[index]
    initial["metadata"]["uid"] = "caller-controlled"  # type: ignore[index]
    canonical = source / "canonical.yaml"
    canonical.write_text(yaml.safe_dump(initial, sort_keys=False))

    _apply(source, revision, canonical)

    applied = _desired(store, tmp_path / "canonical-applied")
    assert applied["metadata"]["uid"] == initial_uid  # type: ignore[index]
    assert applied["spec"] == initial["spec"]


def test_removed_state_construction_commands_are_rejected():
    parser = controller.build_parser()
    for arguments in (
        ["advance-desired", "--environment", "dev"],
        ["instantiate-stack", "--environment", "dev"],
        ["update-direct-stack", "--environment", "dev"],
        ["apply", "unit", "--environment", "dev", "-f", "unit.yaml"],
        ["delete", "unit", "--in=state", "--environment", "dev", "--name", "app", "--uid", "uid-app"],
        ["create", "unit", "--in=source", "--environment", "dev", "--name", "app", "--driver", "terraform"],
        ["audit-desired-compatibility", "--all"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(arguments)


def test_promote_requires_explicit_target_input():
    parser = controller.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "promote",
                "--from-environment",
                "dev",
                "--to-environment",
                "staging",
            ]
        )
    args = parser.parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--partition",
            "application",
            "-f",
            "deployment/staging",
        ]
    )
    assert args.files == ["deployment/staging"]
    assert args.partition == "application"


def test_converge_defaults_to_all_units_and_partition_is_unit_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    observed.mkdir()
    for name, partition in (("application", "application"), ("companion", None)):
        resource = controller.RESOURCE_CATALOG.parse_unit(
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "KubernetesManifests",
                "metadata": {
                    "name": name,
                    "uid": f"uid-{name}",
                    **({"labels": {"gitopsctr.io/partition": partition}} if partition is not None else {}),
                },
                "spec": {
                    "source": {"path": ".", "revision": "a" * 40, "inputHash": "sha256:inputs"},
                    "materialize": {"type": "plain"},
                    "delivery": {"mode": "external"},
                    "materialization": {
                        "path": f"materialized/{name}",
                        "digest": "sha256:" + "0" * 64,
                        "mediaType": "application/yaml",
                        "metadata": {"renderer": "plain", "inventory": []},
                    },
                },
            },
            profile="desired",
            expected_name=name,
        )
        (desired / "materialized" / name).mkdir(parents=True)
        (desired / "materialized" / name / "manifest.yaml").write_text("apiVersion: v1\nkind: List\nitems: []\n")
        resource = resource.with_spec(
            __import__("dataclasses").replace(
                resource.spec,
                materialization=__import__("dataclasses").replace(
                    resource.spec.materialization,
                    digest=controller.materialization_tree_digest(desired / "materialized" / name),
                ),
            )
        )
        controller.write_desired_candidate_unit(desired / "units" / f"{name}.yaml", resource, tmp_path)
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(controller, "fetch_ref", lambda ref: "d" * 40 if ref == "deploy/dev" else None)
    monkeypatch.setattr(
        controller,
        "materialize_revision",
        lambda _revision, target: __import__("shutil").copytree(desired, target),
    )
    monkeypatch.setattr(controller, "observed_tree", lambda _ref, target: (target.mkdir(), None)[1])
    all_args = controller.build_parser().parse_args(["converge", "--environment", "dev", "--yes"])
    partition_args = controller.build_parser().parse_args(
        ["converge", "--environment", "dev", "--partition", "application", "--yes"]
    )

    controller.command_converge(all_args)
    controller.command_converge(partition_args)

    parser = controller.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "converge",
                "--environment",
                "dev",
                "--partition",
                "application",
                "--unit",
                "application",
            ]
        )
