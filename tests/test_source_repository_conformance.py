"""Shared conformance checks for exact source acquisition and retention."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr.adapters.git.sources import GitSourceRepository, GitSourceRetentionStore
from gitopsctr.adapters.memory.sources import MemorySourceRepository, MemorySourceRetentionStore
from gitopsctr.application.model import (
    ContentId,
    RetainedSource,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.sources import (
    SourceError,
    SourceNotFoundError,
    SourceRepository,
    SourceRequest,
    SourceRetentionError,
)
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry, WorkspaceImmutableError
from gitopsctr.errors import OperationError
from gitopsctr.git_local import DulwichLocalRepository


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *arguments),
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _workspace(version: bytes) -> InMemoryWorkspace:
    return InMemoryWorkspace(
        [
            WorkspaceEntry.file("application/config.yaml", version),
            WorkspaceEntry.file("bin/run", b"#!/bin/sh\necho ready\n", executable=True),
        ],
        mutable=False,
    )


def _forged_retained_source(
    handle: RetainedSourceHandle,
    retention_store_id: RetentionStoreId,
    source_snapshot_id: SourceSnapshotId,
    content_id: ContentId,
) -> RetainedSource:
    """Make a value shaped like a retained source but without issuance proof."""

    forged = object.__new__(RetainedSource)
    object.__setattr__(forged, "handle", handle)
    object.__setattr__(forged, "retention_store_id", retention_store_id)
    object.__setattr__(forged, "source_snapshot_id", source_snapshot_id)
    object.__setattr__(forged, "content_id", content_id)
    object.__setattr__(forged, "_issuance", object())
    return forged


@dataclass
class SourceFixture:
    repository: SourceRepository
    source_id: SourceId
    source_request: Callable[[str], SourceRequest]
    move_current: Callable[[str], None]
    remove_source: Callable[[], None]
    make_dirty: Callable[[], None]
    reopen: Callable[[], SourceRepository]
    close: Callable[[], None]


@pytest.fixture(params=("memory", "git"), ids=("memory", "git"))
def source_repository(request: pytest.FixtureRequest, tmp_path: Path) -> SourceFixture:
    source_id = SourceId(f"{request.param}-authored-source")
    if request.param == "memory":
        repository = MemorySourceRepository(source_id)
        first = repository.install(SnapshotId("first"), _workspace(b"version: 1\n"))
        second = repository.install(SnapshotId("second"), _workspace(b"version: 2\n"))
        repository.set_selector("current", first.source_snapshot_id.snapshot_id)
        repository.set_selector("historical", first.source_snapshot_id.snapshot_id)
        repository.set_selector("v1", first.source_snapshot_id.snapshot_id)
        repository.set_selector("private", second.source_snapshot_id.snapshot_id)

        def move_current(selector: str) -> None:
            selected = {"first": first, "second": second}[selector]
            repository.set_selector("current", selected.source_snapshot_id.snapshot_id)

        def remove_source() -> None:
            repository.remove_selector("current")
            repository.remove_snapshot(first.source_snapshot_id.snapshot_id)

        fixture = SourceFixture(
            repository,
            source_id,
            lambda selector: SourceRequest(source_id, selector),
            move_current,
            remove_source,
            lambda: None,
            lambda: MemorySourceRepository(source_id, repository.retention_store),
            repository.close,
        )
    else:
        source_root = tmp_path / "source"
        retention_root = tmp_path / "retention"
        source_root.mkdir()
        retention_root.mkdir()
        _git(source_root, "init", "-b", "main")
        (source_root / "application").mkdir()
        (source_root / "application" / "config.yaml").write_bytes(b"version: 1\n")
        (source_root / "bin").mkdir()
        program = source_root / "bin" / "run"
        program.write_bytes(b"#!/bin/sh\necho ready\n")
        program.chmod(0o755)
        _git(source_root, "add", ".")
        _git(source_root, "commit", "-m", "first")
        first = _git(source_root, "rev-parse", "HEAD")
        _git(source_root, "tag", "v1", first)
        _git(source_root, "branch", "current", first)
        (source_root / "application" / "config.yaml").write_bytes(b"version: 2\n")
        _git(source_root, "add", ".")
        _git(source_root, "commit", "-m", "second")
        second = _git(source_root, "rev-parse", "HEAD")
        _git(source_root, "branch", "private", second)
        repository = GitSourceRepository.from_path(source_id, source_root, retention_root)

        def move_current(selector: str) -> None:
            revision = {"first": first, "second": second}[selector]
            _git(source_root, "branch", "-f", "current", revision)
            repository.repository.refresh()

        def remove_source() -> None:
            _git(source_root, "branch", "-D", "current")
            repository.repository.refresh()

        def make_dirty() -> None:
            (source_root / "application" / "config.yaml").write_bytes(b"dirty worktree only\n")

        fixture = SourceFixture(
            repository,
            source_id,
            lambda selector: SourceRequest(
                source_id,
                {"current": "current", "historical": first, "v1": "v1", "private": "private"}.get(selector, selector),
            ),
            move_current,
            remove_source,
            make_dirty,
            lambda: GitSourceRepository.from_path(source_id, source_root, retention_root),
            repository.close,
        )
    yield fixture
    fixture.close()


def test_source_selectors_resolve_once_to_exact_immutable_logical_content(source_repository: SourceFixture) -> None:
    source = source_repository.repository.resolve(source_repository.source_request("current"))
    source_repository.make_dirty()
    source_repository.move_current("second")

    assert source.workspace.read("application/config.yaml") == b"version: 1\n"
    assert source.workspace.get_entry("bin/run").executable
    assert not source.workspace.is_mutable
    with pytest.raises(WorkspaceImmutableError):
        source.workspace.write("unexpected", b"mutation")  # type: ignore[attr-defined]

    moved = source_repository.repository.resolve(source_repository.source_request("current"))
    assert moved.content_id != source.content_id
    assert moved.workspace.read("application/config.yaml") == b"version: 2\n"


def test_current_historical_tag_private_and_missing_selection_are_adapter_neutral(
    source_repository: SourceFixture,
) -> None:
    current = source_repository.repository.resolve(source_repository.source_request("current"))
    historical = source_repository.repository.resolve(source_repository.source_request("historical"))
    tag = source_repository.repository.resolve(source_repository.source_request("v1"))
    private = source_repository.repository.resolve(source_repository.source_request("private"))

    assert current.content_id == historical.content_id == tag.content_id
    assert private.content_id != current.content_id
    with pytest.raises(SourceNotFoundError):
        source_repository.repository.resolve(source_repository.source_request("missing"))
    with pytest.raises(SourceNotFoundError):
        source_repository.repository.resolve(SourceRequest(SourceId("other-source"), "current"))


def test_retention_recovers_exact_payload_after_original_selector_and_source_disappear(
    source_repository: SourceFixture,
) -> None:
    source = source_repository.repository.resolve(source_repository.source_request("current"))
    retained = source_repository.repository.retain(source)
    source_repository.remove_source()

    with pytest.raises(SourceNotFoundError):
        source_repository.repository.resolve(source_repository.source_request("current"))
    source_repository.close()
    reopened = source_repository.reopen()
    recovered = reopened.recover(retained)
    reopened.close()
    assert recovered.source_snapshot_id == source.source_snapshot_id
    assert recovered.content_id == source.content_id
    assert recovered.workspace.list_entries() == source.workspace.list_entries()


def test_selector_aba_does_not_alter_an_already_resolved_or_retained_snapshot(source_repository: SourceFixture) -> None:
    first = source_repository.repository.resolve(source_repository.source_request("current"))
    retained = source_repository.repository.retain(first)
    source_repository.move_current("second")
    second = source_repository.repository.resolve(source_repository.source_request("current"))
    source_repository.move_current("first")
    again = source_repository.repository.resolve(source_repository.source_request("current"))

    assert first.content_id != second.content_id
    assert first.content_id == again.content_id
    assert source_repository.repository.recover(retained).content_id == first.content_id


def test_retention_handles_and_release_validate_exact_ownership(source_repository: SourceFixture) -> None:
    source = source_repository.repository.resolve(source_repository.source_request("current"))
    retained = source_repository.repository.retain(source)
    tampered = _forged_retained_source(
        retained.handle,
        retained.retention_store_id,
        SourceSnapshotId(source_repository.source_id, SnapshotId("different-source-snapshot")),
        retained.content_id,
    )
    foreign = _forged_retained_source(
        RetainedSourceHandle("foreign-retention-handle"),
        retained.retention_store_id,
        retained.source_snapshot_id,
        retained.content_id,
    )

    for invalid in (tampered, foreign):
        with pytest.raises(SourceRetentionError):
            source_repository.repository.recover(invalid)
        with pytest.raises(SourceRetentionError):
            source_repository.repository.release(invalid)

    source_repository.repository.release(retained)
    with pytest.raises(SourceRetentionError):
        source_repository.repository.recover(retained)
    with pytest.raises(SourceRetentionError):
        source_repository.repository.release(retained)


def test_retention_store_scope_prevents_same_source_snapshot_handle_confusion() -> None:
    source_id = SourceId("same-source")
    first_store = MemorySourceRetentionStore()
    second_store = MemorySourceRetentionStore()
    first_repository = MemorySourceRepository(source_id, first_store)
    second_repository = MemorySourceRepository(source_id, second_store)
    first = first_repository.install(SnapshotId("same-snapshot"), _workspace(b"first payload"))
    second = second_repository.install(SnapshotId("same-snapshot"), _workspace(b"second payload"))

    first_retained = first_repository.retain(first)
    second_retained = second_repository.retain(second)

    assert first_retained.handle != second_retained.handle
    assert first_retained.retention_store_id != second_retained.retention_store_id
    with pytest.raises(SourceRetentionError):
        second_repository.recover(first_retained)
    assert first_repository.recover(first_retained).workspace.read("application/config.yaml") == b"first payload"
    assert second_repository.recover(second_retained).workspace.read("application/config.yaml") == b"second payload"

    other_source_repository = MemorySourceRepository(SourceId("other-source"), first_store)
    other = other_source_repository.install(SnapshotId("same-snapshot"), _workspace(b"other payload"))
    other_retained = other_source_repository.retain(other)
    with pytest.raises(SourceRetentionError):
        first_repository.recover(other_retained)


def test_concurrent_release_is_typed_and_never_leaks_a_mapping_error(source_repository: SourceFixture) -> None:
    source = source_repository.repository.resolve(source_repository.source_request("current"))
    retained = source_repository.repository.retain(source)

    def release() -> SourceRetentionError | None:
        try:
            source_repository.repository.release(retained)
        except SourceRetentionError as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _unused: release(), range(2)))

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, SourceRetentionError) for result in results) == 1


def test_git_source_object_failure_after_resolution_is_not_mislabeled_as_missing_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    retention_root = tmp_path / "retention"
    source_root.mkdir()
    retention_root.mkdir()
    _git(source_root, "init", "-b", "main")
    (source_root / "state").write_text("ready\n")
    _git(source_root, "add", "state")
    _git(source_root, "commit", "-m", "source")
    repository = GitSourceRepository.from_path(SourceId("git-source"), source_root, retention_root)

    def unavailable_tree(_revision: str) -> tuple[object, ...]:
        raise OperationError("object vanished after ref resolution")

    monkeypatch.setattr(repository.repository, "tree_entries", unavailable_tree)

    with pytest.raises(SourceError) as raised:
        repository.resolve(SourceRequest(SourceId("git-source"), "main"))
    assert not isinstance(raised.value, SourceNotFoundError)


def test_git_ref_to_a_real_missing_object_is_a_source_error_not_a_missing_selector(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    retention_root = tmp_path / "retention"
    source_root.mkdir()
    retention_root.mkdir()
    _git(source_root, "init", "-b", "main")
    (source_root / "state").write_text("ready\n")
    _git(source_root, "add", "state")
    _git(source_root, "commit", "-m", "source")
    dangling_ref = source_root / ".git" / "refs" / "heads" / "dangling"
    dangling_ref.write_text(f"{'0' * 40}\n")
    repository = GitSourceRepository.from_path(SourceId("git-source"), source_root, retention_root)

    with pytest.raises(SourceError) as raised:
        repository.resolve(SourceRequest(SourceId("git-source"), "dangling"))
    assert not isinstance(raised.value, SourceNotFoundError)


def test_git_retention_reissues_from_locator_in_a_fresh_process_after_source_is_renamed(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    retention_root = tmp_path / "retention"
    source_root.mkdir()
    retention_root.mkdir()
    _git(source_root, "init", "-b", "main")
    (source_root / "state").write_bytes(b"durable retained payload\n")
    _git(source_root, "add", "state")
    _git(source_root, "commit", "-m", "source")

    issue = """
import sys
from pathlib import Path
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application.model import SourceId
from gitopsctr.application.sources import RetainedSourceLocator, SourceRequest
source = Path(sys.argv[1])
retention = Path(sys.argv[2])
repository = GitSourceRepository.from_path(SourceId('subprocess-source'), source, retention)
retained = repository.retain(repository.resolve(SourceRequest(SourceId('subprocess-source'), 'main')))
print(RetainedSourceLocator.from_retained(retained).to_wire())
"""
    locator = subprocess.run(
        (sys.executable, "-c", issue, str(source_root), str(retention_root)),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    source_root.rename(tmp_path / "source-removed")

    recover = """
import base64
import sys
from pathlib import Path
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application.model import SourceId
from gitopsctr.application.sources import RetainedSourceLocator
source = Path(sys.argv[1])
retention = Path(sys.argv[2])
locator = RetainedSourceLocator.from_wire(sys.stdin.read())
repository = GitSourceRepository.from_path(SourceId('subprocess-source'), source, retention)
retained = repository.reissue(locator)
print(base64.b64encode(repository.recover(retained).workspace.read('state')).decode('ascii'))
"""
    payload = subprocess.run(
        (sys.executable, "-c", recover, str(source_root), str(retention_root)),
        input=locator,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert payload == "ZHVyYWJsZSByZXRhaW5lZCBwYXlsb2FkCg=="


def test_git_retention_root_must_not_be_inside_the_source_repository(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")

    with pytest.raises(ValueError, match="independently owned"):
        GitSourceRepository.from_path(SourceId("git-source"), tmp_path, tmp_path / "retention")


def test_git_source_rejects_an_injected_store_root_that_differs_from_its_retention_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    retention_root = tmp_path / "retention"
    source_root.mkdir()
    retention_root.mkdir()
    _git(source_root, "init", "-b", "main")

    with pytest.raises(ValueError, match="exactly match"):
        GitSourceRepository(
            SourceId("git-source"),
            DulwichLocalRepository(source_root),
            retention_root,
            GitSourceRetentionStore(source_root / "malicious-store"),
        )
