"""Acceptance-level coverage for Stack lifecycle recovery and ordering."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import cli
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata
from gitopsctr.state import ControllerPin
from tests.test_stack_deletion import _args, _fake_git, _stack_tree
from tests.test_stack_projection import _project, _write_stack_source


def test_source_tracked_stack_cleanup_is_durable_across_restart(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    initial = tmp_path / "initial"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, initial, source)
    initial_stack = cli.RESOURCE_CATALOG.parse_stack(
        cli.RESOURCE_CATALOG.load_document(next((initial / "stacks").glob("web.*"))),
        profile="desired",
        expected_name="web",
    )

    for name, unit in projection.generated_units.items():
        cli.write_desired_candidate_unit(
            initial / "units" / f"{name}.json",
            unit.with_metadata(
                ResourceMetadata(
                    name=name,
                    uid=f"d1-{name}",
                    lifecycle=cli.DesiredLifecycle(owner=projection.owners[name]),
                )
            ),
            source,
        )
    cli.load_desired_resource_graph(initial)

    (environment / "stacks/web.json").unlink()
    observed = tmp_path / "observed"
    observed.mkdir()
    candidate = tmp_path / "candidate"
    first = cli.build_desired_candidate(
        "dev",
        source,
        "b" * 40,
        initial,
        observed,
        None,
        candidate,
        verbose=False,
    )

    first_intent = cli.load_desired_stack_deletion_intents(candidate)["web"]
    assert first.blocked == {}
    assert initial_stack.metadata.uid is not None
    assert first_intent.uid == initial_stack.metadata.uid
    assert [identity.unit_name for identity in first_intent.owned_unit_closure] == ["web--preview-app"]
    assert (
        cli.load_desired_unit(candidate / "units/web--preview-app.json", "web--preview-app").metadata.uid
        == "d1-web--preview-app"
    )

    restarted = tmp_path / "restarted"
    second = cli.build_desired_candidate(
        "dev",
        source,
        "c" * 40,
        candidate,
        observed,
        None,
        restarted,
        verbose=False,
    )

    second_intent = cli.load_desired_stack_deletion_intents(restarted)["web"]
    assert second.blocked == {}
    assert second_intent == first_intent
    assert cli.load_desired_resource_graph(restarted)


def test_two_stacks_from_one_template_have_independent_generated_units(tmp_path: Path):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    (environment / "stacks/api.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "api"},
                "spec": {"template": "preview", "parameters": {"source-path": "."}},
            }
        )
    )

    candidate = tmp_path / "candidate"
    projection = cli.project_stack_resources(source, "dev", "a" * 40, candidate, source)
    assert sorted(projection.generated_units) == ["api--preview-app", "web--preview-app"]
    assert projection.owners["api--preview-app"].name == "api"
    assert projection.owners["web--preview-app"].name == "web"
    for name, unit in projection.generated_units.items():
        cli.write_desired_candidate_unit(
            candidate / "units" / f"{name}.json",
            unit.with_metadata(
                ResourceMetadata(
                    name=name,
                    uid=f"d1-{name}",
                    lifecycle=cli.DesiredLifecycle(owner=projection.owners[name]),
                )
            ),
            source,
        )

    graph = cli.load_desired_resource_graph(candidate)
    assert ("unit.gitopsctr.io/v1", "Terraform", "api--preview-app") in graph
    assert ("unit.gitopsctr.io/v1", "Terraform", "web--preview-app") in graph


def test_direct_stack_finalization_retries_after_injected_publication_failure(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    stack_uid, unit_name = _stack_tree(current)
    published: list[Path] = []
    released: list[tuple[str, str]] = []
    pin = ControllerPin(
        "stacks/dev/preview/d1-stack-direct",
        "refs/heads/gitopsctr/pins/stacks/dev/preview/d1-stack-direct",
        "a" * 40,
    )
    store = SimpleNamespace(
        create_controller_pin=lambda _name, _revision: pin,
        release_controller_pin=lambda name, revision: released.append((name, revision)) or True,
    )

    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(cli, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(cli, "fetch_ref", lambda _ref: "c" * 40)
    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(current, output))
    monkeypatch.setattr(cli, "state_store", lambda: store)
    monkeypatch.setattr(cli, "git", _fake_git)
    monkeypatch.setattr(cli, "resolve_candidate_ref", lambda *_args, **_kwargs: "candidate/dev")

    def publish(_environment: str, candidate: Path, *_args: object, **_kwargs: object):
        snapshot = tmp_path / f"published-{len(published)}"
        shutil.copytree(candidate, snapshot)
        published.append(snapshot)
        return "d" * 40, None

    monkeypatch.setattr(cli, "publish_desired_change", publish)
    assert cli.command_request_delete_direct_stack(_args()) is True

    requested = published[0]
    retryable = tmp_path / "retryable"
    shutil.copytree(requested, retryable)
    (retryable / "units" / f"{unit_name}.json").unlink()
    for path in cli.document_candidates(retryable / ".gitopsctr/deletion-intents/units", unit_name):
        path.unlink()

    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(retryable, output))
    monkeypatch.setattr(
        cli,
        "publish_desired_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationError("injected publication failure")),
    )
    with pytest.raises(OperationError, match="injected publication failure"):
        cli.command_finalize_stack(_args())

    assert cli.load_desired_stack_deletion_intents(retryable)["preview"].uid == stack_uid
    assert released == []

    monkeypatch.setattr(cli, "publish_desired_change", publish)
    assert cli.command_finalize_stack(_args()) is True
    assert cli.load_desired_stack_deletion_intents(published[-1]) == {}
    assert not list((published[-1] / "stacks").glob("preview.*"))
    assert released == [("stacks/dev/preview/d1-stack-direct", "a" * 40)]


def test_dependencies_cli_preserves_explicit_stack_order_after_restart(tmp_path: Path, monkeypatch, capsys):
    source = tmp_path / "source"
    environment = _project(source)
    _write_stack_source(environment)
    template_path = environment / "stack-templates/preview.json"
    template = json.loads(template_path.read_text())
    template["spec"]["resources"].append(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "name": "preview-db",
            "spec": {"source": {"path": "."}},
        }
    )
    template["spec"]["resources"][0]["dependsOn"] = ["preview-db"]
    template_path.write_text(json.dumps(template))

    source_revision = "a" * 40
    monkeypatch.setattr(
        cli,
        "git",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=source_revision + "\n"),
    )
    monkeypatch.setattr(cli, "materialize_revision", lambda _revision, output: shutil.copytree(source, output))
    args = cli.build_parser().parse_args(
        [
            "dependencies",
            "--environment",
            "dev",
            "--source-revision",
            "HEAD",
            "--unit",
            "web--preview-app",
            "--json",
        ]
    )

    args.handler(args)
    first = json.loads(capsys.readouterr().out)
    args.handler(args)
    restarted = json.loads(capsys.readouterr().out)

    assert first == restarted
    assert [unit["name"] for unit in first["units"]] == ["web--preview-db", "web--preview-app"]
    assert first["units"][-1]["dependencies"] == ["web--preview-db"]
