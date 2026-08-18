"""Tests for the first validation command routed through the application port."""

from __future__ import annotations

import argparse
import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

from gitopsctr import composition, controller
from gitopsctr.adapters.source_authored import SourceAuthoredSpecificationValidator
from gitopsctr.application import (
    EnvironmentId,
    Orchestrator,
    ResourceInspectionCommand,
    ResourceInspectionResult,
    SnapshotId,
    ValidateCommand,
    ValidationFailFastError,
    ValidationIssue,
    ValidationResult,
    ValidationSubject,
)
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.errors import OperationError


def _run_cli(root: Path, arguments: list[str]) -> None:
    controller.REPOSITORY_ROOT = root
    args = controller.build_parser().parse_args(arguments)
    args.handler(args)


def _create_project_with_environment(root: Path) -> None:
    _run_cli(root, ["create", "project", "--name", "example"])
    _run_cli(root, ["create", "environment", "--name", "dev"])


@dataclass(frozen=True)
class RecordingValidator:
    result: ValidationResult
    calls: list[ValidateCommand]
    closes: list[None]

    def validate(self, command: ValidateCommand) -> ValidationResult:
        self.calls.append(command)
        return self.result

    def close(self) -> None:
        self.closes.append(None)


@dataclass
class RecordingSnapshotReader:
    close_count: int = 0

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        raise AssertionError("snapshot inspection is not expected")

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingResourceInspector:
    close_count: int = 0

    def inspect(self, _command: ResourceInspectionCommand) -> ResourceInspectionResult:
        raise AssertionError("resource inspection is not expected")

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingApplication:
    result: ValidationResult
    calls: list[ValidateCommand]
    close_count: int = 0

    def validate(self, command: ValidateCommand) -> ValidationResult:
        self.calls.append(command)
        return self.result

    def close(self) -> None:
        self.close_count += 1

    def __enter__(self) -> RecordingApplication:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def test_application_services_uses_its_injected_specification_validator() -> None:
    command = ValidateCommand(("gitopsctr.yaml",), (EnvironmentId("dev"),))
    expected = ValidationResult(validated_documents=("gitopsctr.yaml",), validated_environments=(EnvironmentId("dev"),))
    validator = RecordingValidator(expected, [], [])
    reader = RecordingSnapshotReader()
    inspector = RecordingResourceInspector()
    services = ApplicationServices(reader, validator, inspector)
    orchestrator: Orchestrator = services

    assert orchestrator.validate(command) is expected
    assert validator.calls == [command]
    services.close()
    services.close()
    assert len(validator.closes) == 1
    assert reader.close_count == 1
    assert inspector.close_count == 1


def test_application_services_context_closes_both_dependencies_after_an_exception() -> None:
    reader = RecordingSnapshotReader()
    validator = RecordingValidator(ValidationResult(), [], [])
    inspector = RecordingResourceInspector()

    with pytest.raises(RuntimeError, match="operation failed"):
        with ApplicationServices(reader, validator, inspector):
            raise RuntimeError("operation failed")

    assert reader.close_count == 1
    assert len(validator.closes) == 1
    assert inspector.close_count == 1


def test_source_authored_validator_returns_logical_counts_and_preserves_invalid_fail_fast_parity(
    tmp_path: Path,
) -> None:
    _create_project_with_environment(tmp_path)
    units = tmp_path / "deployment" / "environments" / "dev" / "units"
    units.mkdir()
    for name, kind in (("one", "Missing"), ("two", "AlsoMissing")):
        (units / f"{name}.yaml").write_text(
            yaml.safe_dump({"apiVersion": "unit.gitopsctr.io/v1", "kind": kind}, sort_keys=False)
        )
    validator = SourceAuthoredSpecificationValidator(tmp_path)

    result = validator.validate(ValidateCommand(environments=(EnvironmentId("dev"),)))

    assert not result.valid
    assert len(result.issues) == 2
    assert result.validated_environments == (EnvironmentId("dev"),)
    assert {issue.subject.value for issue in result.issues} == {
        "deployment/environments/dev/units/one.yaml",
        "deployment/environments/dev/units/two.yaml",
    }
    with pytest.raises(ValidationFailFastError, match="one.yaml") as raised:
        validator.validate(ValidateCommand(environments=(EnvironmentId("dev"),), fail_fast=True))
    assert raised.value.issue == result.issues[0]


def test_validation_issue_preserves_human_parser_detail_and_accepts_non_path_subjects() -> None:
    issue = ValidationIssue(ValidationSubject("../invalid-environment"), "first line\n\tparser detail")

    assert issue.subject == ValidationSubject("../invalid-environment")
    assert issue.message == "first line\n\tparser detail"
    with pytest.raises(ValueError, match="NUL"):
        ValidationIssue(ValidationSubject("subject"), "unsafe\0detail")
    with pytest.raises(TypeError, match="ValidationSubject"):
        ValidationIssue(cast(ValidationSubject, "plain string"), "detail")
    assert ValidationSubject(" malformed\n\tname ").value == " malformed\n\tname "
    with pytest.raises(ValueError, match="NUL"):
        ValidationSubject("unsafe\0subject")


def test_malformed_yaml_has_identical_collected_and_fail_fast_issue(tmp_path: Path) -> None:
    malformed = tmp_path / "broken.yaml"
    malformed.write_text("apiVersion: [\n")
    validator = SourceAuthoredSpecificationValidator(tmp_path)

    result = validator.validate(ValidateCommand(("broken.yaml",)))

    assert len(result.issues) == 1
    assert result.issues[0].subject == ValidationSubject("broken.yaml")
    assert "could not parse" in result.issues[0].message
    assert "\n" in result.issues[0].message
    with pytest.raises(ValidationFailFastError) as raised:
        validator.validate(ValidateCommand(("broken.yaml",), fail_fast=True))
    assert raised.value.issue == result.issues[0]


def test_invalid_environment_has_identical_collected_and_fail_fast_issue(tmp_path: Path) -> None:
    validator = SourceAuthoredSpecificationValidator(tmp_path)
    environments = (EnvironmentId("../outside"),)

    result = validator.validate(ValidateCommand(environments=environments))

    assert len(result.issues) == 1
    assert result.issues[0].subject == ValidationSubject("../outside")
    assert "invalid environment name" in result.issues[0].message
    with pytest.raises(ValidationFailFastError) as raised:
        validator.validate(ValidateCommand(environments=environments, fail_fast=True))
    assert raised.value.issue == result.issues[0]


@pytest.mark.parametrize("directory_name", (" malformed ", "control\nname"))
def test_whole_project_discovery_types_malformed_environment_directory_issues(
    tmp_path: Path, directory_name: str
) -> None:
    _create_project_with_environment(tmp_path)
    (tmp_path / "deployment" / "environments" / directory_name).mkdir()
    validator = SourceAuthoredSpecificationValidator(tmp_path)

    result = validator.validate(ValidateCommand())

    issue = next(issue for issue in result.issues if issue.subject == ValidationSubject(directory_name))
    assert "EnvironmentId" in issue.message
    with pytest.raises(ValidationFailFastError) as raised:
        validator.validate(ValidateCommand(fail_fast=True))
    assert raised.value.issue == issue


@pytest.mark.skipif(os.name == "nt", reason="authored symlink policy requires POSIX symlinks")
@pytest.mark.parametrize(
    "scenario", ("repository-root", "project-document", "environment", "units", "document", "loop")
)
def test_source_authored_validation_rejects_every_symlink_layer(tmp_path: Path, scenario: str) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _create_project_with_environment(repository)
    validation_root = repository
    environment = repository / "deployment" / "environments" / "dev"

    if scenario == "repository-root":
        validation_root = tmp_path / "repository-link"
        validation_root.symlink_to(repository, target_is_directory=True)
    elif scenario == "project-document":
        project = repository / "gitopsctr.yaml"
        outside = tmp_path / "outside-project.yaml"
        project.rename(outside)
        project.symlink_to(outside)
    elif scenario == "environment":
        outside = tmp_path / "outside-environment"
        environment.rename(outside)
        environment.symlink_to(outside, target_is_directory=True)
    elif scenario == "units":
        outside = tmp_path / "outside-units"
        outside.mkdir()
        (environment / "units").symlink_to(outside, target_is_directory=True)
    elif scenario == "document":
        document = environment / "environment.yaml"
        outside = tmp_path / "outside-environment.yaml"
        document.rename(outside)
        document.symlink_to(outside)
    else:
        document = environment / "environment.yaml"
        document.unlink()
        document.symlink_to("environment.yaml")

    result = SourceAuthoredSpecificationValidator(validation_root).validate(ValidateCommand())

    assert not result.valid
    assert any("symbolic link" in issue.message for issue in result.issues)


@pytest.mark.skipif(os.name == "nt", reason="authored symlink policy requires POSIX symlinks")
def test_source_authored_validation_rejects_safe_in_root_symlink_too(tmp_path: Path) -> None:
    _create_project_with_environment(tmp_path)
    document = tmp_path / "deployment" / "environments" / "dev" / "environment.yaml"
    target = document.with_name("environment-real.yaml")
    document.rename(target)
    document.symlink_to(target.name)

    result = SourceAuthoredSpecificationValidator(tmp_path).validate(
        ValidateCommand(environments=(EnvironmentId("dev"),))
    )

    assert not result.valid
    assert "must not traverse a symbolic link" in result.issues[0].message


@pytest.mark.skipif(os.name == "nt", reason="repository-root symlink policy requires POSIX symlinks")
def test_default_composition_preserves_repository_root_symlink_for_validation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _create_project_with_environment(repository)
    linked_repository = tmp_path / "linked-repository"
    linked_repository.symlink_to(repository, target_is_directory=True)

    with composition.create_default_application(linked_repository) as application:
        result = application.validate(ValidateCommand())

    assert result.issues == (
        ValidationIssue(
            ValidationSubject(str(linked_repository)),
            "authored repository root must not be a symbolic link",
        ),
    )


def test_command_validate_uses_the_composition_root_and_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[ValidateCommand] = []

    application = RecordingApplication(
        ValidationResult(validated_documents=("gitopsctr.yaml",), validated_environments=(EnvironmentId("dev"),)),
        calls,
    )
    monkeypatch.setattr(composition, "create_default_application", lambda repository: application)
    controller.REPOSITORY_ROOT = tmp_path

    controller.command_validate(
        argparse.Namespace(files=["gitopsctr.yaml"], environment=["dev", "dev"], fail_fast=False)
    )

    assert calls == [ValidateCommand(("gitopsctr.yaml",), (EnvironmentId("dev"),))]
    assert application.close_count == 1
    assert "VALID" in capsys.readouterr().err


def test_command_validate_renders_typed_invalid_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    application = RecordingApplication(
        ValidationResult((ValidationIssue(ValidationSubject("gitopsctr.yaml"), "invalid resource"),)), []
    )
    monkeypatch.setattr(composition, "create_default_application", lambda repository: application)
    controller.REPOSITORY_ROOT = tmp_path

    with pytest.raises(OperationError, match="1 error"):
        controller.command_validate(argparse.Namespace(files=[], environment=None, fail_fast=False))

    assert "INVALID" in capsys.readouterr().err
    assert application.close_count == 1


def test_command_validate_rejects_target_traversal_before_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_composition(repository: Path) -> object:
        raise AssertionError("composition must not run for an unsafe logical target")

    monkeypatch.setattr(composition, "create_default_application", unexpected_composition)
    controller.REPOSITORY_ROOT = tmp_path

    with pytest.raises(OperationError, match="logical label"):
        controller.command_validate(argparse.Namespace(files=["../outside.yaml"], environment=None, fail_fast=False))


def test_command_validate_normalizes_absolute_in_root_target_before_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[ValidateCommand] = []
    application = RecordingApplication(ValidationResult(), calls)
    monkeypatch.setattr(composition, "create_default_application", lambda repository: application)
    controller.REPOSITORY_ROOT = tmp_path

    controller.command_validate(
        argparse.Namespace(files=[str(tmp_path / "nested" / "unit.yaml")], environment=None, fail_fast=False)
    )

    assert calls == [ValidateCommand(("nested/unit.yaml",))]


def test_command_validate_rejects_absolute_out_of_root_before_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_composition(repository: Path) -> object:
        raise AssertionError("composition must not run for an out-of-root target")

    monkeypatch.setattr(composition, "create_default_application", unexpected_composition)
    controller.REPOSITORY_ROOT = tmp_path / "repository"

    with pytest.raises(OperationError, match="escapes the repository root"):
        controller.command_validate(
            argparse.Namespace(files=[str(tmp_path / "outside.yaml")], environment=None, fail_fast=False)
        )


def test_command_validate_maps_typed_fail_fast_and_closes_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issue = ValidationIssue(ValidationSubject("broken.yaml"), "bad\n\tparser detail")

    @dataclass
    class FailingApplication(RecordingApplication):
        def validate(self, command: ValidateCommand) -> ValidationResult:
            raise ValidationFailFastError(issue)

    application = FailingApplication(ValidationResult(), [])
    monkeypatch.setattr(composition, "create_default_application", lambda repository: application)
    controller.REPOSITORY_ROOT = tmp_path

    with pytest.raises(OperationError, match="broken.yaml: bad\n\tparser detail"):
        controller.command_validate(argparse.Namespace(files=["broken.yaml"], environment=None, fail_fast=True))
    assert application.close_count == 1


def test_application_modules_do_not_depend_on_paths_git_or_controller() -> None:
    application_root = Path(__file__).parents[1] / "src" / "gitopsctr" / "application"
    forbidden_prefixes = ("pathlib", "gitopsctr.controller", "gitopsctr.git_local", "gitopsctr.adapters")
    for source in application_root.rglob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        imports = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        violations = {
            module
            for module in imports
            if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden_prefixes)
        }
        assert not violations, f"{source} imports backend details: {sorted(violations)}"


def test_source_authored_controller_compatibility_edge_is_single_and_explicit() -> None:
    source_root = Path(__file__).parents[1] / "src" / "gitopsctr"
    edges: list[tuple[str, str]] = []
    for source in source_root.rglob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "gitopsctr.controller":
                edges.extend((source.relative_to(source_root).as_posix(), alias.name) for alias in node.names)
            elif isinstance(node, ast.Import):
                edges.extend(
                    (source.relative_to(source_root).as_posix(), alias.name)
                    for alias in node.names
                    if alias.name == "gitopsctr.controller" or alias.name.startswith("gitopsctr.controller.")
                )
    assert sorted(edges) == [
        ("adapters/source_authored/validation.py", "validate_authored_resources"),
        ("cli.py", "main"),
    ]
    compatibility_source = (source_root / "adapters" / "source_authored" / "validation.py").read_text()
    assert "phase-2 compatibility adapter" in compatibility_source
