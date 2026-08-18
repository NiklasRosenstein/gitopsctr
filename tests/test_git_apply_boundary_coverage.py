"""Fail-closed coverage for Git apply input and authority boundaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from dulwich.repo import Repo

from gitopsctr.adapters.git import apply as git_apply
from gitopsctr.adapters.git.apply import UnsupportedGitPublicationAuthority
from gitopsctr.application.model import SourceId
from gitopsctr.application.sources import SourceRequest
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry
from gitopsctr.errors import OperationError


def _git_repository(path: Path, *, bare: bool = False) -> Path:
    if bare:
        path.mkdir()
        Repo.init_bare(path).close()
    else:
        Repo.init(path, mkdir=True).close()
    return path


def test_source_selector_is_optional_and_rejects_a_foreign_source() -> None:
    expected = SourceId("expected-source")

    assert git_apply._source_selector(None, expected) is None
    with pytest.raises(ValueError, match="not configured for source"):
        git_apply._source_selector(SourceRequest(SourceId("foreign-source"), "main"), expected)


def test_local_publication_authority_accepts_file_and_relative_urls_and_rejects_open_targets(
    tmp_path: Path,
) -> None:
    working = _git_repository(tmp_path / "working")
    with pytest.raises(UnsupportedGitPublicationAuthority, match="requires origin"):
        git_apply.local_bare_publication_authority(working)

    authority = _git_repository(tmp_path / "authority.git", bare=True)
    subprocess.run(
        ("git", "-C", str(working), "remote", "add", "origin", f"file://localhost{authority}"),
        check=True,
    )
    assert git_apply.local_bare_publication_authority(working) == authority.resolve()

    subprocess.run(
        ("git", "-C", str(working), "remote", "set-url", "origin", "git@example.test:project.git"),
        check=True,
    )
    with pytest.raises(UnsupportedGitPublicationAuthority, match="non-local origins"):
        git_apply.local_bare_publication_authority(working)

    non_bare = _git_repository(tmp_path / "non-bare")
    subprocess.run(
        ("git", "-C", str(working), "remote", "set-url", "origin", str(non_bare)),
        check=True,
    )
    with pytest.raises(UnsupportedGitPublicationAuthority, match="local bare"):
        git_apply.local_bare_publication_authority(working)

    nested_authority = _git_repository(working / "nested.git", bare=True)
    subprocess.run(
        ("git", "-C", str(working), "remote", "set-url", "origin", "nested.git"),
        check=True,
    )
    assert git_apply.local_bare_publication_authority(working) == nested_authority.resolve()


def test_retention_root_and_identity_seed_reject_unsafe_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    retention = git_apply._retention_root(repository)
    retention.write_bytes(b"not a directory")
    with pytest.raises(OperationError, match="private directory"):
        git_apply._ensure_retention_root(repository)

    secured_repository = tmp_path / "secured"
    secured_repository.mkdir()
    original_chmod = os.chmod

    def fail_chmod(_path: object, _mode: object) -> None:
        raise OSError("injected chmod failure")

    monkeypatch.setattr(git_apply.os, "chmod", fail_chmod)
    with pytest.raises(OperationError, match="cannot be secured"):
        git_apply._ensure_retention_root(secured_repository)
    monkeypatch.setattr(git_apply.os, "chmod", original_chmod)

    authority = tmp_path / "authority.git"
    authority.mkdir()
    seed = git_apply._load_identity_seed(authority)
    assert len(seed) == 64
    key = authority.parent / f".{authority.name}{git_apply._IDENTITY_KEY_SUFFIX}"
    key.chmod(0o644)
    with pytest.raises(OperationError, match="private regular file"):
        git_apply._load_identity_seed(authority)

    invalid_authority = tmp_path / "invalid.git"
    invalid_authority.mkdir()
    invalid_key = invalid_authority.parent / f".{invalid_authority.name}{git_apply._IDENTITY_KEY_SUFFIX}"
    invalid_key.write_text("z" * 64)
    invalid_key.chmod(0o600)
    with pytest.raises(OperationError, match="identity key is invalid"):
        git_apply._load_identity_seed(invalid_authority)


def test_cache_failure_and_logical_workspace_input_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _git_repository(tmp_path / "working")
    with pytest.raises(OperationError, match="cannot be cached"):
        git_apply._cache_published_snapshot(repository, "0" * 40)

    monkeypatch.chdir(repository)
    workspace = InMemoryWorkspace(
        (
            WorkspaceEntry.file("inputs/a.yaml", b"kind: A\n"),
            WorkspaceEntry.file("inputs/b.json", b'{"kind":"B"}'),
            WorkspaceEntry.file("inputs/readme.txt", b"ignored"),
        ),
        mutable=False,
    )
    loaded = git_apply._load_workspace_documents(repository, workspace, ("inputs",))
    assert [item.document["kind"] for item in loaded] == ["A", "B"]

    with pytest.raises(OperationError, match="at least one input"):
        git_apply._load_workspace_documents(repository, workspace, ())
    with pytest.raises(OperationError, match="standard input"):
        git_apply._load_workspace_documents(repository, workspace, ("-",))
    with pytest.raises(OperationError, match="must be YAML or JSON"):
        git_apply._load_workspace_documents(repository, workspace, ("inputs/readme.txt",))
    with pytest.raises(OperationError, match="does not exist"):
        git_apply._load_workspace_documents(repository, workspace, ("missing",))
    with pytest.raises(OperationError, match="outside the project repository"):
        git_apply._logical_source_key(repository, str(tmp_path / "outside.yaml"))
    with pytest.raises(OperationError, match="is invalid"):
        git_apply._logical_source_key(repository, ".")


def test_stdin_document_decoder_preserves_document_boundaries_and_rejects_open_yaml() -> None:
    documents = git_apply._issued_stdin_documents("kind: First\n---\nkind: Second\n")
    assert [item.origin for item in documents] == ["stdin#1", "stdin#2"]
    assert [item.document["kind"] for item in documents] == ["First", "Second"]
    assert documents[0].content_id != documents[1].content_id

    with pytest.raises(OperationError, match="invalid YAML"):
        git_apply._issued_stdin_documents("value: [unterminated")
    with pytest.raises(OperationError, match="must be a resource mapping"):
        git_apply._issued_stdin_documents("- list\n- value\n")
    with pytest.raises(OperationError, match="document 1 is invalid"):
        git_apply._issued_stdin_documents("when: 2026-08-18\n")
