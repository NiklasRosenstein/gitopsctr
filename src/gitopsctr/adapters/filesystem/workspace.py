"""Safely translate between a local directory and a logical workspace.

Physical paths deliberately stop at this adapter.  Once the root is opened,
all traversal and mutation is relative to owned directory descriptors.  This
keeps the adapter attached to the object it checked even if a pathname is
concurrently replaced.
"""

from __future__ import annotations

import os
import stat
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from errno import EEXIST
from pathlib import Path
from secrets import token_hex

from gitopsctr.application.workspace import (
    ImmutableWorkspace,
    InMemoryWorkspace,
    WorkspaceCapabilities,
    WorkspaceEntry,
    WorkspaceEntryKind,
    WorkspaceError,
    validate_workspace_key,
)


class FilesystemWorkspaceError(WorkspaceError):
    """Raised when a local filesystem tree cannot safely become a workspace."""


_CAPABILITIES = WorkspaceCapabilities(symlinks=True, explicit_directories=True, executable_mode=True)
_FILE_MODE = 0o644
_EXECUTABLE_FILE_MODE = 0o755
_DIRECTORY_MODE = 0o755
_IDENTITY_FIELDS = ("st_dev", "st_ino")
_REQUIRED_CONSTANTS = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
_REQUIRED_DIR_FD_FUNCTIONS = (os.open, os.stat, os.mkdir, os.unlink, os.rmdir, os.symlink, os.readlink)
_REQUIRED_FD_FUNCTIONS = (os.listdir, os.stat)
try:
    _RENAMEAT2 = CDLL(None, use_errno=True).renameat2
    _RENAMEAT2.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
    _RENAMEAT2.restype = c_int
except AttributeError:
    _RENAMEAT2 = None
_MISSING_SECURE_PRIMITIVES = (
    *(name for name in _REQUIRED_CONSTANTS if not hasattr(os, name)),
    *(function.__name__ for function in _REQUIRED_DIR_FD_FUNCTIONS if function not in os.supports_dir_fd),
    *(function.__name__ for function in _REQUIRED_FD_FUNCTIONS if function not in os.supports_fd),
    *(("renameat2",) if _RENAMEAT2 is None else ()),
)


@dataclass(frozen=True, slots=True)
class _EntryIdentity:
    device: int
    inode: int
    entry_type: int
    links: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _EntryIdentity:
        return cls(metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode), metadata.st_nlink)

    def matches(self, metadata: os.stat_result, *, include_links: bool = True) -> bool:
        candidate = self.from_stat(metadata)
        return (
            self.device == candidate.device
            and self.inode == candidate.inode
            and self.entry_type == candidate.entry_type
            and (not include_links or self.links == candidate.links)
        )


@dataclass(frozen=True, slots=True)
class _CreationRecord:
    key: str
    parent_key: str
    name: str
    kind: WorkspaceEntryKind
    identity: _EntryIdentity
    expected: WorkspaceEntry | None = None


@dataclass(frozen=True, slots=True)
class _AnchoredDirectory:
    name: str
    descriptor: int
    identity: _EntryIdentity


@dataclass(frozen=True, slots=True)
class _DestinationAnchor:
    absolute_path: Path
    destination_name: str
    directories: tuple[_AnchoredDirectory, ...]

    @property
    def parent_descriptor(self) -> int:
        return self.directories[-1].descriptor


@dataclass(frozen=True, slots=True)
class _PrivateStage:
    name: str
    descriptor: int
    identity: _EntryIdentity


@dataclass(frozen=True, slots=True)
class FilesystemWorkspaceAdapter:
    """Read and materialize safe logical workspaces at the filesystem boundary."""

    @property
    def capabilities(self) -> WorkspaceCapabilities:
        """Return the workspace features faithfully represented by this adapter."""

        return _CAPABILITIES

    def read(self, root: Path, *, excluded_top_level: frozenset[str] = frozenset()) -> ImmutableWorkspace:
        """Read ``root`` without following any physical symbolic link.

        Explicit directories, regular-file bytes, executable mode, and safe
        relative symlinks are preserved.  Hardlinks, special files, escaping
        links, and logical link cycles are rejected.
        """

        _require_secure_primitives()
        if not isinstance(excluded_top_level, frozenset) or any(
            not isinstance(name, str) or not name or "/" in name for name in excluded_top_level
        ):
            raise TypeError("excluded_top_level must be a frozenset of non-empty entry names")
        _absolute, directories = _open_absolute_directory_chain(root, "workspace root")
        try:
            entries: list[WorkspaceEntry] = []
            if excluded_top_level:
                _read_directory(directories[-1].descriptor, "", entries, excluded_top_level)
            else:
                _read_directory(directories[-1].descriptor, "", entries)
            _validate_symlink_graph(entries)
            try:
                workspace = InMemoryWorkspace(entries, capabilities=self.capabilities, mutable=False)
            except WorkspaceError as exc:
                raise FilesystemWorkspaceError(f"filesystem tree is not a valid logical workspace: {exc}") from exc
            _verify_directory_chain(directories, "workspace root")
            return workspace
        finally:
            _close_anchored_directories(directories)

    def read_workspace(self, root: Path, *, excluded_top_level: frozenset[str] = frozenset()) -> ImmutableWorkspace:
        """Read a physical root; named for callers that distinguish both boundaries."""

        return self.read(root, excluded_top_level=excluded_top_level)

    def materialize(self, workspace: ImmutableWorkspace, destination: Path) -> None:
        """Atomically install an immutable workspace at an absent destination.

        Every existing ancestor is opened component-by-component without
        following symlinks.  The tree is built and verified in an unpredictable
        private ``0700`` sibling, then installed with an atomic no-replace
        rename.  Pre-publication failures remove only that private stage and
        leave ``destination`` absent.  Files use normalized ``0644``/``0755``
        modes and child directories use normalized ``0755`` mode.

        Concurrent mutation by another process with permission to enumerate or
        modify the anchored parent is outside the supported trust boundary.  To
        make that boundary explicit, the immediate destination parent must be
        owned by the effective uid and must not be group- or world-writable;
        same-uid processes are one trust principal.  Detected ancestor, stage,
        or destination replacement fails closed, but no isolation is claimed
        between cooperating or malicious processes running as that same uid.
        """

        _require_secure_primitives()
        entries = _validated_entries(workspace)
        anchor = _open_destination_anchor(destination)
        stage: _PrivateStage | None = None
        directory_fds: dict[str, int] = {}
        journal: list[_CreationRecord] = []
        published = False
        try:
            _require_trusted_install_parent(anchor)
            _require_destination_absent(anchor)
            stage = _create_private_stage(anchor)
            directory_fds[""] = stage.descriptor
            try:
                _materialize_entries(directory_fds, entries, journal)
                _verify_created_entries(directory_fds, journal)
                _verify_exact_stage_names(directory_fds, entries)
                _verify_anchor_chain(anchor)
                _require_trusted_install_parent(anchor)
                _verify_stage_attachment(anchor, stage)
                _require_destination_absent(anchor)
                _atomic_install(anchor, stage)
                published = True
                _verify_installed_destination(anchor, stage)
            except Exception as exc:
                if not published:
                    _close_materialized_descendants(directory_fds)
                    try:
                        _remove_private_stage(anchor, stage)
                    except Exception as rollback_exc:
                        raise FilesystemWorkspaceError(
                            "workspace materialization failed and private-stage cleanup also failed"
                        ) from rollback_exc
                if isinstance(exc, FilesystemWorkspaceError):
                    raise
                raise FilesystemWorkspaceError("workspace materialization failed") from exc
        finally:
            _close_materialized_descendants(directory_fds)
            if stage is not None:
                os.close(stage.descriptor)
            _close_destination_anchor(anchor)

    def materialize_workspace(self, workspace: ImmutableWorkspace, destination: Path) -> None:
        """Materialize an immutable workspace; named for explicit boundary callers."""

        self.materialize(workspace, destination)


def _require_secure_primitives() -> None:
    if _MISSING_SECURE_PRIMITIVES:
        details = ", ".join(_MISSING_SECURE_PRIMITIVES)
        raise FilesystemWorkspaceError(f"platform lacks required secure filesystem primitives: {details}")


def _open_destination_anchor(destination: Path) -> _DestinationAnchor:
    """Open every existing absolute parent component without following links."""

    if not isinstance(destination, Path):
        raise TypeError("workspace destination must be a pathlib.Path")
    absolute = Path(os.path.abspath(destination))
    if not absolute.name:
        raise FilesystemWorkspaceError("workspace destination must have a final path component")
    _parent, directories = _open_absolute_directory_chain(absolute.parent, "workspace destination parent")
    anchor = _DestinationAnchor(absolute, absolute.name, directories)
    try:
        _verify_anchor_chain(anchor)
        return anchor
    except Exception:
        _close_anchored_directories(directories)
        raise


def _open_absolute_directory_chain(
    path: Path,
    description: str,
) -> tuple[Path, tuple[_AnchoredDirectory, ...]]:
    """Anchor an absolute directory path one non-symlink component at a time."""

    if not isinstance(path, Path):
        raise TypeError(f"{description} must be a pathlib.Path")
    absolute = Path(os.path.abspath(path))

    opened: list[_AnchoredDirectory] = []
    try:
        root_fd = os.open(Path(absolute.anchor), _directory_open_flags())
        try:
            root_metadata = os.fstat(root_fd)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise FilesystemWorkspaceError("absolute filesystem anchor is not a directory")
        except Exception:
            os.close(root_fd)
            raise
        opened.append(_AnchoredDirectory(absolute.anchor, root_fd, _EntryIdentity.from_stat(root_metadata)))
        for component in absolute.parts[1:]:
            parent_fd = opened[-1].descriptor
            descriptor: int | None = None
            try:
                before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise FilesystemWorkspaceError(f"{description} component cannot be inspected: {component!r}") from exc
            if stat.S_ISLNK(before.st_mode):
                raise FilesystemWorkspaceError(f"{description} cannot contain a symbolic link: {component!r}")
            if not stat.S_ISDIR(before.st_mode):
                raise FilesystemWorkspaceError(f"{description} component is not a directory: {component!r}")
            try:
                descriptor = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
                opened_metadata = os.fstat(descriptor)
                after = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                _require_same_entry(before, opened_metadata, component)
                _require_same_entry(opened_metadata, after, component)
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise
            assert descriptor is not None
            opened.append(_AnchoredDirectory(component, descriptor, _EntryIdentity.from_stat(opened_metadata)))
        directories = tuple(opened)
        _verify_directory_chain(directories, description)
        return absolute, directories
    except Exception:
        _close_anchored_directories(tuple(opened))
        raise


def _verify_anchor_chain(anchor: _DestinationAnchor) -> None:
    _verify_directory_chain(anchor.directories, "workspace destination ancestor")


def _verify_directory_chain(directories: tuple[_AnchoredDirectory, ...], description: str) -> None:
    root = directories[0]
    if not root.identity.matches(os.fstat(root.descriptor), include_links=False):
        raise FilesystemWorkspaceError(f"{description} absolute anchor changed")
    for index in range(1, len(directories)):
        parent = directories[index - 1]
        directory = directories[index]
        try:
            attached = os.stat(directory.name, dir_fd=parent.descriptor, follow_symlinks=False)
            opened = os.fstat(directory.descriptor)
        except OSError as exc:
            raise FilesystemWorkspaceError(f"{description} is no longer attached: {directory.name!r}") from exc
        if not directory.identity.matches(attached, include_links=False) or not directory.identity.matches(
            opened, include_links=False
        ):
            raise FilesystemWorkspaceError(f"{description} changed: {directory.name!r}")


def _require_trusted_install_parent(anchor: _DestinationAnchor) -> None:
    try:
        metadata = os.fstat(anchor.parent_descriptor)
    except OSError as exc:
        raise FilesystemWorkspaceError("workspace destination parent cannot be inspected") from exc
    if metadata.st_uid != os.geteuid():
        raise FilesystemWorkspaceError("workspace destination parent must be owned by the effective uid")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise FilesystemWorkspaceError("workspace destination parent must not be group- or world-writable")


def _require_destination_absent(anchor: _DestinationAnchor) -> None:
    try:
        os.stat(anchor.destination_name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FilesystemWorkspaceError("workspace destination absence cannot be verified") from exc
    raise FilesystemWorkspaceError(f"workspace destination must not exist: {anchor.absolute_path!s}")


def _create_private_stage(anchor: _DestinationAnchor) -> _PrivateStage:
    for _attempt in range(32):
        name = f".{anchor.destination_name}.gitopsctr-{token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=anchor.parent_descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise FilesystemWorkspaceError("private workspace stage cannot be created") from exc
        descriptor: int | None = None
        try:
            before = os.stat(name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
            descriptor = os.open(name, _directory_open_flags(), dir_fd=anchor.parent_descriptor)
            opened = os.fstat(descriptor)
            after = os.stat(name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
            _require_same_entry(before, opened, name)
            _require_same_entry(opened, after, name)
            if not stat.S_ISDIR(opened.st_mode):
                raise FilesystemWorkspaceError("private workspace stage is not a directory")
            os.fchmod(descriptor, 0o700)
            return _PrivateStage(name, descriptor, _EntryIdentity.from_stat(opened))
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.rmdir(name, dir_fd=anchor.parent_descriptor)
            except OSError as cleanup_exc:
                raise FilesystemWorkspaceError("private workspace stage creation cleanup failed") from cleanup_exc
            if isinstance(exc, FilesystemWorkspaceError):
                raise
            raise FilesystemWorkspaceError("private workspace stage cannot be verified") from exc
    raise FilesystemWorkspaceError("could not allocate an unpredictable private workspace stage")


def _verify_stage_attachment(anchor: _DestinationAnchor, stage: _PrivateStage) -> None:
    try:
        attached = os.stat(stage.name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
        opened = os.fstat(stage.descriptor)
    except OSError as exc:
        raise FilesystemWorkspaceError("private workspace stage is no longer attached") from exc
    if not stage.identity.matches(attached, include_links=False) or not stage.identity.matches(
        opened, include_links=False
    ):
        raise FilesystemWorkspaceError("private workspace stage changed")
    if stat.S_IMODE(opened.st_mode) != 0o700:
        raise FilesystemWorkspaceError("private workspace stage mode changed")


def _atomic_install(anchor: _DestinationAnchor, stage: _PrivateStage) -> None:
    if _RENAMEAT2 is None:
        raise FilesystemWorkspaceError("atomic no-replace rename is unavailable")
    result = _RENAMEAT2(
        anchor.parent_descriptor,
        os.fsencode(stage.name),
        anchor.parent_descriptor,
        os.fsencode(anchor.destination_name),
        1,
    )
    if result:
        error = get_errno()
        if error == EEXIST:
            raise FilesystemWorkspaceError("workspace destination appeared before atomic installation")
        raise FilesystemWorkspaceError(f"atomic workspace installation failed with errno {error}")


def _verify_installed_destination(anchor: _DestinationAnchor, stage: _PrivateStage) -> None:
    _verify_anchor_chain(anchor)
    _require_trusted_install_parent(anchor)
    try:
        installed = os.stat(anchor.destination_name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
        opened = os.fstat(stage.descriptor)
    except OSError as exc:
        raise FilesystemWorkspaceError("installed workspace destination cannot be verified") from exc
    if not stage.identity.matches(installed, include_links=False) or not stage.identity.matches(
        opened, include_links=False
    ):
        raise FilesystemWorkspaceError("installed workspace destination changed")
    try:
        os.stat(stage.name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FilesystemWorkspaceError("private workspace stage name still exists after installation")


def _close_materialized_descendants(directory_fds: dict[str, int]) -> None:
    for key in sorted((key for key in directory_fds if key), key=lambda value: value.count("/"), reverse=True):
        os.close(directory_fds.pop(key))


def _remove_private_stage(anchor: _DestinationAnchor, stage: _PrivateStage) -> None:
    _clear_owned_directory(stage.descriptor)
    _verify_stage_attachment(anchor, stage)
    os.rmdir(stage.name, dir_fd=anchor.parent_descriptor)


def _clear_owned_directory(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            descriptor = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(descriptor)
                _require_same_entry(metadata, opened, name)
                _clear_owned_directory(descriptor)
                _require_path_still_entry(directory_fd, name, opened, name)
            finally:
                os.close(descriptor)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _close_destination_anchor(anchor: _DestinationAnchor) -> None:
    _close_anchored_directories(anchor.directories)


def _close_anchored_directories(directories: tuple[_AnchoredDirectory, ...]) -> None:
    for directory in reversed(directories):
        os.close(directory.descriptor)


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _read_directory(
    directory_fd: int,
    prefix: str,
    entries: list[WorkspaceEntry],
    excluded_top_level: frozenset[str] = frozenset(),
) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise FilesystemWorkspaceError(f"could not scan workspace directory {prefix or '.'!r}") from exc
    for name in names:
        if not prefix and name in excluded_top_level:
            continue
        key = name if not prefix else f"{prefix}/{name}"
        try:
            validate_workspace_key(key)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except (OSError, WorkspaceError) as exc:
            raise FilesystemWorkspaceError(f"invalid workspace entry {key!r}") from exc
        entry_type = stat.S_IFMT(metadata.st_mode)
        if entry_type == stat.S_IFDIR:
            child_fd = _open_checked_entry(directory_fd, name, metadata, key, directory=True)
            try:
                entries.append(WorkspaceEntry.directory(key))
                _read_directory(child_fd, key, entries, excluded_top_level)
                _require_path_still_entry(directory_fd, name, os.fstat(child_fd), key)
            finally:
                os.close(child_fd)
        elif entry_type == stat.S_IFREG:
            descriptor = _open_checked_entry(directory_fd, name, metadata, key, directory=False)
            try:
                entries.append(
                    WorkspaceEntry.file(
                        key,
                        _read_regular_file(descriptor, key),
                        executable=bool(metadata.st_mode & 0o111),
                    )
                )
                final = os.fstat(descriptor)
                _require_single_link(final, key)
                _require_path_still_entry(directory_fd, name, final, key)
            finally:
                os.close(descriptor)
        elif entry_type == stat.S_IFLNK:
            _require_single_link(metadata, key)
            try:
                target = os.readlink(name, dir_fd=directory_fd)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                _require_same_entry(metadata, after, key)
                _require_single_link(after, key)
                entries.append(WorkspaceEntry.symlink(key, target))
            except (OSError, WorkspaceError) as exc:
                raise FilesystemWorkspaceError(f"unsafe symbolic link at {key!r}") from exc
        else:
            raise FilesystemWorkspaceError(f"unsupported filesystem entry at {key!r}")


def _open_checked_entry(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    key: str,
    *,
    directory: bool,
) -> int:
    flags = _directory_open_flags() if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"workspace entry cannot be opened safely: {key!r}") from exc
    try:
        opened = os.fstat(descriptor)
        _require_same_entry(expected, opened, key)
        _require_path_still_entry(parent_fd, name, opened, key)
        if directory:
            if not stat.S_ISDIR(opened.st_mode):
                raise FilesystemWorkspaceError(f"workspace entry is not a directory: {key!r}")
        else:
            if not stat.S_ISREG(opened.st_mode):
                raise FilesystemWorkspaceError(f"workspace entry is not a regular file: {key!r}")
            _require_single_link(opened, key)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _require_path_still_entry(parent_fd: int, name: str, opened: os.stat_result, key: str) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"workspace entry changed while being read: {key!r}") from exc
    _require_same_entry(opened, current, key)


def _require_same_entry(first: os.stat_result, second: os.stat_result, description: str) -> None:
    same_identity = all(getattr(first, field) == getattr(second, field) for field in _IDENTITY_FIELDS)
    same_type = stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    if not same_identity or not same_type:
        raise FilesystemWorkspaceError(f"filesystem entry changed during access: {description}")


def _require_single_link(metadata: os.stat_result, key: str) -> None:
    if metadata.st_nlink != 1:
        raise FilesystemWorkspaceError(f"workspace entry is hardlinked and therefore unsafe: {key!r}")


def _read_regular_file(descriptor: int, key: str) -> bytes:
    chunks: list[bytes] = []
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"regular workspace file cannot be read safely: {key!r}") from exc
    return b"".join(chunks)


def _validated_entries(workspace: ImmutableWorkspace) -> tuple[WorkspaceEntry, ...]:
    if not isinstance(workspace, ImmutableWorkspace):
        raise TypeError("workspace must implement ImmutableWorkspace")
    if workspace.is_mutable:
        raise FilesystemWorkspaceError("only an immutable workspace can be materialized")
    try:
        entries = tuple(workspace.list_entries())
    except Exception as exc:
        raise FilesystemWorkspaceError("workspace entries cannot be listed") from exc
    validated: list[WorkspaceEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, WorkspaceEntry):
            raise FilesystemWorkspaceError("workspace must expose WorkspaceEntry values")
        try:
            copied = WorkspaceEntry(entry.key, entry.kind, entry.content, entry.executable, entry.target)
        except WorkspaceError as exc:
            raise FilesystemWorkspaceError(f"invalid workspace entry {entry.key!r}: {exc}") from exc
        if copied.key in seen:
            raise FilesystemWorkspaceError(f"workspace contains a duplicate key: {copied.key!r}")
        seen.add(copied.key)
        validated.append(copied)
    try:
        normalized = InMemoryWorkspace(validated, capabilities=_CAPABILITIES, mutable=False)
    except WorkspaceError as exc:
        raise FilesystemWorkspaceError(f"workspace cannot be materialized safely: {exc}") from exc
    normalized_entries = normalized.list_entries()
    _validate_symlink_graph(normalized_entries)
    return normalized_entries


def _validate_symlink_graph(entries: list[WorkspaceEntry] | tuple[WorkspaceEntry, ...]) -> None:
    by_key = {entry.key: entry for entry in entries}
    for key in tuple(by_key):
        parts = key.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            by_key.setdefault(parent, WorkspaceEntry.directory(parent))
    for entry in by_key.values():
        if entry.kind is WorkspaceEntryKind.SYMLINK:
            _resolve_symlink(entry.key, by_key)


def _resolve_symlink(key: str, by_key: dict[str, WorkspaceEntry]) -> str | None:
    """Resolve known symlinks component-wise; return ``None`` for dangling paths."""

    current = by_key[key]
    assert current.kind is WorkspaceEntryKind.SYMLINK
    assert current.target is not None
    pending = _absolute_target_parts(current.key, current.target)
    resolved: list[str] = []
    visited = {key}
    while pending:
        resolved.append(pending.pop(0))
        candidate_key = "/".join(resolved)
        candidate = by_key.get(candidate_key)
        if candidate is None:
            return None
        if candidate.kind is not WorkspaceEntryKind.SYMLINK:
            continue
        if candidate_key in visited:
            raise FilesystemWorkspaceError(f"symbolic link cycle includes {candidate_key!r}")
        visited.add(candidate_key)
        assert candidate.target is not None
        pending = [*_absolute_target_parts(candidate.key, candidate.target), *pending]
        resolved = []
    return "/".join(resolved)


def _absolute_target_parts(key: str, target: str) -> list[str]:
    parts = key.split("/")[:-1]
    for component in target.split("/"):
        if component == "..":
            parts.pop()
        else:
            parts.append(component)
    return parts


def _materialize_entries(
    directory_fds: dict[str, int],
    entries: tuple[WorkspaceEntry, ...],
    journal: list[_CreationRecord],
) -> None:
    entries_by_key = {entry.key: entry for entry in entries}
    for key in _directory_keys(entries):
        parent_key, name = _split_parent(key)
        parent_fd = directory_fds[parent_key]
        descriptor: int | None = None
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise FilesystemWorkspaceError(f"workspace directory cannot be created: {key!r}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise FilesystemWorkspaceError(f"created workspace entry is not a directory: {key!r}")
            _require_path_still_entry(parent_fd, name, metadata, key)
            directory_fds[key] = descriptor
            journal.append(
                _CreationRecord(
                    key,
                    parent_key,
                    name,
                    WorkspaceEntryKind.DIRECTORY,
                    _EntryIdentity.from_stat(metadata),
                    entries_by_key.get(key),
                )
            )
            os.fchmod(descriptor, _DIRECTORY_MODE)
        except Exception:
            if descriptor is not None and key not in directory_fds:
                os.close(descriptor)
            raise

    for entry in entries:
        if entry.kind is WorkspaceEntryKind.DIRECTORY:
            continue
        parent_key, name = _split_parent(entry.key)
        parent_fd = directory_fds[parent_key]
        if entry.kind is WorkspaceEntryKind.FILE:
            _materialize_file(parent_fd, parent_key, name, entry, journal)
        else:
            _materialize_symlink(parent_fd, parent_key, name, entry, journal)


def _materialize_symlink(
    parent_fd: int,
    parent_key: str,
    name: str,
    entry: WorkspaceEntry,
    journal: list[_CreationRecord],
) -> None:
    assert entry.target is not None
    try:
        os.symlink(entry.target, name, dir_fd=parent_fd)
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"workspace symbolic link cannot be created: {entry.key!r}") from exc
    identity = _EntryIdentity.from_stat(before)
    journal.append(_CreationRecord(entry.key, parent_key, name, entry.kind, identity, entry))
    try:
        actual_target = os.readlink(name, dir_fd=parent_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"created workspace symbolic link cannot be verified: {entry.key!r}") from exc
    if (
        not stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not identity.matches(after)
        or actual_target != entry.target
    ):
        raise FilesystemWorkspaceError(f"created workspace symbolic link changed or is unsafe: {entry.key!r}")


def _verify_created_entries(
    directory_fds: dict[str, int],
    journal: list[_CreationRecord],
) -> None:
    """Re-read identity and semantics for every object before reporting success."""

    for record in journal:
        parent_fd = directory_fds[record.parent_key]
        if record.kind is WorkspaceEntryKind.DIRECTORY:
            current = os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(directory_fds[record.key])
            if (
                not record.identity.matches(current, include_links=False)
                or not record.identity.matches(opened, include_links=False)
                or stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE
            ):
                raise FilesystemWorkspaceError(f"created workspace directory changed: {record.key!r}")
        elif record.kind is WorkspaceEntryKind.FILE:
            _verify_created_file(parent_fd, record)
        else:
            _verify_created_symlink(parent_fd, record)


def _verify_exact_stage_names(
    directory_fds: dict[str, int],
    entries: tuple[WorkspaceEntry, ...],
) -> None:
    physical_keys = {*_directory_keys(entries), *(entry.key for entry in entries)}
    expected_by_parent: dict[str, set[str]] = {key: set() for key in directory_fds}
    for key in physical_keys:
        parent_key, name = _split_parent(key)
        expected_by_parent[parent_key].add(name)
    for parent_key, expected_names in expected_by_parent.items():
        try:
            actual_names = set(os.listdir(directory_fds[parent_key]))
        except OSError as exc:
            raise FilesystemWorkspaceError(f"private workspace directory cannot be listed: {parent_key!r}") from exc
        if actual_names != expected_names:
            raise FilesystemWorkspaceError(f"private workspace contains unexpected entries below {parent_key!r}")


def _verify_created_file(parent_fd: int, record: _CreationRecord) -> None:
    assert record.expected is not None and record.expected.content is not None
    try:
        before = os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(record.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"created workspace file cannot be verified: {record.key!r}") from exc
    try:
        opened = os.fstat(descriptor)
        content = _read_regular_file(descriptor, record.key)
        final = os.fstat(descriptor)
        after = os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(descriptor)
    expected_mode = _EXECUTABLE_FILE_MODE if record.expected.executable else _FILE_MODE
    if (
        not record.identity.matches(before)
        or not record.identity.matches(opened)
        or not record.identity.matches(final)
        or not record.identity.matches(after)
        or stat.S_IMODE(final.st_mode) != expected_mode
        or content != record.expected.content
    ):
        raise FilesystemWorkspaceError(f"created workspace file changed: {record.key!r}")


def _verify_created_symlink(parent_fd: int, record: _CreationRecord) -> None:
    assert record.expected is not None and record.expected.target is not None
    try:
        before = os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
        target = os.readlink(record.name, dir_fd=parent_fd)
        after = os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"created workspace symbolic link cannot be verified: {record.key!r}") from exc
    if not record.identity.matches(before) or not record.identity.matches(after) or target != record.expected.target:
        raise FilesystemWorkspaceError(f"created workspace symbolic link changed: {record.key!r}")


def _directory_keys(entries: tuple[WorkspaceEntry, ...]) -> tuple[str, ...]:
    keys = {entry.key for entry in entries if entry.kind is WorkspaceEntryKind.DIRECTORY}
    for entry in entries:
        parts = entry.key.split("/")[:-1]
        keys.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    return tuple(sorted(keys, key=lambda key: (key.count("/"), key)))


def _split_parent(key: str) -> tuple[str, str]:
    parent, separator, name = key.rpartition("/")
    return (parent if separator else "", name if separator else key)


def _materialize_file(
    parent_fd: int,
    parent_key: str,
    name: str,
    entry: WorkspaceEntry,
    journal: list[_CreationRecord],
) -> None:
    assert entry.content is not None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, _FILE_MODE, dir_fd=parent_fd)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"workspace file cannot be created: {entry.key!r}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise FilesystemWorkspaceError(f"created workspace file is unsafe: {entry.key!r}")
        _require_path_still_entry(parent_fd, name, opened, entry.key)
        journal.append(
            _CreationRecord(
                entry.key,
                parent_key,
                name,
                entry.kind,
                _EntryIdentity.from_stat(opened),
                entry,
            )
        )
        remaining = memoryview(entry.content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise FilesystemWorkspaceError(f"workspace file write made no progress: {entry.key!r}")
            remaining = remaining[written:]
        os.fchmod(descriptor, _EXECUTABLE_FILE_MODE if entry.executable else _FILE_MODE)
        final = os.fstat(descriptor)
        _require_single_link(final, entry.key)
        _require_path_still_entry(parent_fd, name, final, entry.key)
    except OSError as exc:
        raise FilesystemWorkspaceError(f"workspace file cannot be written: {entry.key!r}") from exc
    finally:
        os.close(descriptor)
