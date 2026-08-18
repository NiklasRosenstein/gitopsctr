from __future__ import annotations

import pytest

from gitopsctr.application.workspace import (
    InMemoryWorkspace,
    WorkspaceCapabilities,
    WorkspaceEntry,
    WorkspaceEntryKind,
    WorkspaceEntryNotFoundError,
    WorkspaceError,
    WorkspaceImmutableError,
    validate_relative_symlink_target,
    validate_workspace_key,
)


@pytest.mark.parametrize("key", ["", "/absolute", "a/../b", "a/./b", "a//b", "a\\b"])
def test_workspace_keys_reject_noncanonical_or_unsafe_values(key: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_workspace_key(key)


@pytest.mark.parametrize("key", [None, 1, "bad\x00key", "bad\x1fkey", "e\u0301", "bad\ud800key"])
def test_workspace_keys_require_canonical_utf8_text(key: object) -> None:
    with pytest.raises(WorkspaceError):
        validate_workspace_key(key)  # type: ignore[arg-type]


def test_workspace_keys_accept_nfc_utf8_text() -> None:
    assert validate_workspace_key("café/document") == "café/document"


def test_relative_symlink_targets_must_stay_within_workspace() -> None:
    assert validate_relative_symlink_target("dir/link", "../target") == "../target"

    for target in ("/etc/passwd", "../../escape", "..\\escape", "./target", "target//child"):
        with pytest.raises(WorkspaceError):
            validate_relative_symlink_target("dir/link", target)

    for target in (None, 1, "bad\x00target", "bad\x1ftarget", "e\u0301", "bad\ud800target"):
        with pytest.raises(WorkspaceError):
            validate_relative_symlink_target("dir/link", target)  # type: ignore[arg-type]


def test_entries_list_in_canonical_key_order_and_have_stable_identity() -> None:
    first = InMemoryWorkspace(
        [
            WorkspaceEntry.file("z.txt", b"z"),
            WorkspaceEntry.file("a.txt", b"a"),
            WorkspaceEntry.directory("directory"),
        ]
    )
    second = InMemoryWorkspace(
        [
            WorkspaceEntry.directory("directory"),
            WorkspaceEntry.file("a.txt", b"a"),
            WorkspaceEntry.file("z.txt", b"z"),
        ]
    )

    assert [entry.key for entry in first.list_entries()] == ["a.txt", "directory", "z.txt"]
    assert first.content_id == second.content_id
    assert str(first.content_id).startswith("sha256:")


def test_content_id_has_fixed_versioned_vectors() -> None:
    assert (
        str(InMemoryWorkspace().content_id) == "sha256:4d5195f2e2363d81d7e3086fecbec411d6586692bf2eaab7b34e959f0c82d6df"
    )
    assert str(InMemoryWorkspace([WorkspaceEntry.file("a", b"x")]).content_id) == (
        "sha256:a672f15996e7cb7946d3cca2ebfd848267fab5f4b15a70b1fd2ded2f347a294c"
    )
    assert str(InMemoryWorkspace([WorkspaceEntry.directory("a")]).content_id) == (
        "sha256:595ee7ad6f58ff5ca5797073384a519efc513582c5eb61bb4546150e211a4ecc"
    )


def test_content_id_canonicalizes_implicit_and_explicit_nonempty_directories() -> None:
    implicit = InMemoryWorkspace([WorkspaceEntry.file("parent/child/data", b"payload")])
    explicit = InMemoryWorkspace(
        [
            WorkspaceEntry.directory("parent/child"),
            WorkspaceEntry.file("parent/child/data", b"payload"),
            WorkspaceEntry.directory("parent"),
        ]
    )

    assert implicit.content_id == explicit.content_id
    assert str(implicit.content_id) == "sha256:af6438c618953396b75c889fe94d058749f87fca2a6165c88081210bc052b1bd"


def test_content_id_is_neutral_to_directory_capability_for_identical_file_trees() -> None:
    with_directories = InMemoryWorkspace(
        [WorkspaceEntry.file("parent/child/data", b"payload")],
        capabilities=WorkspaceCapabilities(explicit_directories=True),
    )
    without_directories = InMemoryWorkspace(
        [WorkspaceEntry.file("parent/child/data", b"payload")],
        capabilities=WorkspaceCapabilities(explicit_directories=False),
    )

    assert with_directories.content_id == without_directories.content_id
    assert str(without_directories.content_id) == (
        "sha256:af6438c618953396b75c889fe94d058749f87fca2a6165c88081210bc052b1bd"
    )


def test_empty_explicit_directory_remains_identity_bearing() -> None:
    empty = InMemoryWorkspace()
    with_empty_directory = InMemoryWorkspace([WorkspaceEntry.directory("empty")])

    assert empty.content_id != with_empty_directory.content_id


def test_executable_mode_and_entry_kind_contribute_to_content_identity() -> None:
    normal = InMemoryWorkspace([WorkspaceEntry.file("run", b"payload")])
    executable = InMemoryWorkspace([WorkspaceEntry.file("run", b"payload", executable=True)])
    symlink = InMemoryWorkspace([WorkspaceEntry.symlink("run", "payload")])

    assert normal.content_id != executable.content_id
    assert normal.content_id != symlink.content_id


def test_workspace_capabilities_are_declared_and_enforced() -> None:
    workspace = InMemoryWorkspace(capabilities=WorkspaceCapabilities())

    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.symlink("link", "target")
    with pytest.raises(WorkspaceError, match="directory"):
        workspace.mkdir("directory")
    with pytest.raises(WorkspaceError, match="executable"):
        workspace.write("run", b"payload", executable=True)


@pytest.mark.parametrize("capabilities", [False, {}, "all", 1])
def test_workspace_rejects_a_non_capabilities_value(capabilities: object) -> None:
    with pytest.raises(WorkspaceError, match="WorkspaceCapabilities"):
        InMemoryWorkspace(capabilities=capabilities)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("symlinks", 1),
        ("explicit_directories", "true"),
        ("executable_mode", None),
    ],
)
def test_workspace_capabilities_require_exact_bool_values(keyword: str, value: object) -> None:
    with pytest.raises(WorkspaceError, match="must be a bool"):
        WorkspaceCapabilities(**{keyword: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("executable", [1, 0, "true", None])
def test_workspace_entries_require_exact_bool_executable_mode(executable: object) -> None:
    with pytest.raises(WorkspaceError, match="executable mode must be a bool"):
        WorkspaceEntry.file("run", b"payload", executable=executable)  # type: ignore[arg-type]


def test_construction_enforces_parent_containment_independent_of_entry_order() -> None:
    with pytest.raises(WorkspaceError, match="parent"):
        InMemoryWorkspace(
            [
                WorkspaceEntry.file("parent/child", b"payload"),
                WorkspaceEntry.file("parent", b"not a directory"),
            ]
        )


def test_immutable_workspace_cannot_mutate_but_can_seed_a_mutable_candidate() -> None:
    immutable = InMemoryWorkspace([WorkspaceEntry.file("original", b"one")], mutable=False)

    mutations = (
        lambda: immutable.write("new", b"two"),
        lambda: immutable.mkdir("directory"),
        lambda: immutable.symlink("link", "original"),
        lambda: immutable.copy_from(immutable, "original", "copied"),
        lambda: immutable.delete("original"),
    )
    for mutation in mutations:
        with pytest.raises(WorkspaceImmutableError):
            mutation()

    candidate = immutable.mutable_copy()
    candidate.write("new", b"two")
    assert immutable.list_entries() == (WorkspaceEntry.file("original", b"one"),)
    assert candidate.read("new") == b"two"


def test_write_and_copy_cannot_cross_a_non_directory_parent() -> None:
    workspace = InMemoryWorkspace([WorkspaceEntry.file("parent", b"not a directory")])

    with pytest.raises(WorkspaceError, match="parent"):
        workspace.write("parent/child", b"payload")
    with pytest.raises(WorkspaceError, match="parent"):
        workspace.copy_from(InMemoryWorkspace([WorkspaceEntry.file("child", b"payload")]), "child", "parent/child")


def test_copy_preserves_a_directory_subtree_and_payload_metadata() -> None:
    source = InMemoryWorkspace(
        [
            WorkspaceEntry.directory("source"),
            WorkspaceEntry.file("source/run", b"payload", executable=True),
            WorkspaceEntry.symlink("source/link", "run"),
        ],
        mutable=False,
    )
    destination = InMemoryWorkspace()

    destination.copy_from(source, "source", "copied")

    assert destination.list_entries() == (
        WorkspaceEntry.directory("copied"),
        WorkspaceEntry.symlink("copied/link", "run"),
        WorkspaceEntry.file("copied/run", b"payload", executable=True),
    )


def test_copy_never_silently_merges_with_existing_entries() -> None:
    source = InMemoryWorkspace([WorkspaceEntry.file("source", b"new")], mutable=False)
    destination = InMemoryWorkspace([WorkspaceEntry.file("destination", b"old")])

    with pytest.raises(WorkspaceError, match="already contains"):
        destination.copy_from(source, "source", "destination")
    assert destination.read("destination") == b"old"


def test_delete_requires_recursion_for_non_empty_or_implicit_directories() -> None:
    workspace = InMemoryWorkspace([WorkspaceEntry.file("directory/child", b"payload")])

    with pytest.raises(WorkspaceError, match="not empty"):
        workspace.delete("directory")
    workspace.delete("directory", recursive=True)
    with pytest.raises(WorkspaceEntryNotFoundError):
        workspace.get_entry("directory/child")


def test_regular_file_reads_are_typed() -> None:
    workspace = InMemoryWorkspace([WorkspaceEntry.symlink("link", "target")])

    with pytest.raises(WorkspaceError, match="not a regular file"):
        workspace.read("link")
    assert workspace.get_entry("link").kind is WorkspaceEntryKind.SYMLINK
