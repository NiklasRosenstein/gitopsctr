"""Hermetic StackTemplate source acquisition acceptance tests."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from gitopsctr import controller
from gitopsctr.contracts import DesiredStackTemplateSpec
from gitopsctr.errors import OperationError
from gitopsctr.state import GitStateStore
from tests.stack_support import cloned_project_repository as _repository
from tests.stack_support import commit, git, project_repository
from tests.test_promote import _promotion_repository


def _template_document(name: str = "preview", value: str = "external") -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": name},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "app": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {"source": {"path": "."}, "terraform": {"variables": {"value": value}}},
                }
            },
        },
    }


def _stack_document(name: str = "web", template: str = "preview") -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Stack",
        "metadata": {"name": name},
        "spec": {"template": template},
    }


def _apply(source: Path, revision: str, *paths: Path, dry: bool = False) -> str:
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
    for path in paths:
        arguments.extend(("-f", str(path)))
    if dry:
        arguments.append("--dry")
    result = controller.command_apply(controller.build_parser().parse_args(arguments))
    return result or ""


def _source_repository(root: Path) -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    remote = root / "source.git"
    working = root / "source-working"
    git(root, "init", "--bare", str(remote))
    project_repository(working)
    git(working, "init", "-b", "main")
    git(working, "remote", "add", "origin", str(remote))
    template = working / "templates/preview.yaml"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(yaml.safe_dump(_template_document(), sort_keys=False))
    revision = commit(working, "publish external StackTemplate")
    git(working, "push", "-u", "origin", "main")
    return working, remote, revision


def test_external_git_template_is_acquired_with_exact_lineage_and_clean_import_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller_root = tmp_path / "controller"
    controller_root.mkdir()
    source, _store, _controller_revision = _repository(controller_root, monkeypatch)
    _external, external_remote, external_revision = _source_repository(tmp_path / "external")
    selector = source / "selector.yaml"
    selector.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "source": {
                        "fromGit": {
                            "repository": str(external_remote),
                            "revision": "main",
                            "path": "templates/preview.yaml",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    stack = source / "stack.yaml"
    stack.write_text(yaml.safe_dump(_stack_document(), sort_keys=False))
    controller_revision = commit(source, "select external StackTemplate")

    published = _apply(source, controller_revision, selector, stack)
    desired = tmp_path / "desired"
    store = GitStateStore(source)
    store.materialize(published, desired)
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(next((desired / "stack-templates").glob("preview.*"))),
        profile="desired",
        expected_name="preview",
    )
    assert isinstance(template.spec, DesiredStackTemplateSpec)
    assert template.spec.acquisition.requestedSource.fromGit.repository == external_remote.resolve().as_uri()
    assert template.spec.acquisition.resolvedSource.fromGit.revision == external_revision
    assert template.spec.sourceContext is not None
    assert template.spec.sourceContext.repository == external_remote.resolve().as_uri()
    assert template.spec.sourceContext.revision == external_revision
    assert template.spec.acquisition.documentDigest == (
        "sha256:"
        + hashlib.sha256((tmp_path / "external/source-working/templates/preview.yaml").read_bytes()).hexdigest()
    )
    assert not store.git("ls-remote", "--refs", "origin", "refs/heads/gitopsctr/source-retention/*", check=False).stdout

    shutil.rmtree(tmp_path / "external/source.git")
    store.git("reflog", "expire", "--expire=now", "--all")
    store.git("gc", "--prune=now")
    stack_only_args = [
        "apply",
        "--environment",
        "dev",
        "--desired-ref",
        "deploy/dev",
        "--observed-ref",
        "observed/dev",
        "-f",
        str(source / "stack.yaml"),
    ]
    assert controller.command_apply(controller.build_parser().parse_args(stack_only_args)) is not None


def test_git_template_expected_digest_and_recursive_source_fail_without_retention_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller_root = tmp_path / "controller"
    controller_root.mkdir()
    source, _store, _controller_revision = _repository(controller_root, monkeypatch)
    external, external_remote, _revision = _source_repository(tmp_path / "external")
    external_revision = _revision
    selector = source / "selector.yaml"
    selector.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "source": {
                        "fromGit": {
                            "repository": str(external_remote),
                            "revision": external_revision,
                            "path": "templates/preview.yaml",
                            "documentDigest": "sha256:" + "0" * 64,
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    controller_revision = commit(source, "select invalid external StackTemplate")

    with pytest.raises(OperationError, match="documentDigest mismatch"):
        _apply(source, controller_revision, selector)
    store = GitStateStore(source)
    assert not store.git("ls-remote", "--refs", "origin", "refs/heads/gitopsctr/source-retention/*", check=False).stdout

    recursive = external / "templates/recursive.yaml"
    recursive.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {"source": {"fromGit": {"repository": ".", "revision": "main", "path": "x.yaml"}}},
            },
            sort_keys=False,
        )
    )
    external_revision = commit(external, "publish recursive source")
    git(external, "push", "origin", "main")
    document = yaml.safe_load(selector.read_text())
    document["spec"]["source"]["fromGit"].pop("documentDigest")
    document["spec"]["source"]["fromGit"]["revision"] = external_revision
    document["spec"]["source"]["fromGit"]["path"] = "templates/recursive.yaml"
    selector.write_text(yaml.safe_dump(document, sort_keys=False))
    controller_revision = commit(source, "remove invalid digest")
    with pytest.raises(OperationError, match="recursively"):
        _apply(source, controller_revision, selector)
    assert not store.git("ls-remote", "--refs", "origin", "refs/heads/gitopsctr/source-retention/*", check=False).stdout


def test_dry_external_acquisition_is_remote_ref_pure_for_changed_and_noop_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller_root = tmp_path / "controller"
    controller_root.mkdir()
    source, _store, _controller_revision = _repository(controller_root, monkeypatch)
    _external, external_remote, _external_revision = _source_repository(tmp_path / "external")
    selector = source / "selector.yaml"
    selector.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "preview"},
                "spec": {
                    "source": {
                        "fromGit": {
                            "repository": str(external_remote),
                            "revision": "main",
                            "path": "templates/preview.yaml",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    stack = source / "stack.yaml"
    stack.write_text(yaml.safe_dump(_stack_document(), sort_keys=False))
    controller_revision = commit(source, "select external StackTemplate for dry run")
    store = GitStateStore(source)

    before = store.list_remote_refs()
    assert _apply(source, controller_revision, selector, stack, dry=True) == (store.fetch("deploy/dev").revision or "")
    assert store.list_remote_refs() == before
    assert not store.list_controller_pins()
    assert not store.git("ls-remote", "--refs", "origin", "refs/heads/gitopsctr/source-retention/*").stdout

    published = _apply(source, controller_revision, selector, stack)
    after_publish = store.list_remote_refs()
    assert published
    assert _apply(source, controller_revision, selector, stack, dry=True) == published
    assert store.list_remote_refs() == after_publish


def test_from_promotion_uses_the_pinned_source_template_and_apply_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _promotion_repository(tmp_path / "promotion")
    git(source, "init", "-b", "main")
    remote = tmp_path / "promotion-origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    source_template = source / "deployment/stack-templates/application.yaml"
    source_template.parent.mkdir(parents=True, exist_ok=True)
    source_template.write_text(yaml.safe_dump(_template_document("application", "source"), sort_keys=False))
    source_stack = source / "deployment/environments/dev/stacks/application.yaml"
    source_stack.parent.mkdir(parents=True, exist_ok=True)
    source_stack.write_text((source / "target-stack.yaml").read_text())
    promoted_selector = source / "promoted-template.yaml"
    promoted_selector.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "StackTemplate",
                "metadata": {"name": "application"},
                "spec": {"source": {"fromPromotion": {"stack": "application"}}},
            },
            sort_keys=False,
        )
    )
    promoted_stack = source / "promoted-stack.yaml"
    promoted_stack.write_text(yaml.safe_dump(_stack_document("application", "application"), sort_keys=False))
    specification_revision = commit(source, "publish source StackTemplate and Stack")
    git(source, "push", "-u", "origin", "main")
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", source)
    controller._state_store.cache_clear()
    store = GitStateStore(source)

    source_desired = tmp_path / "source-desired"
    source_observed = tmp_path / "source-observed"
    source_desired.mkdir()
    source_observed.mkdir()
    source_candidate = tmp_path / "source-candidate"
    controller.build_desired_candidate(
        "dev", source, specification_revision, source_desired, source_observed, None, source_candidate, verbose=False
    )
    source_template_document = next((source_candidate / "stack-templates").glob("application.*")).read_bytes()
    source_revision = store.publish(
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
            str(promoted_selector),
            "-f",
            str(promoted_stack),
        ]
    )
    controller.command_promote(args)
    target_revision = store.fetch("gitopsctr/desired/staging").revision
    assert target_revision is not None
    target = tmp_path / "target-desired"
    store.materialize(target_revision, target)
    template = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(next((target / "stack-templates").glob("application.*"))),
        profile="desired",
        expected_name="application",
    )
    assert isinstance(template.spec, DesiredStackTemplateSpec)
    resolved = template.spec.acquisition.resolvedSource.fromPromotion
    assert resolved.desiredRevision == source_revision
    assert resolved.stack == "application"
    assert resolved.template == "application"
    assert resolved.templateUid
    assert template.spec.acquisition.documentDigest == (
        "sha256:" + hashlib.sha256(source_template_document).hexdigest()
    )

    deleting_source = tmp_path / "deleting-source"
    shutil.copytree(source_candidate, deleting_source)
    deleting_path = next((deleting_source / "stack-templates").glob("application.*"))
    deleting_resource = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(deleting_path),
        profile="desired",
        expected_name="application",
    )
    controller.write_document(
        deleting_path,
        controller.RESOURCE_CATALOG.serialize_stack_resource(
            controller.mark_resource_for_deletion(deleting_resource), profile="desired"
        ),
        format=controller._document_format_for_path(deleting_path),
    )
    promotion_context = controller.PromotionContext(
        source_environment="dev",
        desired_ref="gitopsctr/desired/dev",
        desired_revision=source_revision,
        observed_ref="gitopsctr/observed/dev",
        observed_revision=None,
        specification_revision=specification_revision,
        desired_root=deleting_source,
    )
    authored_promotion = controller.RESOURCE_CATALOG.parse_stack_template(
        controller.RESOURCE_CATALOG.load_document(promoted_selector),
        profile="authored",
        expected_name="application",
    )
    with pytest.raises(OperationError, match="StackTemplate .* deleting"):
        controller._acquire_promoted_stack_template(authored_promotion, "application", promotion_context)

    with pytest.raises(OperationError, match="explicit promote transaction"):
        _apply(source, specification_revision, promoted_selector)
