from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gitopsctr.application.apply import (
    ApplyCommand,
    ApplyResult,
    AuthoredChangeSet,
    AuthoredDocument,
    _issue_authored_document,
)
from gitopsctr.application.model import ChannelId, ContentId, EnvironmentId, PublicationMode, SnapshotId, SourceId
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.sources import SourceRequest


def _document(name: str) -> AuthoredDocument:
    return _issue_authored_document(
        f"input:{name}",
        {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "metadata": {"name": name}},
        ContentId(f"sha256:{name}"),
    )


def test_authored_change_set_rejects_duplicate_family_identity():
    with pytest.raises(ValueError, match="duplicate"):
        AuthoredChangeSet((_document("app"), _document("app")))


def test_authored_document_is_decoder_issued_and_cannot_be_mutated_through_its_input_mapping():
    original = {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "metadata": {"name": "app"}}
    issued = _issue_authored_document("input:app", original, ContentId("sha256:app"))
    original["metadata"]["name"] = "changed"  # type: ignore[index]

    assert issued.document["metadata"] == {"name": "app"}
    with pytest.raises(TypeError, match="AuthoredChangeDecoder"):
        AuthoredDocument("input:forged", original, ContentId("sha256:forged"))


def test_apply_result_cannot_mislabeled_an_unbacked_publication():
    with pytest.raises(TypeError, match="publication_mode"):
        ApplyResult(SnapshotId("snapshot"), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires a publication"):
        ApplyResult(SnapshotId("snapshot"), PublicationMode.DIRECT_ACCEPTED)


@dataclass
class _Decoder:
    changes: AuthoredChangeSet
    closed: bool = False

    def decode(self, _command: ApplyCommand) -> AuthoredChangeSet:
        return self.changes

    def close(self) -> None:
        self.closed = True


@dataclass
class _Apply:
    received: AuthoredChangeSet | None = None
    closed: bool = False

    def apply(self, _command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        self.received = changes
        return ApplyResult(SnapshotId("applied-snapshot"), None)

    def close(self) -> None:
        self.closed = True


@dataclass
class _ClosedPort:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


def test_application_decodes_before_delegating_apply_and_closes_all_ports():
    changes = AuthoredChangeSet((_document("app"),))
    decoder = _Decoder(changes)
    apply = _Apply()
    snapshot = _ClosedPort()
    validator = _ClosedPort()
    resources = _ClosedPort()
    status = _ClosedPort()
    dependencies = _ClosedPort()
    application = ApplicationServices(snapshot, validator, resources, status, dependencies, apply, decoder)
    command = ApplyCommand(
        EnvironmentId("dev"),
        ("input:app",),
        ChannelId("desired-dev"),
        ChannelId("observed-dev"),
        None,
        None,
    )

    assert application.apply(command).snapshot_id == SnapshotId("applied-snapshot")
    assert apply.received == changes
    application.close()

    assert all(port.closed for port in (decoder, apply, snapshot, validator, resources, status, dependencies))


def test_git_apply_uses_the_exact_source_snapshot_decoded_before_a_moving_ref_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """A selector can move after decode without mixing source snapshots."""

    from gitopsctr.adapters.git.apply import GitAuthoredChangeDecoder
    from gitopsctr.adapters.memory.sources import MemorySourceRepository
    from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry

    source = MemorySourceRepository(SourceId("default-git-source"))
    first = source.install(
        SnapshotId("first"),
        InMemoryWorkspace(
            (
                WorkspaceEntry.file(
                    "unit.yaml",
                    b"apiVersion: unit.gitopsctr.io/v1\nkind: Terraform\nmetadata:\n  name: app\nspec: {}\n",
                ),
            ),
            mutable=False,
        ),
    )
    second = source.install(
        SnapshotId("second"),
        InMemoryWorkspace((WorkspaceEntry.file("unit.yaml", b"kind: changed\n"),), mutable=False),
    )
    source.set_selector("refs/heads/main", first.source_snapshot_id.snapshot_id)
    monkeypatch.chdir(tmp_path)
    command = ApplyCommand(
        EnvironmentId("dev"),
        ("unit.yaml",),
        ChannelId("desired-dev"),
        ChannelId("observed-dev"),
        None,
        SourceRequest(SourceId("default-git-source"), "refs/heads/main"),
    )
    changes = GitAuthoredChangeDecoder(tmp_path, source_repository=source).decode(command)
    assert changes.source_snapshot_id is not None
    assert changes.source_snapshot_id.snapshot_id == SnapshotId("first")
    source.set_selector("refs/heads/main", second.source_snapshot_id.snapshot_id)

    assert changes.source_acquisition is not None
    assert source.recover(changes.source_acquisition.retained).source_snapshot_id == first.source_snapshot_id


def test_git_apply_rejects_a_source_request_from_another_source() -> None:
    from gitopsctr.adapters.git.apply import GitAuthoredChangeDecoder

    command = ApplyCommand(
        EnvironmentId("dev"),
        ("unit.yaml",),
        None,
        None,
        None,
        SourceRequest(SourceId("foreign-source"), "main"),
    )
    with pytest.raises(ValueError, match="not configured for source"):
        GitAuthoredChangeDecoder(Path(".")).decode(command)
