"""Shared conformance checks for immutable snapshot readers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr.adapters.git.snapshots import GitSnapshotReader
from gitopsctr.adapters.memory.snapshots import InMemorySnapshotStore
from gitopsctr.application.inspection import ResourceInspectionCommand, ResourceInspectionResult
from gitopsctr.application.model import (
    ChannelId,
    SnapshotId,
    SnapshotInspectionCommand,
    ValidateCommand,
    ValidationResult,
)
from gitopsctr.application.ports import SnapshotReader
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.snapshots import SnapshotNotFoundError
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry, WorkspaceImmutableError


@dataclass(frozen=True)
class SnapshotReaderFixture:
    reader: SnapshotReader
    snapshot_id: SnapshotId
    expected: InMemoryWorkspace

    def open_snapshot(self, snapshot_id: SnapshotId):
        return self.reader.open_snapshot(snapshot_id)


class NoopSpecificationValidator:
    def validate(self, command: ValidateCommand) -> ValidationResult:
        return ValidationResult()

    def close(self) -> None:
        """No resources are owned."""


class NoopResourceInspector:
    def inspect(self, command: ResourceInspectionCommand) -> ResourceInspectionResult:
        raise AssertionError("resource inspection is not expected")

    def close(self) -> None:
        """No resources are owned."""


class NoopStatusInspector:
    def status(self, _command: object) -> object:
        raise AssertionError("status inspection is not expected")

    def close(self) -> None:
        """No resources are owned."""


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *arguments),
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture(params=("memory", "git"), ids=("memory", "git"))
def snapshot_reader(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    expected = InMemoryWorkspace(
        [
            WorkspaceEntry.file("application/config.yaml", b"version: 1\n"),
            WorkspaceEntry.file("bin/run", b"#!/bin/sh\necho ready\n", executable=True),
        ]
    )
    if request.param == "memory":
        store = InMemorySnapshotStore()
        snapshot_id = SnapshotId("memory-snapshot-one")
        store.install(snapshot_id, expected)
        fixture = SnapshotReaderFixture(store, snapshot_id, expected)
    else:
        _git(tmp_path, "init", "-b", "main")
        (tmp_path / "application").mkdir()
        (tmp_path / "application" / "config.yaml").write_bytes(b"version: 1\n")
        (tmp_path / "bin").mkdir()
        executable = tmp_path / "bin" / "run"
        executable.write_bytes(b"#!/bin/sh\necho ready\n")
        executable.chmod(0o755)
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "snapshot")
        reader = GitSnapshotReader.from_path(tmp_path)
        fixture = SnapshotReaderFixture(reader, reader.snapshot_id_for_revision("HEAD"), expected)

    yield fixture
    if isinstance(fixture.reader, GitSnapshotReader):
        fixture.reader.close()


def test_opened_snapshot_is_exact_immutable_logical_content(snapshot_reader: SnapshotReaderFixture) -> None:
    view = snapshot_reader.open_snapshot(snapshot_reader.snapshot_id)

    assert view.snapshot_id == snapshot_reader.snapshot_id
    assert view.content_id == snapshot_reader.expected.content_id
    assert view.workspace.content_id == snapshot_reader.expected.content_id
    assert view.workspace.list_entries() == snapshot_reader.expected.list_entries()
    assert not view.workspace.is_mutable
    with pytest.raises(WorkspaceImmutableError):
        view.workspace.write("unexpected", b"mutation")  # type: ignore[attr-defined]


def test_missing_snapshot_fails_closed(snapshot_reader: SnapshotReaderFixture) -> None:
    with pytest.raises(SnapshotNotFoundError):
        snapshot_reader.open_snapshot(SnapshotId("missing-snapshot"))


def test_application_inspection_uses_the_injected_snapshot_reader(snapshot_reader: SnapshotReaderFixture) -> None:
    with ApplicationServices(
        snapshot_reader.reader,
        NoopSpecificationValidator(),
        NoopResourceInspector(),
        NoopStatusInspector(),
    ) as services:
        result = services.inspect_snapshot(SnapshotInspectionCommand(snapshot_reader.snapshot_id))

    assert result.snapshot_id == snapshot_reader.snapshot_id
    assert result.content_id == snapshot_reader.expected.content_id


def test_in_memory_head_observation_incarnations_distinguish_absence_and_aba() -> None:
    """This proves observation identity only; stale-CAS is a later port contract."""

    store = InMemorySnapshotStore()
    first = SnapshotId("state-a")
    second = SnapshotId("state-b")
    store.install(first, InMemoryWorkspace([WorkspaceEntry.file("value", b"a")]))
    store.install(second, InMemoryWorkspace([WorkspaceEntry.file("value", b"b")]))
    channel = ChannelId("desired-production")

    absent = store.resolve_head(channel)
    head_a = store.set_head(channel, first)
    head_b = store.set_head(channel, second)
    head_a_again = store.set_head(channel, first)
    absent_again = store.clear_head(channel)

    assert absent.is_absent
    assert absent_again.is_absent
    assert [head.incarnation for head in (absent, head_a, head_b, head_a_again, absent_again)] == [
        "memory:0",
        "memory:1",
        "memory:2",
        "memory:3",
        "memory:4",
    ]
    assert head_a.snapshot_id == head_a_again.snapshot_id == first
    assert head_a != head_a_again


def test_in_memory_install_copies_content_before_the_source_workspace_mutates() -> None:
    store = InMemorySnapshotStore()
    source = InMemoryWorkspace([WorkspaceEntry.file("state", b"first")])
    snapshot_id = SnapshotId("installed-before-mutation")

    installed = store.install(snapshot_id, source)
    source.write("state", b"second")

    reopened = store.open_snapshot(snapshot_id)
    assert installed.workspace.read("state") == b"first"
    assert reopened.workspace.read("state") == b"first"
    assert reopened.content_id != source.content_id
