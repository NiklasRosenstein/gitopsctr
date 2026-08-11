"""Read-only desired-state compatibility audit coverage."""

from __future__ import annotations

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from gitopsctr import cli
from gitopsctr.errors import OperationError
from tests.test_finalization import _terraform_unit


def _install_tree(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "observed_tree",
        lambda _ref, output: (shutil.copytree(root, output), "c" * 40)[1],
    )


def _run_audit(root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    _install_tree(root, monkeypatch)
    cli.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
    return json.loads(capsys.readouterr().out)


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
        cli.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
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
    unit = cli.load_desired_unit(unit_path, "application")
    intent = replace(
        cli.UnitDeletionIntent.from_unit(unit, unit_path, root),
        retained_identity_known=False,
    )
    cli.write_deletion_intent(root, intent)
    cli.write_opaque_cleanup_root(
        root,
        "orphan",
        cli.OpaqueCleanupRoot(
            path=root / ".gitopsctr/cleanup/units/orphan.json",
            payload="unparseable",
            metadata=cli.ResourceMetadata.source_tracked_from_provenance("orphan", "audit"),
            source=None,
        ),
    )

    _install_tree(root, monkeypatch)
    with pytest.raises(OperationError, match="2 finding"):
        cli.command_audit_desired_compatibility(Namespace(environment="dev", desired_ref="deploy/dev"))
    result = json.loads(capsys.readouterr().out)
    codes = {(finding["code"], finding["unit"]) for finding in result["findings"]}

    assert codes == {
        ("opaque-cleanup-root", "orphan"),
        ("unverified-deletion-identity", "application"),
    }
