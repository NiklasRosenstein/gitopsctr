"""Focused phase-3a tests for the typed ``get`` application vertical."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr import composition, controller
from gitopsctr.adapters.git.status import GitStatusInspector
from gitopsctr.application import (
    DependencyCommand,
    DependencyEntry,
    DependencyResult,
    InspectionOutputFormat,
    InspectionTable,
    ResourceInspectionCommand,
    ResourceInspectionResult,
    SnapshotId,
    StatusCommand,
    StatusEntry,
    StatusResult,
    StatusState,
    ValidationResult,
)
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.snapshots import SnapshotView


@dataclass
class RecordingSnapshotReader:
    close_count: int = 0

    def open_snapshot(self, snapshot_id: SnapshotId) -> SnapshotView:
        raise AssertionError("snapshot inspection is not expected")

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingValidator:
    close_count: int = 0

    def validate(self, _command: object) -> ValidationResult:
        return ValidationResult()

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingInspector:
    result: ResourceInspectionResult
    calls: list[ResourceInspectionCommand]
    close_count: int = 0

    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        self.calls.append(command)
        return self.result

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingStatusInspector:
    result: StatusResult
    calls: list[StatusCommand]
    close_count: int = 0

    def status(self, command: StatusCommand) -> StatusResult:
        self.calls.append(command)
        return self.result

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingDependencyInspector:
    close_count: int = 0

    def dependencies(self, _command: object) -> object:
        raise AssertionError("dependency inspection is not expected")

    def close(self) -> None:
        self.close_count += 1


@dataclass
class DispatchingDependencyInspector:
    result: DependencyResult
    calls: list[DependencyCommand]
    close_count: int = 0

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        self.calls.append(command)
        return self.result

    def close(self) -> None:
        self.close_count += 1


@dataclass
class RecordingApplication:
    result: ResourceInspectionResult
    calls: list[ResourceInspectionCommand]
    close_count: int = 0

    def inspect_resources(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        self.calls.append(command)
        return self.result

    def __enter__(self) -> RecordingApplication:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close_count += 1


@dataclass
class RecordingDependencyApplication:
    result: DependencyResult
    calls: list[DependencyCommand]
    close_count: int = 0

    def dependencies(self, command: DependencyCommand) -> DependencyResult:
        self.calls.append(command)
        return self.result

    def __enter__(self) -> RecordingDependencyApplication:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close_count += 1


def test_application_services_uses_injected_resource_inspector_and_closes_it() -> None:
    expected = ResourceInspectionResult(tables=(InspectionTable(("NAME",), (("dev",),)),))
    inspector = RecordingInspector(expected, [])
    reader = RecordingSnapshotReader()
    validator = RecordingValidator()
    command = ResourceInspectionCommand("environments")

    services = ApplicationServices(
        reader,
        validator,
        inspector,
        RecordingStatusInspector(StatusResult("dev", "desired/dev", None, "observed/dev", None, ()), []),
        RecordingDependencyInspector(),
    )

    assert services.inspect_resources(command) is expected
    assert inspector.calls == [command]
    services.close()
    services.close()
    assert inspector.close_count == 1
    assert reader.close_count == 1
    assert validator.close_count == 1


def test_application_services_uses_closed_status_port_and_closes_it() -> None:
    inspector = RecordingInspector(ResourceInspectionResult(), [])
    status = RecordingStatusInspector(
        StatusResult(
            "dev", "desired/dev", "a" * 40, "observed/dev", None, (StatusEntry("web", StatusState.READY, "new"),)
        ),
        [],
    )
    services = ApplicationServices(
        RecordingSnapshotReader(), RecordingValidator(), inspector, status, RecordingDependencyInspector()
    )
    command = StatusCommand("dev")

    assert services.status(command) is status.result
    assert status.calls == [command]
    services.close()
    assert status.close_count == 1


def test_application_services_dispatches_exact_dependency_command_and_result() -> None:
    expected = DependencyResult(
        "dev",
        "a" * 40,
        SnapshotId("dependency-source"),
        ("consumer",),
        (DependencyEntry("consumer", ()),),
    )
    dependencies = DispatchingDependencyInspector(expected, [])
    services = ApplicationServices(
        RecordingSnapshotReader(),
        RecordingValidator(),
        RecordingInspector(ResourceInspectionResult(), []),
        RecordingStatusInspector(StatusResult("dev", "desired/dev", None, "observed/dev", None, ()), []),
        dependencies,
    )
    command = DependencyCommand("dev", source_selector="source", units=("consumer",))

    assert services.dependencies(command) is expected
    assert dependencies.calls == [command]


def test_application_services_closes_all_five_dependencies_once_after_a_close_failure() -> None:
    class CloseProbe:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.calls = 0

        def close(self) -> None:
            self.calls += 1
            if self.fail:
                raise RuntimeError("dependency close failed")

    reader, validator, inspector, status = (CloseProbe() for _ in range(4))
    dependencies = CloseProbe(fail=True)
    services = ApplicationServices(reader, validator, inspector, status, dependencies)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="dependency close failed"):
        services.close()
    services.close()
    assert [item.calls for item in (reader, validator, inspector, status, dependencies)] == [1, 1, 1, 1, 1]


def test_command_dependencies_dispatches_the_exact_typed_command_and_renders_result(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    result = DependencyResult(
        "dev",
        "a" * 40,
        SnapshotId("dependency-source"),
        ("consumer",),
        (DependencyEntry("base", ()), DependencyEntry("consumer", ("base",))),
    )
    application = RecordingDependencyApplication(result, [])
    monkeypatch.setattr(composition, "create_default_application", lambda _repository: application)
    controller.REPOSITORY_ROOT = tmp_path
    args = controller.build_parser().parse_args(
        ["dependencies", "--environment", "dev", "--source-revision", "custom", "--unit", "consumer", "--depth", "1"]
    )

    controller.command_dependencies(args)

    assert application.calls == [DependencyCommand("dev", "custom", ("consumer",), 1)]
    assert application.close_count == 1
    assert capsys.readouterr().out == "consumer\n└── base\n"


def test_status_entry_rejects_unclosed_string_states() -> None:
    with pytest.raises(TypeError, match="StatusState"):
        StatusEntry("web", "READY", "new")  # type: ignore[arg-type]


def test_git_status_evidence_preserves_glob_pathspecs(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class Completed:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(
        "gitopsctr.adapters.git.status.subprocess.run",
        lambda args, **_kwargs: calls.append(args) or Completed(),
    )
    inspector = GitStatusInspector(tmp_path, object(), object())  # type: ignore[arg-type]

    inspector._source_evidence(
        {"revision": "a" * 40},
        {"revision": "b" * 40, "path": "infra", "inputs": ["*.tf", "README.md"]},
    )

    assert all(":(glob)infra/*.tf" in call for call in calls)
    assert all("infra/README.md" in call for call in calls)


def test_command_get_translates_to_orchestrator_and_renders_its_typed_result(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    expected = ResourceInspectionResult(tables=(InspectionTable(("NAME",), (("dev",),)),))
    application = RecordingApplication(expected, [])
    monkeypatch.setattr(composition, "create_default_application", lambda _repository: application)
    controller.REPOSITORY_ROOT = tmp_path
    args = controller.build_parser().parse_args(["get", "environments"])

    controller.command_get(args)

    assert application.calls == [ResourceInspectionCommand("environments", output=InspectionOutputFormat.TABLE)]
    assert application.close_count == 1
    assert capsys.readouterr().out.splitlines() == ["NAME", "dev"]


def test_get_command_keeps_snapshot_selection_as_backend_neutral_hints() -> None:
    command = ResourceInspectionCommand(
        "units",
        environment="dev",
        desired_reference="desired-channel",
        desired_snapshot="state-at-time-x",
    )

    assert command.desired_reference == "desired-channel"
    assert command.desired_snapshot == "state-at-time-x"


class InventoryHandle:
    """A backend-owned value that must not cross the inspection result port."""


def test_inspection_result_rejects_backend_objects_and_exposes_no_mutable_document() -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        ResourceInspectionResult(document=InventoryHandle())
    with pytest.raises(TypeError, match="JSON-compatible"):
        ResourceInspectionResult(document=object())

    source = {"items": [{"name": "original"}]}
    result = ResourceInspectionResult(document=source)
    source["items"][0]["name"] = "adapter mutation"
    first_read = result.document
    assert first_read == {"items": [{"name": "original"}]}

    assert isinstance(first_read, dict)
    first_read["items"][0]["name"] = "caller mutation"
    assert result.document == {"items": [{"name": "original"}]}


def test_inspection_table_requires_exact_immutable_tuple_storage() -> None:
    with pytest.raises(TypeError, match="headers must be a tuple"):
        InspectionTable(["NAME"], ())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="rows must be a tuple"):
        InspectionTable(("NAME",), [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="header width"):
        InspectionTable(("NAME",), (("one", "two"),))
