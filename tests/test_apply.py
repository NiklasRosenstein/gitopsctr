from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore
from tests.stack_support import cloned_project_repository as _repository
from tests.stack_support import commit, git


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


def _authored_source_less_unit(path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "FrontendS3Cloudfront",
                "metadata": {"name": name},
                "spec": {
                    "inputs": {
                        "bundle": "registry.example/frontend@sha256:" + "0" * 64,
                        "bucket": "example-frontend",
                        "distributionId": "EXAMPLE123",
                        "url": "https://www.example.invalid",
                        "runtimeConfig": {
                            "schema": 1,
                            "apiBase": "https://api.example.invalid",
                            "auth": {
                                "mode": "cognito",
                                "issuer": "https://issuer.example.invalid",
                                "clientId": "example-client",
                            },
                        },
                    },
                    "pull": {"credentialProvider": {"type": "aws-ecr"}},
                },
            },
            sort_keys=False,
        )
    )
    return path


def _apply(
    source: Path,
    revision: str,
    *files: Path,
    partition: str | None = None,
    candidate_ref: str | None = None,
):
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
    if candidate_ref is not None:
        arguments.extend(("--candidate-ref", candidate_ref))
    for path in files:
        arguments.extend(("-f", str(path)))
    return controller.command_apply(controller.build_parser().parse_args(arguments))


def _apply_worktree(*files: Path, partition: str | None = None):
    arguments = [
        "apply",
        "--environment",
        "dev",
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


def test_single_stdin_document_hashes_the_exact_supplied_bytes(monkeypatch: pytest.MonkeyPatch):
    raw = "# preserved comment\n---\napiVersion: unit.gitopsctr.io/v1\nkind: Terraform\nmetadata:\n  name: app\nspec:\n  source:\n    path: .\n  \n"
    monkeypatch.setattr(controller.sys, "stdin", io.StringIO(raw))

    documents = controller._load_apply_documents(["-"])

    assert len(documents) == 1
    assert documents[0].origin == "stdin#1"
    assert documents[0].document_digest == f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def test_multi_document_stdin_uses_contiguous_parser_segments(monkeypatch: pytest.MonkeyPatch):
    raw = (
        "# first\n---\napiVersion: unit.gitopsctr.io/v1\nkind: Terraform\nmetadata:\n  name: first\n"
        "---\n# second\napiVersion: unit.gitopsctr.io/v1\nkind: Terraform\nmetadata:\n  name: second\n"
    )
    monkeypatch.setattr(controller.sys, "stdin", io.StringIO(raw))

    documents = controller._load_apply_documents(["-"])
    nodes = list(yaml.compose_all(raw))
    first_segment = raw[: nodes[1].start_mark.index].encode()
    second_segment = raw[nodes[1].start_mark.index :].encode()

    assert [item.origin for item in documents] == ["stdin#1", "stdin#2"]
    assert documents[0].document_digest == f"sha256:{hashlib.sha256(first_segment).hexdigest()}"
    assert documents[1].document_digest == f"sha256:{hashlib.sha256(second_segment).hexdigest()}"


def test_apply_resolves_authored_unit_and_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "add application")

    first = _apply(source, revision, authored, partition="application")
    assert first is not None
    document = _desired(store, tmp_path / "first")
    assert document["metadata"]["labels"] == {"gitopsctr.io/partition": "application"}  # type: ignore[index]
    assert document["spec"]["source"]["revision"] == revision  # type: ignore[index]

    second = _apply(source, revision, authored, partition="application")
    assert second == first


@pytest.mark.parametrize("candidate_ref", ["observed/dev", "refs/heads/observed/dev"])
def test_apply_rejects_candidate_ref_matching_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_ref: str
):
    source, _store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "add application")

    with pytest.raises(OperationError, match="conflicts with deployment state"):
        _apply(source, revision, authored, partition="application", candidate_ref=candidate_ref)


def test_live_source_less_inputs_support_repository_relative_cwd_and_outside_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    authored = _authored_source_less_unit(source / "frontend.yaml", "frontend")

    published = _apply_worktree(authored)
    assert published is not None
    desired = tmp_path / "source-less"
    store.materialize(published, desired)
    unit = controller.load_desired_unit(controller.unit_document_path(desired, "frontend"), "frontend")
    assert getattr(unit.spec, "source", None) is None

    caller = tmp_path / "caller"
    authored = _authored_source_less_unit(caller / "inputs/relative.yaml", "relative")
    monkeypatch.chdir(caller)
    published = _apply_worktree(Path("inputs/relative.yaml"))
    assert published is not None
    desired = tmp_path / "cwd-relative"
    store.materialize(published, desired)
    unit = controller.load_desired_unit(controller.unit_document_path(desired, "relative"), "relative")
    assert getattr(unit.spec, "source", None) is None
    assert authored.is_file()

    authored = _authored_source_less_unit(tmp_path / "outside/external.yaml", "external")
    published = _apply_worktree(authored)
    assert published is not None
    desired = tmp_path / "outside-desired"
    store.materialize(published, desired)
    assert controller.unit_document_path(desired, "external").is_file()


def test_apply_with_source_revision_reads_snapshot_instead_of_dirty_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "add application")
    authored.write_text(authored.read_text().replace("name: application", "name: dirty"))

    published = _apply(source, revision, authored)

    assert published is not None
    desired = _desired(store, tmp_path / "revision-snapshot")
    assert desired["metadata"]["name"] == "application"  # type: ignore[index]


def test_apply_with_source_revision_uses_snapshot_for_a_dirty_symlink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    input_path = source / "inputs/application.yaml"
    authored = _authored_unit(input_path, "application")
    revision = commit(source, "add application input")
    dirty = _authored_unit(tmp_path / "dirty.yaml", "dirty")
    input_path.unlink()
    input_path.symlink_to(dirty)
    monkeypatch.chdir(source)

    published = _apply(source, revision, Path("inputs/application.yaml"))

    assert published is not None
    desired = _desired(store, tmp_path / "symlink-snapshot")
    assert desired["metadata"]["name"] == "application"  # type: ignore[index]
    assert authored.is_symlink()


def test_apply_symlink_loop_is_reported_as_operation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, _revision = _repository(tmp_path, monkeypatch)
    loop = source / "loop.yaml"
    loop.symlink_to(loop.name)

    with pytest.raises(OperationError, match="invalid or looping symbolic link"):
        _apply_worktree(loop)


def test_apply_snapshot_symlink_loop_is_reported_as_operation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, _revision = _repository(tmp_path, monkeypatch)
    loop = source / "loop.yaml"
    loop.symlink_to(loop.name)
    revision = commit(source, "add looping input")

    with pytest.raises(OperationError, match="invalid or looping symbolic link"):
        _apply(source, revision, loop)


def test_revision_apply_rejects_stdin_and_outside_input_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, _store, revision = _repository(tmp_path, monkeypatch)
    outside = _authored_source_less_unit(tmp_path / "outside/frontend.yaml", "frontend")
    materialized = False

    def fail_materialization(_revision: str, _target: Path) -> None:
        nonlocal materialized
        materialized = True

    monkeypatch.setattr(controller, "materialize_revision", fail_materialization)
    with pytest.raises(OperationError, match="standard input"):
        _apply(source, revision, Path("-"))
    with pytest.raises(OperationError, match="outside the project repository"):
        _apply(source, revision, outside)
    assert not materialized


@pytest.mark.parametrize("input_kind", ["missing", "malformed", "empty"])
def test_live_apply_validates_input_before_materializing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_kind: str
):
    source, _store, _revision = _repository(tmp_path, monkeypatch)
    if input_kind == "missing":
        input_path = source / "missing.yaml"
    elif input_kind == "malformed":
        input_path = source / "malformed.yaml"
        input_path.write_text("metadata: [")
    else:
        input_path = source / "empty"
        input_path.mkdir()
    materialized = False

    def fail_materialization(_target: Path) -> None:
        nonlocal materialized
        materialized = True

    monkeypatch.setattr(controller, "_materialize_apply_worktree", fail_materialization)
    expected = (
        "does not exist" if input_kind == "missing" else "zero documents" if input_kind == "empty" else "malformed"
    )
    with pytest.raises(OperationError, match=expected):
        _apply_worktree(input_path)
    assert not materialized


def test_apply_with_source_revision_rejects_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, revision = _repository(tmp_path, monkeypatch)

    with pytest.raises(OperationError, match=r"--source-revision.*standard input"):
        _apply(source, revision, Path("-"))


def test_apply_with_source_revision_rejects_outside_repository_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, revision = _repository(tmp_path, monkeypatch)
    outside = _authored_source_less_unit(tmp_path / "outside/frontend.yaml", "frontend")

    with pytest.raises(OperationError, match=r"outside the project repository.*--source-revision"):
        _apply(source, revision, outside)


def test_partitioned_empty_directory_marks_omitted_members_for_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    authored = _authored_source_less_unit(tmp_path / "application.yaml", "application")
    _apply_worktree(authored, partition="application")
    empty = tmp_path / "empty"
    empty.mkdir()

    published = _apply_worktree(empty, partition="application")

    assert published is not None
    desired = tmp_path / "empty-partition"
    store.materialize(published, desired)
    omitted = controller.load_desired_unit(controller.unit_document_path(desired, "application"), "application")
    assert omitted.metadata.partition == "application"
    assert controller.resource_deletion(omitted) is not None


def test_apply_without_source_revision_rejects_repository_backed_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, _revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")

    with pytest.raises(OperationError, match=r"Unit 'application'.*--source-revision <commit>"):
        _apply_worktree(authored)


def test_missing_required_source_revision_does_not_initialize_gated_desired_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    git(source, "push", "origin", ":refs/heads/deploy/dev")
    environment = source / "deployment/environments/dev/environment.json"
    document = yaml.safe_load(environment.read_text())
    document["spec"]["changeGate"] = "pullRequest"
    environment.write_text(json.dumps(document))
    authored = _authored_unit(source / "application.yaml", "application")

    with pytest.raises(OperationError, match=r"--source-revision <commit>"):
        _apply_worktree(authored)

    assert store.fetch("deploy/dev").revision is None


def test_first_apply_initializes_an_unpublished_desired_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    git(source, "push", "origin", ":refs/heads/deploy/dev")
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "add application")

    published = _apply(source, revision, authored, partition="application")

    assert published is not None
    assert store.fetch("deploy/dev").revision == published


def test_empty_first_partition_is_a_noop_without_creating_desired_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, _revision = _repository(tmp_path, monkeypatch)
    git(source, "push", "origin", ":refs/heads/deploy/dev")
    empty = source / "empty"
    empty.mkdir()

    published = _apply_worktree(empty, partition="application")

    assert published is None
    assert store.fetch("deploy/dev").revision is None


def test_unpartitioned_apply_preserves_existing_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    revision = commit(source, "add application")
    _apply(source, revision, authored, partition="application")

    _apply(source, revision, authored)

    document = _desired(store, tmp_path / "preserved")
    assert document["metadata"]["labels"] == {"gitopsctr.io/partition": "application"}  # type: ignore[index]


def test_apply_rejects_cross_partition_transfer_and_duplicate_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, _store, revision = _repository(tmp_path, monkeypatch)
    authored = _authored_unit(source / "application.yaml", "application")
    same_physical_unit = source / "same-name-other-kind.yaml"
    same_physical_unit.write_text(authored.read_text().replace("kind: Terraform", "kind: OciImages"))
    revision = commit(source, "add duplicate inputs")
    _apply(source, revision, authored, partition="application")

    with pytest.raises(OperationError, match="belongs to partition 'application'"):
        _apply(source, revision, authored, partition="other")
    with pytest.raises(OperationError, match="duplicate apply resource"):
        _apply(source, revision, authored, authored)

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
    revision = commit(source, "add partition inputs")
    _apply(source, revision, first, second, partition="application")

    _apply(source, revision, second, partition="application")

    head = store.fetch("deploy/dev").revision
    assert head is not None
    desired = tmp_path / "pruned"
    store.materialize(head, desired)
    omitted = controller.load_desired_unit(controller.unit_document_path(desired, "first"), "first")
    assert omitted.metadata.partition == "application"
    assert controller.resource_deletion(omitted) is not None


def test_apply_carries_promotion_lineage_through_unrelated_and_noop_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, store, revision = _repository(tmp_path, monkeypatch)
    promotion = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Promotion",
        "metadata": {"name": "dev"},
        "spec": {
            "source": {
                "environment": "staging",
                "desiredRef": "deploy/staging",
                "desiredRevision": revision,
                "observedRef": "observed/staging",
                "observedRevision": revision,
            },
            "specificationRevision": revision,
        },
    }
    desired_revision = store.fetch("deploy/dev").revision
    assert desired_revision is not None
    baseline = tmp_path / "promotion-baseline"
    store.materialize(desired_revision, baseline)
    promotion_path = baseline / "promotion.yaml"
    promotion_path.write_text(yaml.safe_dump(promotion, sort_keys=False))
    promotion_bytes = promotion_path.read_bytes()
    store.publish(
        "deploy/dev", baseline, desired_revision, "seed promotion lineage", expected_publication_head=desired_revision
    )

    unrelated = _authored_source_less_unit(source / "unrelated.yaml", "unrelated")
    revision = commit(source, "add unrelated root")
    published = _apply(source, revision, unrelated)
    assert published is not None
    desired = tmp_path / "promotion-desired"
    store.materialize(published, desired)
    assert controller.document_candidates(desired, "promotion")[0].read_bytes() == promotion_bytes

    assert _apply(source, revision, unrelated) == published


def test_shared_partition_pruning_marks_omitted_promotion_roots_for_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Promotion and apply share omission-based partition pruning."""

    source, store, revision = _repository(tmp_path, monkeypatch)
    first = _authored_unit(source / "first.yaml", "first")
    second = _authored_unit(source / "second.yaml", "second")
    revision = commit(source, "add partition inputs")
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
    revision = commit(source, "add application")
    _apply(source, revision, authored)
    initial = _desired(store, tmp_path / "canonical-initial")
    initial_uid = initial["metadata"]["uid"]  # type: ignore[index]
    initial["metadata"]["uid"] = "caller-controlled"  # type: ignore[index]
    canonical = source / "canonical.yaml"
    canonical.write_text(yaml.safe_dump(initial, sort_keys=False))
    revision = commit(source, "add canonical input")

    _apply(source, revision, canonical)

    applied = _desired(store, tmp_path / "canonical-applied")
    assert applied["metadata"]["uid"] == initial_uid  # type: ignore[index]
    assert applied["spec"] == initial["spec"]


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

    monkeypatch.setattr(
        controller,
        "deployment_refs",
        lambda *_args, **_kwargs: pytest.fail("explicit converge refs must not load live configuration"),
    )
    explicit_args = controller.build_parser().parse_args(
        [
            "converge",
            "--environment",
            "dev",
            "--desired-ref",
            "deploy/dev",
            "--observed-ref",
            "observed/dev",
            "--yes",
        ]
    )
    controller.command_converge(explicit_args)

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
