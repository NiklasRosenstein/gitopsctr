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
    assert {
        "active",
        "reconciled",
        "desired-revision",
        "change-revision",
    } <= set(metadata["outputs"])
    assert "advance" not in metadata["inputs"]
    assert metadata["inputs"]["files"]["required"] is False


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
        "CANDIDATE_REF": "",
        "DESIRED_REF": "",
        "DESIRED_REVISION": "",
        "DRY": "false",
        "ENVIRONMENT": "",
        "FROM_ENVIRONMENT": "",
        "FILES": "",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "OBSERVED_REF": "",
        "OPERATION": "",
        "PLAN": "false",
        "PARTITION": "",
        "PATH": f"{binary_directory}:{os.environ['PATH']}",
        "PWD_LOG": str(tmp_path / "pwd.log"),
        "REAPPLY": "false",
        "REPORT": "",
        "REQUIRE_SOURCE_REF": "",
        "ROLLBACK_REASON": "",
        "ROLLBACK_REVISION": "",
        "ROLLBACK_UNITS": "",
        "SOURCE_REVISION": "",
        "SPECIFICATION_REVISION": "",
        "TO_ENVIRONMENT": "",
        "UNIT": "",
        "WORKFLOW_REVISION": "f" * 40,
        "WORKING_DIRECTORY": str(tmp_path),
    }
    environment.update(overrides)
    return environment


def _run_action(
    tmp_path: Path,
    command_body: str = 'printf "%s\\0" "$@" > "${ACTION_LOG}"',
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir(exist_ok=True)
    _fake_command(binary_directory, "gitopsctr", command_body)
    return subprocess.run(
        ("bash", str(ROOT / "action/run.sh")),
        check=False,
        text=True,
        capture_output=True,
        env=_action_environment(tmp_path, **overrides),
    )


def _action_arguments(tmp_path: Path) -> list[str]:
    return [value.decode() for value in (tmp_path / "action.log").read_bytes().split(b"\0") if value]


def _action_outputs(tmp_path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (tmp_path / "github-output").read_text().splitlines())


@pytest.mark.parametrize("desired_revision", ["d" * 40, ""])
def test_prepare_action_resolves_existing_desired_state(
    tmp_path: Path,
    desired_revision: str,
) -> None:
    resolved = "e" * 40
    result = _run_action(
        tmp_path,
        command_body='printf "%s\\0" "$@" > "${ACTION_LOG}"\nprintf "' + resolved + '\\n"',
        OPERATION="prepare",
        ENVIRONMENT="prod",
        DESIRED_REVISION=desired_revision,
    )

    assert result.returncode == 0, result.stderr
    expected = [
        "--repository",
        str(tmp_path),
        "resolve-desired",
        "--desired-ref",
        "gitopsctr/desired/prod",
    ]
    if desired_revision:
        expected += ["--desired-revision", desired_revision]
    assert _action_arguments(tmp_path) == expected
    assert _action_outputs(tmp_path) == {
        "active": "true",
        "desired_revision": resolved,
    }


def test_prepare_action_reports_missing_desired_state(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        command_body='printf "%s\\0" "$@" > "${ACTION_LOG}"',
        OPERATION="prepare",
        ENVIRONMENT="dev",
    )

    assert result.returncode == 0, result.stderr
    assert _action_outputs(tmp_path) == {
        "active": "false",
        "desired_revision": "",
    }


def test_apply_action_maps_explicit_files_partition_and_refs(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="apply",
        ENVIRONMENT="dev",
        FILES="deployment/app.yaml\n deployment/worker.yaml ",
        PARTITION="application",
        SOURCE_REVISION="s" * 40,
        DESIRED_REF="deploy/dev",
        OBSERVED_REF="observed/dev",
        CANDIDATE_REF="changes/dev/application",
        DRY="true",
    )

    assert result.returncode == 0, result.stderr
    assert _action_arguments(tmp_path) == [
        "--repository",
        str(tmp_path),
        "apply",
        "--environment",
        "dev",
        "--file",
        "deployment/app.yaml",
        "--file",
        "deployment/worker.yaml",
        "--partition",
        "application",
        "--source-revision",
        "s" * 40,
        "--desired-ref",
        "deploy/dev",
        "--observed-ref",
        "observed/dev",
        "--candidate-ref",
        "changes/dev/application",
        "--dry",
    ]


def test_action_runs_gitopsctr_from_the_configured_working_directory(tmp_path: Path) -> None:
    working_directory = tmp_path / "repository"
    working_directory.mkdir()
    result = _run_action(
        tmp_path,
        command_body='printf "%s\\0" "$@" > "${ACTION_LOG}"\nprintf "%s" "$PWD" > "${PWD_LOG}"',
        OPERATION="apply",
        ENVIRONMENT="dev",
        FILES="input.yaml",
        WORKING_DIRECTORY=str(working_directory),
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pwd.log").read_text() == str(working_directory)


@pytest.mark.parametrize("files", ["", "deployment/application.yaml"])
def test_converge_action_maps_optional_input_and_selection(tmp_path: Path, files: str) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="converge",
        ENVIRONMENT="dev",
        FILES=files,
        PARTITION="application",
        SOURCE_REVISION="s" * 40,
    )

    assert result.returncode == 0, result.stderr
    expected = ["--repository", str(tmp_path), "converge", "--environment", "dev"]
    if files:
        expected += ["--file", files]
    expected += ["--partition", "application", "--source-revision", "s" * 40]
    assert _action_arguments(tmp_path) == expected


def test_reconcile_action_maps_typed_inputs_to_cli_arguments(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="reconcile",
        ENVIRONMENT="dev",
        UNIT="application",
        DESIRED_REVISION="d" * 40,
        SOURCE_REVISION="s" * 40,
        REQUIRE_SOURCE_REF="main",
        PLAN="true",
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
        "--plan",
        "--reapply",
    ]


@pytest.mark.parametrize(
    ("units", "unit_arguments"),
    [
        ("", []),
        ("aws-application, frontend", ["--unit", "aws-application", "--unit", "frontend"]),
    ],
)
def test_rollback_action_maps_full_or_targeted_change(
    tmp_path: Path,
    units: str,
    unit_arguments: list[str],
) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="rollback",
        ENVIRONMENT="prod",
        ROLLBACK_REVISION="d" * 40,
        ROLLBACK_UNITS=units,
        ROLLBACK_REASON="Incident mitigation",
        DESIRED_REF="deploy/prod",
        OBSERVED_REF="observed/prod",
        CANDIDATE_REF="changes/prod/rollback",
        DRY="true",
    )

    assert result.returncode == 0, result.stderr
    assert _action_arguments(tmp_path) == [
        "--repository",
        str(tmp_path),
        "rollback",
        "--environment",
        "prod",
        "--to-desired-revision",
        "d" * 40,
        "--reason",
        "Incident mitigation",
        "--desired-ref",
        "deploy/prod",
        "--observed-ref",
        "observed/prod",
        "--candidate-ref",
        "changes/prod/rollback",
        *unit_arguments,
        "--dry",
    ]


def test_promote_action_defaults_to_the_workflow_revision(tmp_path: Path) -> None:
    result = _run_action(
        tmp_path,
        OPERATION="promote",
        FROM_ENVIRONMENT="dev",
        TO_ENVIRONMENT="staging",
        FILES="deployment/environments/staging/stacks/application.yaml",
        PARTITION="application",
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
        "--file",
        "deployment/environments/staging/stacks/application.yaml",
        "--partition",
        "application",
        "--source-desired-revision",
        "d" * 40,
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OPERATION": "reconcile", "ENVIRONMENT": "dev"}, "unit is required"),
        ({"OPERATION": "unknown"}, "operation must be"),
        ({"OPERATION": "apply", "ENVIRONMENT": "dev", "DRY": "sometimes"}, "dry must be"),
        ({"OPERATION": "apply", "ENVIRONMENT": "dev", "PLAN": "sometimes"}, "plan must be"),
        ({"OPERATION": "apply", "ENVIRONMENT": "dev", "PLAN": "true"}, "plan is only valid"),
        ({"OPERATION": "reconcile", "ENVIRONMENT": "dev", "UNIT": "app", "DRY": "true"}, "dry is only valid"),
        ({"OPERATION": "apply", "ENVIRONMENT": "dev"}, "files is required"),
        ({"OPERATION": "promote", "FROM_ENVIRONMENT": "dev", "TO_ENVIRONMENT": "staging"}, "files is required"),
        ({"OPERATION": "prepare", "ENVIRONMENT": "dev", "SOURCE_REVISION": "s" * 40}, "does not accept source"),
        ({"OPERATION": "reconcile", "ENVIRONMENT": "dev", "UNIT": "app", "FILES": "app.yaml"}, "files is only"),
        (
            {
                "OPERATION": "converge",
                "ENVIRONMENT": "dev",
                "UNIT": "app",
                "PARTITION": "application",
            },
            "mutually exclusive",
        ),
        (
            {"OPERATION": "rollback", "ENVIRONMENT": "prod", "ROLLBACK_REVISION": "d" * 40},
            "reason is required",
        ),
        (
            {
                "OPERATION": "rollback",
                "ENVIRONMENT": "prod",
                "ROLLBACK_REVISION": "d" * 40,
                "ROLLBACK_REASON": "test",
                "ROLLBACK_UNITS": "aws-application,,frontend",
            },
            "units contains an empty unit name",
        ),
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
