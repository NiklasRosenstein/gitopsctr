"""Backend-neutral logical workspaces.

This module deliberately models content and logical POSIX keys rather than a
filesystem.  Adapters may materialize a workspace at their boundary, but
application code must not need a local path in order to inspect or transform
candidate content.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, runtime_checkable
from unicodedata import category, normalize

from gitopsctr.application.model import ContentId

type WorkspaceKey = str

_CONTENT_ID_DOMAIN = b"gitopsctr.logical-workspace.content.v1\0"


class WorkspaceError(ValueError):
    """Raised when a logical workspace operation would be unsafe or invalid."""


class WorkspaceEntryNotFoundError(WorkspaceError):
    """Raised when an operation requires an entry that does not exist."""


class WorkspaceImmutableError(WorkspaceError):
    """Raised when a mutation is attempted through an immutable workspace."""


class WorkspaceEntryKind(StrEnum):
    """The three payload forms represented by a logical workspace."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class WorkspaceCapabilities:
    """Features whose semantics are meaningful in a workspace implementation."""

    symlinks: bool = False
    explicit_directories: bool = False
    executable_mode: bool = False

    def __post_init__(self) -> None:
        for name in ("symlinks", "explicit_directories", "executable_mode"):
            if type(getattr(self, name)) is not bool:
                raise WorkspaceError(f"workspace capability {name!r} must be a bool")


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    """One immutable logical entry, including identity-bearing payload metadata."""

    key: WorkspaceKey
    kind: WorkspaceEntryKind
    content: bytes | None = None
    executable: bool = False
    target: str | None = None

    def __post_init__(self) -> None:
        validate_workspace_key(self.key)
        if not isinstance(self.kind, WorkspaceEntryKind):
            raise WorkspaceError("workspace entry kind must be a WorkspaceEntryKind")
        if type(self.executable) is not bool:
            raise WorkspaceError("workspace entry executable mode must be a bool")
        if self.kind is WorkspaceEntryKind.FILE:
            if self.content is None or self.target is not None:
                raise WorkspaceError("a file entry requires bytes and cannot have a symlink target")
            if not isinstance(self.content, bytes):
                raise WorkspaceError("a file entry's content must be bytes")
            return
        if self.kind is WorkspaceEntryKind.DIRECTORY:
            if self.content is not None or self.target is not None or self.executable:
                raise WorkspaceError("a directory entry cannot have payload metadata")
            return
        if self.kind is WorkspaceEntryKind.SYMLINK:
            if self.content is not None or self.executable or self.target is None:
                raise WorkspaceError("a symlink entry requires a target and cannot have file metadata")
            validate_relative_symlink_target(self.key, self.target)
            return
        raise WorkspaceError(f"unsupported workspace entry kind: {self.kind!r}")

    @classmethod
    def file(cls, key: str, content: bytes, *, executable: bool = False) -> WorkspaceEntry:
        return cls(key, WorkspaceEntryKind.FILE, content=content, executable=executable)

    @classmethod
    def directory(cls, key: str) -> WorkspaceEntry:
        return cls(key, WorkspaceEntryKind.DIRECTORY)

    @classmethod
    def symlink(cls, key: str, target: str) -> WorkspaceEntry:
        return cls(key, WorkspaceEntryKind.SYMLINK, target=target)


def validate_workspace_key(key: WorkspaceKey) -> WorkspaceKey:
    """Validate and return a canonical, relative POSIX workspace key."""

    _validate_canonical_text(key, "workspace key")
    if "\\" in key:
        raise WorkspaceError(f"workspace key must use POSIX separators: {key!r}")
    if key.startswith("/"):
        raise WorkspaceError(f"workspace key must be relative: {key!r}")
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceError(f"workspace key must be canonical and cannot traverse: {key!r}")
    return key


def validate_relative_symlink_target(key: str, target: str) -> str:
    """Validate a relative symlink target and prove it cannot escape ``key``'s root."""

    validate_workspace_key(key)
    _validate_canonical_text(target, "symlink target")
    if "\\" in target or target.startswith("/"):
        raise WorkspaceError(f"symlink target must be relative POSIX text: {target!r}")

    resolved = key.split("/")[:-1]
    for part in target.split("/"):
        if part in {"", "."}:
            raise WorkspaceError(f"symlink target must be canonical: {target!r}")
        if part == "..":
            if not resolved:
                raise WorkspaceError(f"symlink target escapes workspace containment: {target!r}")
            resolved.pop()
        else:
            resolved.append(part)
    return target


def _validate_canonical_text(value: object, description: str) -> str:
    """Reject text that cannot be one canonical UTF-8 workspace spelling."""

    if not isinstance(value, str) or not value:
        raise WorkspaceError(f"a {description} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspaceError(f"{description} must be UTF-8 encodable") from exc
    if value != normalize("NFC", value):
        raise WorkspaceError(f"{description} must be NFC-normalized")
    if any(category(character) in {"Cc", "Cs"} for character in value):
        raise WorkspaceError(f"{description} cannot contain control characters or surrogates")
    return value


def _canonical_entry_bytes(entry: WorkspaceEntry) -> bytes:
    """Encode an entry without delimiters whose contents could be ambiguous."""

    def field(value: bytes) -> bytes:
        return len(value).to_bytes(8, "big") + value

    payload = b"" if entry.content is None else entry.content
    target = b"" if entry.target is None else entry.target.encode("utf-8")
    return b"".join(
        (
            field(entry.key.encode("utf-8")),
            field(entry.kind.value.encode("ascii")),
            b"\x01" if entry.executable else b"\x00",
            field(payload),
            field(target),
        )
    )


@runtime_checkable
class ImmutableWorkspace(Protocol):
    """Read-only view of one logical content tree."""

    @property
    def capabilities(self) -> WorkspaceCapabilities: ...

    @property
    def is_mutable(self) -> bool: ...

    @property
    def content_id(self) -> ContentId: ...

    def list_entries(self, prefix: WorkspaceKey | None = None) -> tuple[WorkspaceEntry, ...]: ...

    def list(self, prefix: WorkspaceKey | None = None) -> tuple[WorkspaceEntry, ...]: ...

    def get_entry(self, key: WorkspaceKey) -> WorkspaceEntry: ...

    def inspect(self, key: WorkspaceKey) -> WorkspaceEntry: ...

    def read(self, key: WorkspaceKey) -> bytes: ...


@runtime_checkable
class MutableWorkspace(ImmutableWorkspace, Protocol):
    """Candidate workspace capability; it does not seal or publish content."""

    def write(self, key: WorkspaceKey, content: bytes, *, executable: bool = False) -> None: ...

    def mkdir(self, key: WorkspaceKey) -> None: ...

    def symlink(self, key: WorkspaceKey, target: str) -> None: ...

    def copy_from(
        self, source: ImmutableWorkspace, source_key: WorkspaceKey, destination_key: WorkspaceKey
    ) -> None: ...

    def delete(self, key: WorkspaceKey, *, recursive: bool = False) -> None: ...


class InMemoryWorkspace:
    """A small, deterministic conformance implementation of logical workspaces."""

    def __init__(
        self,
        entries: Iterable[WorkspaceEntry] = (),
        *,
        capabilities: WorkspaceCapabilities | None = None,
        mutable: bool = True,
    ) -> None:
        if type(mutable) is not bool:
            raise WorkspaceError("workspace mutability must be a bool")
        if capabilities is not None and not isinstance(capabilities, WorkspaceCapabilities):
            raise WorkspaceError("workspace capabilities must be a WorkspaceCapabilities instance")
        self._capabilities = capabilities or WorkspaceCapabilities(
            symlinks=True, explicit_directories=True, executable_mode=True
        )
        self._mutable = mutable
        self._entries: dict[str, WorkspaceEntry] = {}
        for entry in entries:
            self._validate_capabilities(entry)
            if entry.key in self._entries:
                raise WorkspaceError(f"duplicate workspace key: {entry.key!r}")
            self._entries[entry.key] = entry
        for entry in self._entries.values():
            self._ensure_parent_containment(entry.key)

    @property
    def capabilities(self) -> WorkspaceCapabilities:
        return self._capabilities

    @property
    def is_mutable(self) -> bool:
        return self._mutable

    @property
    def content_id(self) -> ContentId:
        digest = sha256()
        digest.update(_CONTENT_ID_DOMAIN)
        for entry in self._identity_entries():
            encoded = _canonical_entry_bytes(entry)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return ContentId(f"sha256:{digest.hexdigest()}")

    def list_entries(self, prefix: str | None = None) -> tuple[WorkspaceEntry, ...]:
        if prefix is not None:
            validate_workspace_key(prefix)
            entries = (entry for key, entry in self._entries.items() if key == prefix or key.startswith(f"{prefix}/"))
        else:
            entries = self._entries.values()
        return tuple(sorted(entries, key=lambda entry: entry.key))

    def list(self, prefix: WorkspaceKey | None = None) -> tuple[WorkspaceEntry, ...]:
        """List typed entries in canonical logical-key order."""

        return self.list_entries(prefix)

    def get_entry(self, key: str) -> WorkspaceEntry:
        validate_workspace_key(key)
        try:
            return self._entries[key]
        except KeyError as exc:
            raise WorkspaceEntryNotFoundError(f"workspace entry does not exist: {key!r}") from exc

    def inspect(self, key: WorkspaceKey) -> WorkspaceEntry:
        """Inspect typed entry metadata without reading its regular-file bytes."""

        return self.get_entry(key)

    def read(self, key: str) -> bytes:
        entry = self.get_entry(key)
        if entry.kind is not WorkspaceEntryKind.FILE:
            raise WorkspaceError(f"workspace entry is not a regular file: {key!r}")
        assert entry.content is not None
        return entry.content

    def write(self, key: str, content: bytes, *, executable: bool = False) -> None:
        self._ensure_mutable()
        entry = WorkspaceEntry.file(key, content, executable=executable)
        self._validate_capabilities(entry)
        self._replace_leaf(entry)

    def mkdir(self, key: str) -> None:
        self._ensure_mutable()
        entry = WorkspaceEntry.directory(key)
        self._validate_capabilities(entry)
        if key in self._entries:
            raise WorkspaceError(f"workspace entry already exists: {key!r}")
        self._ensure_parent_containment(key)
        self._entries[key] = entry

    def symlink(self, key: str, target: str) -> None:
        self._ensure_mutable()
        entry = WorkspaceEntry.symlink(key, target)
        self._validate_capabilities(entry)
        self._replace_leaf(entry)

    def copy_from(self, source: ImmutableWorkspace, source_key: str, destination_key: str) -> None:
        self._ensure_mutable()
        validate_workspace_key(source_key)
        validate_workspace_key(destination_key)
        source_entry = source.get_entry(source_key)
        source_entries = source.list_entries(source_key)
        if source_entry.kind is not WorkspaceEntryKind.DIRECTORY:
            source_entries = (source_entry,)
        if source_key == destination_key and source is self:
            return
        copied_entries: list[WorkspaceEntry] = []
        for entry in source_entries:
            suffix = entry.key.removeprefix(source_key)
            destination = f"{destination_key}{suffix}"
            copied = WorkspaceEntry(
                destination,
                entry.kind,
                content=entry.content,
                executable=entry.executable,
                target=entry.target,
            )
            self._validate_capabilities(copied)
            copied_entries.append(copied)

        collisions = {key for key in self._entries if key == destination_key or key.startswith(f"{destination_key}/")}
        if collisions:
            rendered = ", ".join(repr(key) for key in sorted(collisions))
            raise WorkspaceError(f"copy destination already contains entries: {rendered}")
        for entry in copied_entries:
            self._ensure_parent_containment(entry.key)
        self._entries.update((entry.key, entry) for entry in copied_entries)

    def delete(self, key: str, *, recursive: bool = False) -> None:
        self._ensure_mutable()
        validate_workspace_key(key)
        descendants = [candidate for candidate in self._entries if candidate.startswith(f"{key}/")]
        if key not in self._entries:
            if descendants:
                if not recursive:
                    raise WorkspaceError(f"workspace directory is not empty: {key!r}")
            else:
                raise WorkspaceEntryNotFoundError(f"workspace entry does not exist: {key!r}")
        if descendants and not recursive:
            raise WorkspaceError(f"workspace directory is not empty: {key!r}")
        self._entries.pop(key, None)
        for descendant in descendants:
            del self._entries[descendant]

    def mutable_copy(self) -> InMemoryWorkspace:
        """Return an independent mutable candidate view of the same logical entries."""

        return InMemoryWorkspace(self.list_entries(), capabilities=self.capabilities, mutable=True)

    def _identity_entries(self) -> tuple[WorkspaceEntry, ...]:
        """Return the canonical logical tree used for content identity.

        Every nonempty parent is represented once, regardless of an adapter's
        directory capability or whether it stored that directory explicitly.
        An explicitly stored directory without descendants remains present and
        therefore remains identity-bearing.
        """

        entries = dict(self._entries)
        for key in tuple(entries):
            parts = key.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                entries.setdefault(parent, WorkspaceEntry.directory(parent))
        return tuple(sorted(entries.values(), key=lambda entry: entry.key))

    def _ensure_mutable(self) -> None:
        if not self._mutable:
            raise WorkspaceImmutableError("this workspace is immutable")

    def _validate_capabilities(self, entry: WorkspaceEntry) -> None:
        if entry.kind is WorkspaceEntryKind.SYMLINK and not self.capabilities.symlinks:
            raise WorkspaceError("workspace does not support symlink entries")
        if entry.kind is WorkspaceEntryKind.DIRECTORY and not self.capabilities.explicit_directories:
            raise WorkspaceError("workspace does not support explicit directory entries")
        if entry.executable and not self.capabilities.executable_mode:
            raise WorkspaceError("workspace does not support executable file mode")

    def _ensure_parent_containment(self, key: str) -> None:
        parts = key.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            entry = self._entries.get(parent)
            if entry is not None and entry.kind is not WorkspaceEntryKind.DIRECTORY:
                raise WorkspaceError(f"workspace parent is not a directory: {parent!r}")

    def _replace_leaf(self, entry: WorkspaceEntry) -> None:
        descendants = [key for key in self._entries if key.startswith(f"{entry.key}/")]
        if descendants:
            raise WorkspaceError(f"cannot replace a workspace directory with a leaf: {entry.key!r}")
        existing = self._entries.get(entry.key)
        if existing is not None and existing.kind is WorkspaceEntryKind.DIRECTORY:
            raise WorkspaceError(f"cannot replace a workspace directory with a leaf: {entry.key!r}")
        self._ensure_parent_containment(entry.key)
        self._entries[entry.key] = entry
