"""Focused contract coverage for promoted StackTemplate source selection."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests.stack_support import commit, git, write_projected_units


@dataclass(frozen=True)
class PromotionFixture:
    source: Path
    revision_a: str
    revision_b: str
    dev_desired: Path
    promotion: controller.PromotionContext


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True))


def _template(marker: str, *, name: str = "application", deploy_depends: bool = False) -> dict[str, Any]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": name},
        "spec": {
            "parameters": [{"name": "target", "type": "string"}],
            "unitTemplates": {
                "image": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": "."}},
                },
                "deploy": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "dependsOn": ["image"] if deploy_depends else [],
                    "spec": {
                        "source": {"path": "."},
                        "terraform": {
                            "variables": {
                                "template-marker": marker,
                                "target": {"fromParameter": {"name": "target"}},
                            }
                        },
                    },
                },
            },
        },
    }


def _stack(source: object, *, target: str = "staging", units: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"template": source, "parameters": {"target": target}}
    if units is not None:
        spec["units"] = units
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": "application"},
        "spec": spec,
    }


def _desired_stack(root: Path):
    path = controller.document_candidates(root / "stacks", "application")[0]
    return controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(path), profile="desired", expected_name="application"
    )


def _marker(projection: controller.StackProjection) -> tuple[str, str]:
    variables = projection.generated_units["application--deploy"].spec.terraform.variables
    assert variables is not None
    return variables["template-marker"], variables["target"]


@pytest.fixture
def promotion_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromotionFixture:
    source = tmp_path / "source"
    _write_json(
        source / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "promoted-template-sources"},
            "spec": {"effectLease": None},
        },
    )
    for environment in ("dev", "staging"):
        _write_json(
            source / f"deployment/environments/{environment}/environment.json",
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": environment},
                "spec": {},
            },
        )
    template_path = source / "deployment/stack-templates/application.json"
    _write_json(template_path, _template("A"))
    _write_json(source / "deployment/environments/dev/stacks/application.json", _stack("application", target="dev"))
    _write_json(
        source / "deployment/environments/staging/stacks/application.json",
        _stack(
            {"name": "application", "source": {"fromPromotion": {"stack": "application"}}},
            units=["deploy"],
        ),
    )
    git(source, "init", "-b", "main")
    revision_a = commit(source, "template A")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()

    dev_desired = tmp_path / "dev-desired"
    controller.project_stack_resources(source, "dev", revision_a, dev_desired, source)

    _write_json(template_path, _template("B", deploy_depends=True))
    revision_b = commit(source, "template B")
    promotion = controller.PromotionContext(
        source_environment="dev",
        desired_ref="deploy/dev",
        desired_revision="d" * 40,
        observed_ref="observed/dev",
        observed_revision=None,
        specification_revision=revision_b,
        desired_root=dev_desired,
    )
    return PromotionFixture(source, revision_a, revision_b, dev_desired, promotion)


def test_from_promotion_loads_source_pin_but_expands_target_parameters_and_subset(
    promotion_fixture: PromotionFixture, tmp_path: Path
) -> None:
    fixture = promotion_fixture
    projection = controller.project_stack_resources(
        fixture.source,
        "staging",
        fixture.revision_b,
        tmp_path / "staging-promoted",
        fixture.source,
        promotion=fixture.promotion,
    )

    assert set(projection.generated_units) == {"application--deploy"}
    assert _marker(projection) == ("A", "staging")
    target_stack = _desired_stack(tmp_path / "staging-promoted")
    source_stack = _desired_stack(fixture.dev_desired)
    assert target_stack.spec.resolvedSource == source_stack.spec.resolvedSource
    assert target_stack.spec.resolvedSource.fromGit.commit == fixture.revision_a
    write_projected_units(tmp_path / "staging-promoted", projection, fixture.source)
    resources = controller.load_desired_resource_graph(tmp_path / "staging-promoted")
    assert controller.stack_dependency_edges(resources, include_missing=True)["application--deploy"] == ()


def test_from_resource_uses_target_specification_revision_during_promotion(
    promotion_fixture: PromotionFixture, tmp_path: Path
) -> None:
    fixture = promotion_fixture
    _write_json(
        fixture.source / "deployment/environments/staging/stacks/application.json",
        _stack("application"),
    )
    output = tmp_path / "staging-resource"
    projection = controller.project_stack_resources(
        fixture.source,
        "staging",
        fixture.revision_b,
        output,
        fixture.source,
        promotion=fixture.promotion,
    )

    assert _marker(projection) == ("B", "staging")
    assert _desired_stack(output).spec.resolvedSource.fromGit.commit == fixture.revision_b


def test_promotion_input_loads_exact_specification_snapshot(
    promotion_fixture: PromotionFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = promotion_fixture
    input_path = fixture.source / "promotion-input.yaml"
    input_path.write_text(
        "apiVersion: unit.gitopsctr.io/v1\nkind: FrontendS3Cloudfront\nmetadata:\n  name: reviewed\nspec: {}\n"
    )
    specification_revision = commit(fixture.source, "add reviewed promotion input")
    input_path.write_text(input_path.read_text().replace("reviewed", "dirty"))
    snapshot = tmp_path / "specification"
    controller.materialize_revision(specification_revision, snapshot)
    monkeypatch.chdir(fixture.source)

    documents = controller._load_apply_documents(
        ["promotion-input.yaml"],
        source_revision=specification_revision,
        source_root=snapshot,
        operation="promotion",
        revision_option="--specification-revision",
    )

    assert documents[0][1]["metadata"] == {"name": "reviewed"}


@pytest.mark.parametrize("value", ["-", "outside.yaml"])
def test_promotion_revision_rejects_stdin_and_outside_input_before_materialization(
    promotion_fixture: PromotionFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    fixture = promotion_fixture
    outside = tmp_path / value
    if value != "-":
        outside.write_text("apiVersion: unit.gitopsctr.io/v1\nkind: Terraform\nmetadata:\n  name: outside\n")
        value = str(outside)
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
            fixture.revision_b,
            "-f",
            value,
        ]
    )

    with pytest.raises(OperationError, match="standard input|outside the project repository"):
        controller.command_promote(args)
    assert not materialized


def test_promotion_rejects_zero_documents_without_partition(
    promotion_fixture: PromotionFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = promotion_fixture
    monkeypatch.chdir(fixture.source)
    empty_input = fixture.source / "empty-promotion-input"
    empty_input.mkdir()
    (empty_input / "README").write_text("not a resource")
    specification_revision = commit(fixture.source, "add empty promotion input")
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
            "empty-promotion-input",
        ]
    )

    with pytest.raises(OperationError, match="promotion produced zero documents"):
        controller.command_promote(args)


def test_empty_first_promotion_partition_is_a_noop(
    promotion_fixture: PromotionFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = promotion_fixture
    monkeypatch.chdir(fixture.source)
    empty_input = fixture.source / "empty-promotion-input"
    empty_input.mkdir()
    (empty_input / "README").write_text("not a resource")
    specification_revision = commit(fixture.source, "add empty promotion partition input")
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
            "empty-promotion-input",
        ]
    )
    actual_materialize = controller.materialize_revision

    def materialize(revision: str, target: Path) -> None:
        if revision == specification_revision:
            actual_materialize(revision, target)
        else:
            shutil.copytree(fixture.dev_desired, target)

    monkeypatch.setattr(controller, "materialize_revision", materialize)
    monkeypatch.setattr(controller, "allowed_promotion_sources", lambda *_args: {"dev"})
    monkeypatch.setattr(controller, "deployment_refs", lambda *_args: ("deploy/source", "observed/source"))
    monkeypatch.setattr(controller, "resolve_ref", lambda *_args: "d" * 40)
    monkeypatch.setattr(controller, "fetch_ref", lambda _ref: None)
    monkeypatch.setattr(controller, "require_clean_source", lambda *_args: None)
    monkeypatch.setattr(controller, "observed_tree", lambda _ref, target: (target.mkdir(), None)[1])
    monkeypatch.setattr(controller, "change_gate", lambda *_args: "none")
    monkeypatch.setattr(controller, "_copy_apply_source_base", lambda _source, destination, _environment: None)
    monkeypatch.setattr(controller, "_write_apply_authored_documents", lambda *_args: ([], []))

    def build_empty_candidate(*build_args: object, **_kwargs: object) -> None:
        candidate = build_args[6]
        assert isinstance(candidate, Path)
        for directory in (candidate / "units", candidate / "stacks", candidate / "stack-templates"):
            directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(controller, "build_desired_candidate", build_empty_candidate)
    monkeypatch.setattr(controller, "_applied_root_closure", lambda *_args: frozenset())
    monkeypatch.setattr(controller, "_copy_unrelated_desired_resources", lambda *_args: None)
    monkeypatch.setattr(controller, "_prune_omitted_partition_resources", lambda *_args: None)
    monkeypatch.setattr(controller, "effect_lease_ref", lambda *_args: None)
    monkeypatch.setattr(controller, "validate_effect_leases_preserved", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "publish_tree",
        lambda *_args: pytest.fail("empty first promotion partition must not publish"),
    )

    assert controller.command_promote(args) is None


@pytest.mark.parametrize(
    ("selection", "expected_revision", "expected_marker"), (("ref", "B", "B"), ("commit", "A", "A"))
)
def test_from_git_resolves_independently_during_promotion(
    promotion_fixture: PromotionFixture,
    tmp_path: Path,
    selection: str,
    expected_revision: str,
    expected_marker: str,
) -> None:
    fixture = promotion_fixture
    external = fixture.source / "external"
    _write_json(
        external / "gitopsctr.yaml",
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": "external"},
            "spec": {"effectLease": None},
        },
    )
    _write_json(external / "deployment/stack-templates/application.json", _template("A"))
    revision_external_a = commit(fixture.source, "external template A")
    _write_json(external / "deployment/stack-templates/application.json", _template("B"))
    revision_external_b = commit(fixture.source, "external template B")
    request = {"path": "external", selection: revision_external_a if selection == "commit" else "main"}
    _write_json(
        fixture.source / "deployment/environments/staging/stacks/application.json",
        _stack({"name": "application", "source": {"fromGit": request}}, units=["deploy"]),
    )

    output = tmp_path / f"staging-git-{selection}"
    projection = controller.project_stack_resources(
        fixture.source,
        "staging",
        revision_external_b,
        output,
        fixture.source,
        promotion=fixture.promotion,
    )
    resolved = _desired_stack(output).spec.resolvedSource.fromGit
    expected = revision_external_b if expected_revision == "B" else revision_external_a
    assert resolved.commit == expected
    assert _marker(projection) == (expected_marker, "staging")


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("no-promotion", "requires an active Promotion"),
        ("missing-stack", "is not available"),
        ("template-name", "but target Stack references"),
        ("missing-pin", "has no resolved source"),
        ("missing-commit", "is not available for pinned StackTemplate source"),
        ("missing-path", "document 'missing.json' is not available"),
        ("digest", "has digest"),
        ("document-name", "is invalid"),
        ("parameters", "parameter"),
    ),
)
def test_from_promotion_fails_closed(
    promotion_fixture: PromotionFixture,
    tmp_path: Path,
    failure: str,
    expected: str,
) -> None:
    fixture = promotion_fixture
    promotion = fixture.promotion
    source_stack_path = controller.document_candidates(fixture.dev_desired / "stacks", "application")[0]
    source_stack = controller.RESOURCE_CATALOG.load_document(source_stack_path)
    if failure == "no-promotion":
        promotion = None
    elif failure == "missing-stack":
        empty = tmp_path / "empty-source-desired"
        empty.mkdir()
        promotion = controller.PromotionContext(
            source_environment="dev",
            desired_ref="deploy/dev",
            desired_revision="d" * 40,
            observed_ref="observed/dev",
            observed_revision=None,
            specification_revision=fixture.revision_b,
            desired_root=empty,
        )
    elif failure == "template-name":
        source_stack["spec"]["template"] = "other"
        source_stack_path.write_text(json.dumps(source_stack))
    elif failure == "missing-pin":
        source_stack["spec"].pop("resolvedSource")
        source_stack_path.write_text(json.dumps(source_stack))
    elif failure == "missing-commit":
        source_stack["spec"]["resolvedSource"]["fromGit"]["commit"] = "0" * 40
        source_stack_path.write_text(json.dumps(source_stack))
    elif failure == "missing-path":
        source_stack["spec"]["resolvedSource"]["fromGit"]["resourcePath"] = "missing.json"
        source_stack_path.write_text(json.dumps(source_stack))
    elif failure == "digest":
        source_stack["spec"]["resolvedSource"]["fromGit"]["digest"] = "0" * 64
        source_stack_path.write_text(json.dumps(source_stack))
    elif failure == "document-name":
        template_path = fixture.source / "deployment/stack-templates/application.json"
        _write_json(template_path, _template("invalid", name="other"))
        invalid_revision = commit(fixture.source, "mismatched template identity")
        source_stack["spec"]["resolvedSource"]["fromGit"].update(
            {
                "commit": invalid_revision,
                "digest": hashlib.sha256(template_path.read_bytes()).hexdigest(),
            }
        )
        source_stack_path.write_text(json.dumps(source_stack))
        _write_json(template_path, _template("B", deploy_depends=True))
    elif failure == "parameters":
        target_path = fixture.source / "deployment/environments/staging/stacks/application.json"
        target = json.loads(target_path.read_text())
        target["spec"]["parameters"] = {}
        target_path.write_text(json.dumps(target))

    with pytest.raises((OperationError, ValueError), match=expected):
        controller.project_stack_resources(
            fixture.source,
            "staging",
            fixture.revision_b,
            tmp_path / f"failed-{failure}",
            fixture.source,
            promotion=promotion,
        )


def test_promotion_allows_target_only_companion_stack(promotion_fixture: PromotionFixture, tmp_path: Path) -> None:
    fixture = promotion_fixture
    template_path = fixture.source / "deployment/stack-templates/companion.json"
    _write_json(template_path, _template("target-only", name="companion"))
    stack = _stack("companion", target="staging")
    stack["metadata"]["name"] = "companion"
    _write_json(fixture.source / "deployment/environments/staging/stacks/companion.json", stack)
    (fixture.source / "deployment/environments/staging/stacks/application.json").unlink()
    companion_revision = commit(fixture.source, "target-only companion Stack")

    output = tmp_path / "companion"
    projection = controller.project_stack_resources(
        fixture.source,
        "staging",
        companion_revision,
        output,
        fixture.source,
        promotion=fixture.promotion,
    )

    assert set(projection.generated_units) == {"companion--deploy", "companion--image"}
    desired = controller.RESOURCE_CATALOG.parse_stack(
        controller.RESOURCE_CATALOG.load_document(controller.document_candidates(output / "stacks", "companion")[0]),
        profile="desired",
        expected_name="companion",
    )
    assert type(desired.spec.requestedSource).__name__ == "StackTemplateFromResource"
