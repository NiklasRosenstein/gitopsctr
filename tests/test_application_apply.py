from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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
):
    """A selector can move after decode without mixing source snapshots."""

    from gitopsctr import controller
    from gitopsctr.adapters.git.apply import GitApplyService, GitAuthoredChangeDecoder

    first_revision = "a" * 40
    second_revision = "b" * 40
    selected = {"revision": first_revision}

    monkeypatch.setattr(
        controller,
        "git",
        lambda *_args: SimpleNamespace(stdout=f"{selected['revision']}\n"),
    )
    monkeypatch.setattr(controller, "_validate_apply_input_selection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "materialize_revision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "_load_apply_documents",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                origin="source:unit.yaml",
                document={
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "metadata": {"name": "app"},
                },
                document_digest="sha256:app",
            )
        ],
    )
    command = ApplyCommand(
        EnvironmentId("dev"),
        ("unit.yaml",),
        ChannelId("desired-dev"),
        ChannelId("observed-dev"),
        None,
        SourceRequest(SourceId("default-git-source"), "refs/heads/main"),
    )
    changes = GitAuthoredChangeDecoder(Path(".")).decode(command)
    assert changes.source_snapshot_id is not None
    assert changes.source_snapshot_id.snapshot_id == SnapshotId(first_revision)

    selected["revision"] = second_revision
    observed: dict[str, str | None] = {}

    def execute(arguments: SimpleNamespace, *, documents: object) -> None:
        observed["source_revision"] = arguments.source_revision
        return None

    monkeypatch.setattr(controller, "_execute_git_apply", execute)
    GitApplyService(Path(".")).apply(command, changes)

    assert observed["source_revision"] == first_revision


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
