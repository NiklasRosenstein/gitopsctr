"""Read-only desired-state compatibility audit coverage."""

from __future__ import annotations

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from tests.test_finalization import _terraform_unit


def _install_tree(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        controller,
        "observed_tree",
        lambda _ref, output: (shutil.copytree(root, output), "c" * 40)[1],
    )


def _run_audit(root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    _install_tree(root, monkeypatch)
    controller.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
    return json.loads(capsys.readouterr().out)


def _write_project_and_environments(
    root: Path,
    environments: dict[str, str | None],
    *,
    environments_path: str = "deployment/environments",
) -> None:
    project = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Project",
        "metadata": {"name": "audit-test"},
        "spec": {"environmentsPath": environments_path},
    }
    (root / "gitopsctr.yaml").write_text(json.dumps(project))
    for name, desired_ref in environments.items():
        environment = {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Environment",
            "metadata": {"name": name},
            "spec": {},
        }
        if desired_ref is not None:
            environment["spec"] = {"refs": {"desired": desired_ref}}
        environment_root = root / environments_path / name
        environment_root.mkdir(parents=True)
        (environment_root / "environment.json").write_text(json.dumps(environment))


def _write_canonical_tree(root: Path, name: str = "application") -> None:
    unit_path = root / f"units/{name}.json"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(json.dumps(_terraform_unit(name, f"d1-{name}")))


def _install_ref_trees(
    trees: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    unavailable: set[str] = frozenset(),
    missing: set[str] = frozenset(),
) -> None:
    def observed_tree(ref: str, output: Path) -> str | None:
        if ref in unavailable:
            raise RuntimeError("ref unavailable")
        if ref in missing:
            return None
        source = trees[ref]
        shutil.copytree(source, output)
        return (ref.replace("/", "") + "0" * 40)[:40]

    monkeypatch.setattr(controller, "observed_tree", observed_tree)


def test_compatibility_audit_accepts_canonical_desired_state(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "desired"
    unit_path = root / "units/application.json"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(json.dumps(_terraform_unit("application", "d1-application")))

    result = _run_audit(root, monkeypatch, capsys)

    assert result == {
        "clean": True,
        "environment": "dev",
        "findings": [],
        "ref": "deploy/dev",
        "revision": "c" * 40,
        "schema": 1,
    }


def test_compatibility_audit_reports_legacy_unit(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "desired"
    unit_path = root / "units/application.json"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(
        json.dumps(
            {
                "name": "application",
                "driver": "terraform",
                "source": {"path": ".", "revision": "a" * 40},
            }
        )
    )

    _install_tree(root, monkeypatch)
    with pytest.raises(OperationError, match="1 finding"):
        controller.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
    result = json.loads(capsys.readouterr().out)

    assert result["clean"] is False
    assert result["findings"] == [
        {
            "code": "legacy-unit",
            "message": "desired Unit has no lifecycle identity",
            "path": "units/application.json",
            "unit": "application",
        }
    ]


def test_compatibility_audit_reports_opaque_and_unverified_cleanup_state(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "desired"
    unit_path = root / "units/application.json"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(json.dumps(_terraform_unit("application", "d1-application")))
    unit = controller.load_desired_unit(unit_path, "application")
    intent = replace(
        controller.UnitDeletionIntent.from_unit(unit, unit_path, root),
        retained_identity_known=False,
    )
    controller.write_deletion_intent(root, intent)
    controller.write_opaque_cleanup_root(
        root,
        "orphan",
        controller.OpaqueCleanupRoot(
            path=root / ".gitopsctr/cleanup/units/orphan.json",
            payload="unparseable",
            metadata=controller.ResourceMetadata.source_tracked_from_provenance("orphan", "audit"),
            source=None,
        ),
    )

    _install_tree(root, monkeypatch)
    with pytest.raises(OperationError, match="2 finding"):
        controller.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
    result = json.loads(capsys.readouterr().out)
    codes = {(finding["code"], finding["unit"]) for finding in result["findings"]}

    assert codes == {
        ("opaque-cleanup-root", "orphan"),
        ("unverified-deletion-identity", "application"),
    }


def test_aggregate_compatibility_audit_covers_multiple_environments(tmp_path: Path, monkeypatch, capsys):
    _write_project_and_environments(tmp_path, {"dev": None, "staging": None})
    trees = {
        ref: tmp_path / f"tree-{environment}"
        for environment, ref in (("dev", "gitopsctr/desired/dev"), ("staging", "gitopsctr/desired/staging"))
    }
    for tree in trees.values():
        _write_canonical_tree(tree)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    _install_ref_trees(trees, monkeypatch)

    args = controller.build_parser().parse_args(["audit-desired-compatibility", "--all"])
    args.handler(args)
    result = json.loads(capsys.readouterr().out)

    assert result["schema"] == 1
    assert result["mode"] == "all"
    assert result["clean"] is True
    assert [item["environment"] for item in result["environments"]] == ["dev", "staging"]
    assert [item["ref"] for item in result["environments"]] == [
        "gitopsctr/desired/dev",
        "gitopsctr/desired/staging",
    ]
    assert result["findings"] == []


def test_aggregate_compatibility_audit_uses_custom_project_and_environment_refs(tmp_path: Path, monkeypatch, capsys):
    _write_project_and_environments(
        tmp_path,
        {"dev": "custom/dev", "staging": None},
        environments_path="config/environments",
    )
    (tmp_path / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "audit-test"},
                "spec": {
                    "environmentsPath": "config/environments",
                    "environmentDefaults": {"refs": {"desired": "release/{environment}"}},
                },
            }
        )
    )
    trees = {"custom/dev": tmp_path / "tree-dev", "release/staging": tmp_path / "tree-staging"}
    for tree in trees.values():
        _write_canonical_tree(tree)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    _install_ref_trees(trees, monkeypatch)

    args = controller.build_parser().parse_args(["audit-desired-compatibility", "--all"])
    args.handler(args)
    result = json.loads(capsys.readouterr().out)

    assert [(item["environment"], item["ref"]) for item in result["environments"]] == [
        ("dev", "custom/dev"),
        ("staging", "release/staging"),
    ]


def test_aggregate_compatibility_audit_rejects_duplicate_desired_refs(tmp_path: Path, monkeypatch, capsys):
    _write_project_and_environments(tmp_path, {"dev": "shared", "staging": "shared"})
    tree = tmp_path / "tree-shared"
    _write_canonical_tree(tree)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    _install_ref_trees({"shared": tree}, monkeypatch)

    args = controller.build_parser().parse_args(["audit-desired-compatibility", "--all"])
    with pytest.raises(OperationError, match="aggregate desired compatibility"):
        args.handler(args)
    result = json.loads(capsys.readouterr().out)

    assert result["clean"] is False
    assert [
        (item["environment"], [finding["code"] for finding in item["findings"]]) for item in result["environments"]
    ] == [("dev", ["duplicate-desired-ref"]), ("staging", ["duplicate-desired-ref"])]


def test_aggregate_compatibility_audit_reports_partial_ref_failures(tmp_path: Path, monkeypatch, capsys):
    _write_project_and_environments(
        tmp_path,
        {"dev": "good", "missing": "missing", "unavailable": "unavailable"},
    )
    good_tree = tmp_path / "tree-good"
    _write_canonical_tree(good_tree)
    monkeypatch.setattr(controller, "REPOSITORY_ROOT", tmp_path)
    _install_ref_trees({"good": good_tree}, monkeypatch, unavailable={"unavailable"}, missing={"missing"})

    args = controller.build_parser().parse_args(["audit-desired-compatibility", "--all"])
    with pytest.raises(OperationError, match="aggregate desired compatibility"):
        args.handler(args)
    result = json.loads(capsys.readouterr().out)

    assert result["clean"] is False
    assert [(item["environment"], item["clean"]) for item in result["environments"]] == [
        ("dev", True),
        ("missing", False),
        ("unavailable", False),
    ]
    assert [item["findings"][0]["code"] for item in result["environments"][1:]] == [
        "missing-ref",
        "unavailable-ref",
    ]
