"""Command-level promotion coverage for ordinary Units and direct Stack input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests.conftest import receipt_resource
from tests.stack_support import commit, git, project_repository


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True))


def _promotion_repository(root: Path) -> Path:
    environment = project_repository(root)
    _write(
        root / "deployment/environments/staging/environment.json",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Environment",
            "metadata": {"name": "staging"},
            "spec": {"promotion": {"allowedSources": ["dev"]}},
        },
    )
    _write(
        environment / "units/source.json",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "source"},
            "spec": {
                "source": {"path": "."},
                "terraform": {"variables": {"value": "source"}},
            },
        },
    )
    _write(
        root / "target-template.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "StackTemplate",
            "metadata": {"name": "application"},
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
                                        "fromPromotion": {
                                            "unit": "source",
                                            "pointer": "/terraform/variables/value",
                                        }
                                    }
                                }
                            },
                        },
                    },
                    "wait": {
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
                    },
                },
            },
        },
    )
    _write(
        root / "target-stack.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "application"},
            "spec": {"template": "application"},
        },
    )
    _write(
        root / "promoted.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "promoted"},
            "spec": {
                "source": {"path": "."},
                "terraform": {
                    "variables": {
                        "value": {
                            "fromPromotion": {
                                "unit": "source",
                                "pointer": "/terraform/variables/value",
                            }
                        }
                    }
                },
            },
        },
    )
    _write(
        root / "producer.yaml",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "producer"},
            "spec": {"source": {"path": "."}},
        },
    )
    return root


def test_persisted_promotion_contexts_materialize_under_digest_specific_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    materialized: list[Path] = []
    monkeypatch.setattr(controller, "resolve_ref", lambda _ref, revision: revision)

    def materialize(_revision: str, destination: Path) -> None:
        materialized.append(destination)
        destination.mkdir(parents=True)

    monkeypatch.setattr(controller, "materialize_revision", materialize)
    documents = []
    for index, digest in enumerate(("sha256:" + "a" * 64, "sha256:" + "b" * 64)):
        documents.append(
            (
                digest,
                controller.PromotionContext(
                    source_environment="dev",
                    desired_ref=f"desired-{index}",
                    desired_revision=f"{index + 1:040x}",
                    observed_ref=f"observed-{index}",
                    observed_revision=f"{index + 3:040x}",
                    specification_revision=f"{index + 5:040x}",
                    desired_root=tmp_path,
                ).document(),
            )
        )

    contexts = [
        controller._promotion_context_from_document(
            controller.RESOURCE_CATALOG.serialize_promotion(document),
            tmp_path,
            digest,
        )
        for digest, document in documents
    ]

    assert contexts[0].desired_root != contexts[1].desired_root
    assert contexts[0].desired_root.parent.name == "promotion-" + "a" * 64
    assert contexts[1].desired_root.parent.name == "promotion-" + "b" * 64
    assert materialized == [
        contexts[0].desired_root,
        contexts[0].observed_root,
        contexts[1].desired_root,
        contexts[1].observed_root,
    ]


def test_command_promote_resolves_unit_and_direct_inline_stack_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _promotion_repository(tmp_path / "source")
    git(source, "init", "-b", "main")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    specification_revision = commit(source, "review target promotion inputs")
    git(source, "push", "-u", "origin", "main")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    store = controller.GitStateStore(source)

    current = tmp_path / "source-desired"
    observed = tmp_path / "source-observed"
    current.mkdir()
    observed.mkdir()
    source_candidate = tmp_path / "source-candidate"
    controller.build_desired_candidate(
        "dev",
        source,
        specification_revision,
        current,
        observed,
        None,
        source_candidate,
        verbose=False,
    )
    source_desired_revision = store.publish(
        "gitopsctr/desired/dev", source_candidate, None, "source desired", expected_publication_head=None
    ).revision
    monkeypatch.setattr(controller, "require_clean_source", lambda *_args: None)

    args = controller.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--specification-revision",
            specification_revision,
            "-f",
            "target-template.yaml",
            "-f",
            "target-stack.yaml",
            "-f",
            "promoted.yaml",
            "-f",
            "producer.yaml",
        ]
    )
    monkeypatch.chdir(source)
    controller.command_promote(args)

    target_revision = store.fetch("gitopsctr/desired/staging").revision
    assert target_revision is not None
    target = tmp_path / "target-desired"
    store.materialize(target_revision, target)
    promoted = controller.load_desired_unit(controller.unit_document_path(target, "promoted"), "promoted")
    projected = controller.load_desired_unit(
        controller.unit_document_path(target, "application--app"), "application--app"
    )
    assert promoted.spec.terraform.variables == {"value": "source"}  # type: ignore[union-attr]
    assert projected.spec.terraform.variables == {"value": "source"}  # type: ignore[union-attr]
    assert not controller.unit_document_path(target, "application--wait").exists()
    promotion_path = controller.document_candidates(target, "promotion")[0]
    promotion = controller.RESOURCE_CATALOG.load_document(promotion_path)
    assert promotion["spec"]["source"]["desiredRevision"] == source_desired_revision

    repeated_revision = store.fetch("gitopsctr/desired/staging").revision
    controller.command_promote(args)
    assert store.fetch("gitopsctr/desired/staging").revision == repeated_revision

    target_observed = tmp_path / "target-observed"
    target_observed.mkdir()
    producer_path = controller.unit_document_path(target, "producer")
    receipt = receipt_resource(
        "terraform",
        "producer",
        {"revision": target_revision, "unitBlob": controller.file_blob(producer_path)},
        result={"applied": {"sourceRevision": specification_revision}, "outputs": {"value": "evidence"}},
    )
    controller.write_document(
        target_observed / "units/producer.json",
        controller.RESOURCE_CATALOG.serialize_receipt(receipt),
        format=controller.DocumentFormat.JSON,
    )
    store.publish(
        "gitopsctr/observed/staging", target_observed, None, "publish target evidence", expected_publication_head=None
    )

    progressed = controller.progress_durable_stack_projection(
        "staging",
        "gitopsctr/desired/staging",
        "gitopsctr/observed/staging",
    )
    assert progressed is not None
    progressed_root = tmp_path / "progressed-target"
    progressed_revision = store.fetch("gitopsctr/desired/staging").revision
    assert progressed_revision is not None
    store.materialize(progressed_revision, progressed_root)
    promoted_after_progress = controller.load_desired_unit(
        controller.unit_document_path(progressed_root, "application--app"), "application--app"
    )
    waiting_after_progress = controller.load_desired_unit(
        controller.unit_document_path(progressed_root, "application--wait"), "application--wait"
    )
    assert promoted_after_progress.spec.terraform.variables == {"value": "source"}  # type: ignore[union-attr]
    assert waiting_after_progress.spec.terraform.variables == {"value": "evidence"}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("lease_environment", "candidate_ref", "configured_lease_ref"),
    [
        ("dev", "refs/heads/lease/dev", "lease/dev"),
        ("staging", "lease/staging", "refs/heads/lease/staging"),
    ],
)
def test_command_promote_rejects_candidate_ref_matching_absent_promotion_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lease_environment: str,
    candidate_ref: str,
    configured_lease_ref: str,
):
    source = _promotion_repository(tmp_path / "source")
    staging_environment = source / "deployment/environments/staging/environment.json"
    staging = json.loads(staging_environment.read_text())
    staging["spec"]["changeGate"] = "pullRequest"
    _write(staging_environment, staging)
    git(source, "init", "-b", "main")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    specification_revision = commit(source, "review target promotion lease conflict")
    git(source, "push", "-u", "origin", "main")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    store = controller.GitStateStore(source)

    current = tmp_path / "source-desired"
    observed = tmp_path / "source-observed"
    current.mkdir()
    observed.mkdir()
    source_candidate = tmp_path / "source-candidate"
    controller.build_desired_candidate(
        "dev",
        source,
        specification_revision,
        current,
        observed,
        None,
        source_candidate,
        verbose=False,
    )
    store.publish("gitopsctr/desired/dev", source_candidate, None, "source desired", expected_publication_head=None)
    monkeypatch.setattr(controller, "require_clean_source", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "effect_lease_ref",
        lambda environment, _desired_ref, _configuration_root: (
            configured_lease_ref if environment == lease_environment else None
        ),
    )
    monkeypatch.setattr(
        controller,
        "publish_change_candidate",
        lambda *_args, **_kwargs: pytest.fail("promotion candidate was published"),
    )

    args = controller.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--specification-revision",
            specification_revision,
            "--candidate-ref",
            candidate_ref,
            "-f",
            "target-template.yaml",
            "-f",
            "target-stack.yaml",
            "-f",
            "promoted.yaml",
            "-f",
            "producer.yaml",
        ]
    )

    monkeypatch.chdir(source)
    with pytest.raises(OperationError, match="conflicts with deployment state"):
        controller.command_promote(args)


@pytest.mark.parametrize("value", ["-", "outside.yaml"])
def test_command_promote_rejects_unsafe_revision_inputs_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    source = _promotion_repository(tmp_path / "source")
    git(source, "init", "-b", "main")
    specification_revision = commit(source, "review promotion input selection")
    outside = tmp_path / value
    if value != "-":
        outside.write_text("not used")
        selected = str(outside)
    else:
        selected = value
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    materialized = False

    def fail_materialization(_revision: str, _target: Path) -> None:
        nonlocal materialized
        materialized = True

    monkeypatch.setattr(controller, "materialize_revision", fail_materialization)
    args = controller.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--specification-revision",
            specification_revision,
            "-f",
            selected,
        ]
    )
    expected = "standard input" if value == "-" else "outside the project repository"
    with pytest.raises(OperationError, match=expected):
        controller.command_promote(args)
    assert not materialized


def test_command_promote_uses_the_exact_specification_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _promotion_repository(tmp_path / "source")
    input_path = source / "reviewed.yaml"
    input_path.write_text(
        "apiVersion: unit.gitopsctr.io/v1\nkind: FrontendS3Cloudfront\nmetadata:\n  name: reviewed\nspec: {}\n"
    )
    git(source, "init", "-b", "main")
    specification_revision = commit(source, "record reviewed promotion input")
    input_path.write_text(input_path.read_text().replace("reviewed", "dirty"))
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    monkeypatch.chdir(source)
    snapshot = tmp_path / "specification"
    controller.materialize_revision(specification_revision, snapshot)

    documents = controller._load_apply_documents(
        ["reviewed.yaml"],
        source_revision=specification_revision,
        source_root=snapshot,
        operation="promotion",
        revision_option="--specification-revision",
    )
    assert documents[0].document["metadata"] == {"name": "reviewed"}


def test_command_promote_rejects_zero_documents_without_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _promotion_repository(tmp_path / "source")
    empty = source / "empty"
    empty.mkdir()
    (empty / ".gitkeep").write_text("")
    git(source, "init", "-b", "main")
    specification_revision = commit(source, "record empty promotion input")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    monkeypatch.chdir(source)
    args = controller.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--specification-revision",
            specification_revision,
            "-f",
            "empty",
        ]
    )
    with pytest.raises(OperationError, match="zero documents"):
        controller.command_promote(args)


def test_command_promote_empty_first_partition_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _promotion_repository(tmp_path / "source")
    git(source, "init", "-b", "main")
    specification_revision = commit(source, "record empty partition promotion input")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    store = controller.GitStateStore(source)
    current = tmp_path / "source-current"
    observed = tmp_path / "source-observed"
    current.mkdir()
    observed.mkdir()
    candidate = tmp_path / "source-candidate"
    controller.build_desired_candidate(
        "dev",
        source,
        specification_revision,
        current,
        observed,
        None,
        candidate,
        verbose=False,
    )
    store.publish("gitopsctr/desired/dev", candidate, None, "publish source desired", expected_publication_head=None)
    monkeypatch.setattr(controller, "require_clean_source", lambda *_args: None)
    empty = source / "empty"
    empty.mkdir()
    (empty / ".gitkeep").write_text("")
    specification_revision = commit(source, "add empty partition input")
    monkeypatch.chdir(source)
    args = controller.build_parser().parse_args(
        [
            "promote",
            "--from-environment",
            "dev",
            "--to-environment",
            "staging",
            "--specification-revision",
            specification_revision,
            "--partition",
            "application",
            "-f",
            "empty",
        ]
    )
    controller.command_promote(args)
    assert store.fetch("gitopsctr/desired/staging").revision is None
