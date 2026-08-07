from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def test_action_metadata_exposes_the_supported_operations_and_install_modes() -> None:
    metadata = yaml.safe_load((ROOT / "action.yml").read_text())

    assert metadata["runs"]["using"] == "composite"
    assert metadata["inputs"]["operation"]["required"] is True
    assert metadata["inputs"]["package-source"]["default"] == "pypi"
    assert {"reconciled", "desired-changed", "desired-revision", "change-revision"} <= set(metadata["outputs"])


def _fake_command(tmp_path: Path, name: str, body: str) -> Path:
    command = tmp_path / name
    command.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    command.chmod(0o755)
    return command


def _action_environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir(exist_ok=True)
    environment = os.environ | {
        "ACTION_LOG": str(tmp_path / "action.log"),
        "ADVANCE": "false",
        "CANDIDATE_REF": "",
        "DESIRED_REF": "",
        "DESIRED_REVISION": "",
        "DRY": "false",
        "ENVIRONMENT": "",
        "FROM_ENVIRONMENT": "",
        "OBSERVED_REF": "",
        "OPERATION": "",
        "PATH": f"{binary_directory}:{os.environ['PATH']}",
        "REAPPLY": "false",
        "REPORT": "",
        "REQUIRE_SOURCE_REF": "",
        "SOURCE_REVISION": "",
        "SPECIFICATION_REVISION": "",
        "TO_ENVIRONMENT": "",
        "UNIT": "",
        "WORKFLOW_REVISION": "f" * 40,
        "WORKING_DIRECTORY": str(tmp_path),
    }
    environment.update(overrides)
    return environment


def _run_action(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir(exist_ok=True)
    _fake_command(binary_directory, "gitopsctr", 'printf "%s\\0" "$@" > "${ACTION_LOG}"')
    return subprocess.run(
        ("bash", str(ROOT / "action/run.sh")),
        check=False,
        text=True,
        capture_output=True,
        env=_action_environment(tmp_path, **overrides),
    )


def _action_arguments(tmp_path: Path) -> list[str]:
    return [value.decode() for value in (tmp_path / "action.log").read_bytes().split(b"\0") if value]


def test_reconcile_action_maps_typed_inputs_to_cli_arguments(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="reconcile",
        ENVIRONMENT="dev",
        UNIT="application",
        DESIRED_REVISION="d" * 40,
        SOURCE_REVISION="s" * 40,
        REQUIRE_SOURCE_REF="main",
        ADVANCE="true",
        REAPPLY="true",
        REPORT="reports/application",
    )

    assert result.returncode == 0, result.stderr
    assert _action_arguments(tmp_path) == [
        "--repository",
        str(tmp_path),
        "reconcile",
        "--environment",
        "dev",
        "--unit",
        "application",
        "--desired-revision",
        "d" * 40,
        "--source-revision",
        "s" * 40,
        "--require-source-ref",
        "main",
        "--report",
        "reports/application",
        "--advance",
        "--reapply",
    ]


def test_advance_action_maps_environment_and_refs(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="advance",
        ENVIRONMENT="staging",
        DESIRED_REF="deploy/staging",
        OBSERVED_REF="observed/staging",
        DRY="true",
    )

    assert result.returncode == 0, result.stderr
    assert _action_arguments(tmp_path) == [
        "--repository",
        str(tmp_path),
        "advance-desired",
        "--environment",
        "staging",
        "--desired-ref",
        "deploy/staging",
        "--observed-ref",
        "observed/staging",
        "--dry",
    ]


def test_promote_action_defaults_to_the_workflow_revision(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="promote",
        FROM_ENVIRONMENT="dev",
        TO_ENVIRONMENT="staging",
        SOURCE_REVISION="d" * 40,
    )

    assert result.returncode == 0, result.stderr
    assert _action_arguments(tmp_path) == [
        "--repository",
        str(tmp_path),
        "promote",
        "--from-environment",
        "dev",
        "--to-environment",
        "staging",
        "--specification-revision",
        "f" * 40,
        "--source-desired-revision",
        "d" * 40,
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OPERATION": "reconcile", "ENVIRONMENT": "dev"}, "unit is required"),
        ({"OPERATION": "unknown"}, "operation must be"),
        ({"OPERATION": "advance", "ENVIRONMENT": "dev", "DRY": "sometimes"}, "dry must be"),
    ],
)
def test_action_rejects_invalid_operation_inputs(tmp_path: Path, overrides: dict[str, str], message: str) -> None:
    result = _run_action(tmp_path, **overrides)

    assert result.returncode == 2
    assert message in result.stderr


def _run_installer(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    _fake_command(binary_directory, "uv", 'printf "%s\\0" "$@" >> "${ACTION_LOG}"')
    github_path = tmp_path / "github-path"
    environment = os.environ | {
        "ACTION_LOG": str(tmp_path / "action.log"),
        "GITHUB_ACTION_PATH": str(ROOT),
        "GITHUB_PATH": str(github_path),
        "PACKAGE_REPOSITORY": "",
        "PACKAGE_REVISION": "",
        "PACKAGE_SOURCE": "pypi",
        "PACKAGE_VERSION": "",
        "PATH": f"{binary_directory}:{os.environ['PATH']}",
    }
    environment.update(overrides)
    result = subprocess.run(
        ("bash", str(ROOT / "action/install.sh")),
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    log = tmp_path / "action.log"
    arguments = [value.decode() for value in log.read_bytes().split(b"\0") if value] if log.exists() else []
    return result, arguments


@pytest.mark.parametrize(
    ("overrides", "expected_package"),
    [
        ({}, "gitopsctr"),
        ({"PACKAGE_VERSION": "1.2.3"}, "gitopsctr==1.2.3"),
        ({"PACKAGE_SOURCE": "action"}, str(ROOT)),
        (
            {
                "PACKAGE_SOURCE": "git",
                "PACKAGE_REPOSITORY": "example-org/gitopsctr",
                "PACKAGE_REVISION": "feature/test",
            },
            "gitopsctr @ git+https://github.com/example-org/gitopsctr.git@feature/test",
        ),
    ],
)
def test_installer_supports_pypi_action_and_git_sources(
    tmp_path: Path, overrides: dict[str, str], expected_package: str
) -> None:
    result, arguments = _run_installer(tmp_path, **overrides)

    assert result.returncode == 0, result.stderr
    assert arguments[:4] == ["tool", "install", "--force", expected_package]
    assert arguments[4:] == ["tool", "dir", "--bin"]


def test_git_installer_requires_an_explicit_revision(tmp_path: Path) -> None:
    result, arguments = _run_installer(
        tmp_path,
        PACKAGE_SOURCE="git",
        PACKAGE_REPOSITORY="example-org/gitopsctr",
    )

    assert result.returncode == 2
    assert "requires package-repository and package-revision" in result.stderr
    assert arguments == []
