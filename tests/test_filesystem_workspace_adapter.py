from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import gitopsctr.adapters.filesystem.workspace as filesystem_workspace
from gitopsctr.adapters.filesystem import FilesystemWorkspaceAdapter, FilesystemWorkspaceError
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceCapabilities, WorkspaceEntry


def immutable(*entries: WorkspaceEntry) -> InMemoryWorkspace:
    return InMemoryWorkspace(entries, mutable=False)


def test_round_trip_preserves_bytes_executable_directories_and_safe_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "bin").mkdir()
    (source / "bin/run").write_bytes(b"#!/bin/sh\necho workspace\n")
    (source / "bin/run").chmod(0o755)
    (source / "links").mkdir()
    (source / "links/run").symlink_to("../bin/run")
    destination = tmp_path / "destination"

    adapter = FilesystemWorkspaceAdapter()
    workspace = adapter.read(source)
    adapter.materialize(workspace, destination)
    reread = adapter.read(destination)

    assert workspace.content_id == reread.content_id
    assert reread.read("bin/run") == b"#!/bin/sh\necho workspace\n"
    assert reread.get_entry("bin/run").executable
    assert reread.get_entry("empty") == WorkspaceEntry.directory("empty")
    assert reread.get_entry("links/run") == WorkspaceEntry.symlink("links/run", "../bin/run")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_materialize_leaves_only_the_exact_requested_name_in_parent(tmp_path: Path) -> None:
    before = {path.name for path in tmp_path.iterdir()}
    destination = tmp_path / "published"

    FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"value")), destination)

    assert {path.name for path in tmp_path.iterdir()} == {*before, "published"}
    assert (destination / "payload").read_bytes() == b"value"


def test_read_rejects_a_symbolic_link_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(source, target_is_directory=True)

    with pytest.raises(FilesystemWorkspaceError, match="root.*symbolic link"):
        FilesystemWorkspaceAdapter().read(root_link)


def test_read_rejects_a_preexisting_nonfinal_symlink(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    (real_parent / "nested/source").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(FilesystemWorkspaceError, match="root.*symbolic link"):
        FilesystemWorkspaceAdapter().read(alias / "nested/source")


@pytest.mark.parametrize("target", ["/outside", "../../outside"])
def test_read_rejects_escaping_symlink_targets(tmp_path: Path, target: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested/link").symlink_to(target)

    with pytest.raises(FilesystemWorkspaceError, match="unsafe symbolic link"):
        FilesystemWorkspaceAdapter().read(source)


def test_read_rejects_symlink_cycles(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "first").symlink_to("second")
    (source / "second").symlink_to("first")

    with pytest.raises(FilesystemWorkspaceError, match="cycle"):
        FilesystemWorkspaceAdapter().read(source)


def test_read_rejects_fifo_when_supported(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform does not support FIFOs")
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")

    with pytest.raises(FilesystemWorkspaceError, match="unsupported"):
        FilesystemWorkspaceAdapter().read(source)


@pytest.mark.parametrize("link_location", ["external", "in-tree"])
def test_read_rejects_regular_file_hardlinks(tmp_path: Path, link_location: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "original"
    original.write_bytes(b"shared inode")
    os.link(original, tmp_path / "external-link" if link_location == "external" else source / "internal-link")

    with pytest.raises(FilesystemWorkspaceError, match="hardlinked"):
        FilesystemWorkspaceAdapter().read(source)


def test_materialize_requires_a_nonexistent_destination(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "keep").write_bytes(b"keep")

    with pytest.raises(FilesystemWorkspaceError, match="must not exist"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("new", b"new")), destination)
    assert (destination / "keep").read_bytes() == b"keep"


def test_materialize_rejects_a_symbolic_link_destination_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "destination"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FilesystemWorkspaceError, match="must not exist"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert list(outside.iterdir()) == []


def test_materialize_rejects_mutable_workspaces_before_creating_destination(tmp_path: Path) -> None:
    destination = tmp_path / "destination"

    with pytest.raises(FilesystemWorkspaceError, match="immutable"):
        FilesystemWorkspaceAdapter().materialize(
            InMemoryWorkspace([WorkspaceEntry.file("new", b"not-written")]), destination
        )
    assert not destination.exists()


def test_adapter_fails_closed_without_required_secure_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(filesystem_workspace, "_MISSING_SECURE_PRIMITIVES", ("O_NOFOLLOW",))

    with pytest.raises(FilesystemWorkspaceError, match="lacks required.*O_NOFOLLOW"):
        FilesystemWorkspaceAdapter().read(source)


def test_read_detects_root_replacement_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    detached = tmp_path / "detached"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == source.name and dir_fd is not None and not swapped:
            swapped = True
            source.rename(detached)
            replacement.rename(source)
        return descriptor

    monkeypatch.setattr(filesystem_workspace.os, "open", racing_open)

    with pytest.raises(FilesystemWorkspaceError, match="changed"):
        FilesystemWorkspaceAdapter().read(source)


def test_read_detects_nested_parent_replacement_and_never_reads_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "nested"
    nested.mkdir()
    (nested / "inside").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"outside")
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "nested" and dir_fd is not None and not swapped:
            swapped = True
            nested.rename(source / "detached")
            nested.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem_workspace.os, "open", racing_open)

    with pytest.raises(FilesystemWorkspaceError, match="changed"):
        FilesystemWorkspaceAdapter().read(source)


def test_read_reverifies_full_chain_after_late_ancestor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "ancestor"
    source = ancestor / "source"
    source.mkdir(parents=True)
    detached = tmp_path / "detached"
    real_read_directory = filesystem_workspace._read_directory
    swapped = False

    def read_then_replace(directory_fd: int, prefix: str, entries: list[WorkspaceEntry]) -> None:
        nonlocal swapped
        real_read_directory(directory_fd, prefix, entries)
        if not swapped:
            swapped = True
            ancestor.rename(detached)
            ancestor.mkdir()

    monkeypatch.setattr(filesystem_workspace, "_read_directory", read_then_replace)

    with pytest.raises(FilesystemWorkspaceError, match="root changed.*ancestor"):
        FilesystemWorkspaceAdapter().read(source)


def test_materialize_round_trip_canonicalizes_implicit_and_explicit_parents(tmp_path: Path) -> None:
    capabilities = WorkspaceCapabilities(symlinks=True, explicit_directories=True, executable_mode=True)
    implicit = InMemoryWorkspace(
        [WorkspaceEntry.file("parent/child", b"payload")], capabilities=capabilities, mutable=False
    )
    explicit = InMemoryWorkspace(
        [WorkspaceEntry.directory("parent"), WorkspaceEntry.file("parent/child", b"payload")],
        capabilities=capabilities,
        mutable=False,
    )
    destination = tmp_path / "destination"

    adapter = FilesystemWorkspaceAdapter()
    adapter.materialize(implicit, destination)
    reread = adapter.read(destination)

    assert implicit.content_id == explicit.content_id == reread.content_id
    assert reread.get_entry("parent") == WorkspaceEntry.directory("parent")


def test_symlink_resolution_handles_prefix_chains_dangling_and_directory_targets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "real").mkdir()
    (source / "real/payload").write_bytes(b"payload")
    (source / "middle").symlink_to("real", target_is_directory=True)
    (source / "start").symlink_to("middle/payload")
    (source / "dangling").symlink_to("missing/payload")
    (source / "directory").symlink_to("real", target_is_directory=True)

    workspace = FilesystemWorkspaceAdapter().read(source)

    assert workspace.get_entry("start") == WorkspaceEntry.symlink("start", "middle/payload")
    assert workspace.get_entry("dangling") == WorkspaceEntry.symlink("dangling", "missing/payload")
    assert workspace.get_entry("directory") == WorkspaceEntry.symlink("directory", "real")


def test_symlink_resolution_rejects_prefix_cycle_with_an_implied_directory(tmp_path: Path) -> None:
    workspace = immutable(
        WorkspaceEntry.symlink("start", "dir/link"),
        WorkspaceEntry.symlink("dir/link", "../start"),
    )
    destination = tmp_path / "destination"

    with pytest.raises(FilesystemWorkspaceError, match="cycle"):
        FilesystemWorkspaceAdapter().materialize(workspace, destination)
    assert not destination.exists()


def test_materialize_normalizes_file_and_directory_modes(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    workspace = immutable(
        WorkspaceEntry.directory("directory"),
        WorkspaceEntry.file("directory/plain", b"plain"),
        WorkspaceEntry.file("directory/run", b"run", executable=True),
    )

    FilesystemWorkspaceAdapter().materialize(workspace, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "directory").stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / "directory/plain").stat().st_mode) == 0o644
    assert stat.S_IMODE((destination / "directory/run").stat().st_mode) == 0o755


def test_materialize_rejects_an_unsafe_immediate_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe-parent"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    destination = parent / "destination"

    with pytest.raises(FilesystemWorkspaceError, match="must not be group- or world-writable"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert list(parent.iterdir()) == []


def test_destination_anchor_verification_failure_closes_the_full_descriptor_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("platform does not expose process file descriptors")
    destination = tmp_path / "nested/destination"
    destination.parent.mkdir()
    before = len(list(descriptor_directory.iterdir()))

    def reject_anchor(_anchor: filesystem_workspace._DestinationAnchor) -> None:
        raise FilesystemWorkspaceError("injected outer anchor verification failure")

    monkeypatch.setattr(filesystem_workspace, "_verify_anchor_chain", reject_anchor)

    for _attempt in range(32):
        with pytest.raises(FilesystemWorkspaceError, match="injected outer anchor"):
            FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert len(list(descriptor_directory.iterdir())) == before


@pytest.mark.parametrize("operation", ["mkdir", "write", "fchmod", "symlink"])
def test_prepublish_failure_removes_private_stage_and_leaves_destination_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    destination = tmp_path / "destination"
    workspace = immutable(
        WorkspaceEntry.directory("nested"),
        WorkspaceEntry.file("nested/payload", b"payload"),
        WorkspaceEntry.symlink("link", "nested/payload"),
    )
    names_before = {path.name for path in tmp_path.iterdir()}
    real_function = getattr(os, operation)
    fchmod_calls = 0

    def injected_failure(*args: object, **kwargs: object) -> object:
        nonlocal fchmod_calls
        if operation == "mkdir" and args[0] == "nested":
            raise OSError("injected mkdir failure")
        if operation == "symlink":
            raise OSError("injected symlink failure")
        result = real_function(*args, **kwargs)
        if operation == "write":
            raise OSError("injected write failure")
        if operation == "fchmod":
            fchmod_calls += 1
            if fchmod_calls == 3:
                raise OSError("injected fchmod failure")
        return result

    monkeypatch.setattr(filesystem_workspace.os, operation, injected_failure)

    with pytest.raises(FilesystemWorkspaceError):
        FilesystemWorkspaceAdapter().materialize(workspace, destination)
    assert not destination.exists()
    assert {path.name for path in tmp_path.iterdir()} == names_before


def test_post_leaf_mutation_is_detected_before_publish_and_stage_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    names_before = {path.name for path in tmp_path.iterdir()}
    real_verify = filesystem_workspace._verify_created_entries

    def mutate_then_verify(directory_fds: dict[str, int], journal: list[filesystem_workspace._CreationRecord]) -> None:
        descriptor = os.open("leaf", os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fds[""])
        try:
            os.write(descriptor, b"tampered")
        finally:
            os.close(descriptor)
        real_verify(directory_fds, journal)

    monkeypatch.setattr(filesystem_workspace, "_verify_created_entries", mutate_then_verify)

    with pytest.raises(FilesystemWorkspaceError, match="changed"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("leaf", b"expected")), destination)
    assert not destination.exists()
    assert {path.name for path in tmp_path.iterdir()} == names_before


def test_destination_appearing_as_symlink_before_atomic_install_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_install = filesystem_workspace._atomic_install

    def race_install(
        anchor: filesystem_workspace._DestinationAnchor, stage: filesystem_workspace._PrivateStage
    ) -> None:
        destination.symlink_to(outside, target_is_directory=True)
        real_install(anchor, stage)

    monkeypatch.setattr(filesystem_workspace, "_atomic_install", race_install)

    with pytest.raises(FilesystemWorkspaceError, match="destination appeared"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert destination.is_symlink()
    assert list(outside.iterdir()) == []
    assert not any("gitopsctr" in path.name for path in tmp_path.iterdir())


def test_pre_stat_ancestor_symlink_swap_is_rejected_without_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    detached = tmp_path / "detached"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = parent / "destination"
    real_stat = os.stat
    swapped = False

    def racing_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal swapped
        if path == "parent" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(detached)
            parent.symlink_to(outside, target_is_directory=True)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_workspace.os, "stat", racing_stat)

    with pytest.raises(FilesystemWorkspaceError, match="destination parent.*symbolic link"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert list(outside.iterdir()) == []
    assert not (detached / "destination").exists()


def test_post_stage_mkdir_stat_failure_cleans_private_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "destination"
    names_before = {path.name for path in tmp_path.iterdir()}
    real_stat = os.stat

    def failing_stage_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".destination.gitopsctr-"):
            raise OSError("injected stage lstat failure")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_workspace.os, "stat", failing_stage_stat)

    with pytest.raises(FilesystemWorkspaceError, match="stage cannot be verified"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert not destination.exists()
    assert {path.name for path in tmp_path.iterdir()} == names_before


def test_same_principal_stage_symlink_mutation_is_detected_as_an_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    names_before = {path.name for path in tmp_path.iterdir()}
    real_readlink = os.readlink
    swapped = False

    def swapping_readlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *, dir_fd: int | None = None
    ) -> str | bytes:
        nonlocal swapped
        if path == "link" and dir_fd is not None and not swapped:
            swapped = True
            os.rename("link", "original-link", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink("/outside", "link", dir_fd=dir_fd)
        return real_readlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_workspace.os, "readlink", swapping_readlink)

    with pytest.raises(FilesystemWorkspaceError, match="changed or is unsafe"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.symlink("link", "target")), destination)
    assert not destination.exists()
    assert {path.name for path in tmp_path.iterdir()} == names_before


def test_late_ancestor_replacement_after_atomic_publish_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    destination = parent / "destination"
    detached = tmp_path / "detached"
    real_install = filesystem_workspace._atomic_install

    def install_then_replace_ancestor(
        anchor: filesystem_workspace._DestinationAnchor, stage: filesystem_workspace._PrivateStage
    ) -> None:
        real_install(anchor, stage)
        parent.rename(detached)
        parent.mkdir()

    monkeypatch.setattr(filesystem_workspace, "_atomic_install", install_then_replace_ancestor)

    with pytest.raises(FilesystemWorkspaceError, match="ancestor changed"):
        FilesystemWorkspaceAdapter().materialize(immutable(WorkspaceEntry.file("payload", b"safe")), destination)
    assert not destination.exists()
    assert (detached / "destination/payload").read_bytes() == b"safe"
