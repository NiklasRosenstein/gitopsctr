"""Focused local-Git snapshot adapter behavior."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from dulwich.objects import Blob, Commit, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo

from gitopsctr.adapters.git.snapshots import GitSnapshotEntryError, GitSnapshotReader
from gitopsctr.application.model import SnapshotId
from gitopsctr.application.snapshots import SnapshotNotFoundError, SnapshotReadError
from gitopsctr.application.workspace import WorkspaceEntryKind


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *arguments),
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _raw_tree_reader(
    tmp_path: Path, path: bytes, mode: int, *, add_blob: bool = True
) -> tuple[GitSnapshotReader, SnapshotId]:
    """Build one minimally formed commit, including tree spellings a worktree rejects."""

    repository = Repo.init(str(tmp_path))
    blob = Blob.from_string(b"content")
    if add_blob:
        repository.object_store.add_object(blob)
    tree = Tree()
    tree.add(path, mode, blob.id)
    repository.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"test <test@example.invalid>"
    commit.author_time = commit.commit_time = 0
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"raw tree"
    repository.object_store.add_object(commit)
    assert repository.refs.set_if_equals(Ref(b"refs/heads/main"), None, commit.id)
    repository.close()
    reader = GitSnapshotReader.from_path(tmp_path)
    return reader, SnapshotId(f"git-commit:{commit.id.decode()}")


def test_git_reader_keeps_an_exact_commit_open_after_the_branch_moves(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    path = tmp_path / "state"
    path.write_bytes(b"first")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-m", "first")
    with GitSnapshotReader.from_path(tmp_path) as reader:
        snapshot_id = reader.snapshot_id_for_revision("main")

        path.write_bytes(b"second")
        _git(tmp_path, "add", "state")
        _git(tmp_path, "commit", "-m", "second")

        view = reader.open_snapshot(snapshot_id)
        assert view.workspace.read("state") == b"first"


def test_git_reader_preserves_regular_file_bytes_and_executable_mode(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    program = tmp_path / "program"
    program.write_bytes(b"\x00binary\xff\n")
    program.chmod(0o755)
    _git(tmp_path, "add", "program")
    _git(tmp_path, "commit", "-m", "program")
    with GitSnapshotReader.from_path(tmp_path) as reader:
        view = reader.open_snapshot(reader.snapshot_id_for_revision("HEAD"))
        entry = view.workspace.get_entry("program")
        assert entry.kind is WorkspaceEntryKind.FILE
        assert entry.executable
        assert view.workspace.read("program") == b"\x00binary\xff\n"


@pytest.mark.skipif(os.name == "nt", reason="Git symlink fixture requires POSIX symlink support")
def test_git_reader_rejects_symbolic_links_until_the_workspace_contract_supports_them(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "target").write_text("target")
    (tmp_path / "link").symlink_to("target")
    _git(tmp_path, "add", "target", "link")
    _git(tmp_path, "commit", "-m", "link")
    with GitSnapshotReader.from_path(tmp_path) as reader:
        with pytest.raises(GitSnapshotEntryError, match="symbolic link"):
            reader.open_snapshot(reader.snapshot_id_for_revision("HEAD"))


def test_git_reader_rejects_a_gitlink_without_reading_its_unavailable_target(tmp_path: Path) -> None:
    reader, snapshot_id = _raw_tree_reader(tmp_path, b"nested-repository", 0o160000, add_blob=False)
    try:
        entry = reader.repository.tree_entries(snapshot_id.value.removeprefix("git-commit:"))[0]
        assert entry.data is None
        with pytest.raises(GitSnapshotEntryError, match="unsupported entry"):
            reader.open_snapshot(snapshot_id)
    finally:
        reader.close()


def test_git_reader_maps_a_missing_required_regular_file_blob_to_content_error(tmp_path: Path) -> None:
    reader, snapshot_id = _raw_tree_reader(tmp_path, b"missing-blob", stat.S_IFREG | 0o644, add_blob=False)
    try:
        with pytest.raises(GitSnapshotEntryError, match="content cannot be represented"):
            reader.open_snapshot(snapshot_id)
    finally:
        reader.close()


@pytest.mark.parametrize("path", [b"\xff", b"e\xcc\x81", b"bad\x1fname", b"../escape", b"bad\x00name"])
def test_git_reader_rejects_noncanonical_or_non_utf8_git_paths(tmp_path: Path, path: bytes) -> None:
    reader, snapshot_id = _raw_tree_reader(tmp_path, path, stat.S_IFREG | 0o644)
    try:
        with pytest.raises(GitSnapshotEntryError):
            reader.open_snapshot(snapshot_id)
    finally:
        reader.close()


def test_git_reader_distinguishes_an_unavailable_well_formed_commit_id(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    with GitSnapshotReader.from_path(tmp_path) as reader:
        with pytest.raises(SnapshotNotFoundError):
            reader.open_snapshot(SnapshotId(f"git-commit:{'0' * 40}"))


def test_git_reader_does_not_misclassify_an_invalid_repository_as_a_missing_snapshot(tmp_path: Path) -> None:
    with GitSnapshotReader.from_path(tmp_path) as reader:
        with pytest.raises(SnapshotReadError, match="repository cannot be opened") as raised:
            reader.open_snapshot(SnapshotId(f"git-commit:{'1' * 40}"))

    assert not isinstance(raised.value, SnapshotNotFoundError)


def test_git_reader_opens_fresh_immutable_views_with_stable_content_identity(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "state").write_bytes(b"content")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-m", "state")
    with GitSnapshotReader.from_path(tmp_path) as reader:
        snapshot_id = reader.snapshot_id_for_revision("HEAD")
        first = reader.open_snapshot(snapshot_id)
        second = reader.open_snapshot(snapshot_id)
        assert first.workspace is not second.workspace
        assert first.content_id == second.content_id
        reader.close()
        reader.close()
