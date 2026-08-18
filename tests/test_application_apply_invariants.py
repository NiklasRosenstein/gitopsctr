"""Application-boundary invariants for typed apply dispatch.

These tests deliberately exercise the small facade without involving a Git
checkout.  They make sure untrusted inbound values cannot be smuggled across
the application boundary and that the facade neither re-decodes nor
reinterprets a decoded change set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from gitopsctr.application.apply import (
    ApplyCommand,
    ApplyResult,
    AuthoredChangeSet,
    AuthoredDocument,
    _issue_authored_document,
)
from gitopsctr.application.apply_projection import (
    SourceBindingRole,
    _issue_retained_source_descriptor,
)
from gitopsctr.application.model import (
    CandidateStoreId,
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    PublicationTarget,
    RetainedSourceHandle,
    RetentionStoreId,
    SealedCandidateHandle,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
    _issue_retained_source,
    _issue_sealed_candidate,
)
from gitopsctr.application.services import ApplicationServices
from gitopsctr.application.sources import SourceRequest
from gitopsctr.resource_api import JsonObject


def _document(name: str = "app", *, kind: str = "Terraform") -> AuthoredDocument:
    return _issue_authored_document(
        f"input:{name}",
        {
            "apiVersion": "gitopsctr.io/v1" if kind in {"Stack", "StackTemplate"} else "unit.gitopsctr.io/v1",
            "kind": kind,
            "metadata": {"name": name},
        },
        ContentId(f"sha256:{name}"),
    )


def _command(**overrides: object) -> ApplyCommand:
    values: dict[str, object] = {
        "environment_id": EnvironmentId("dev"),
        "input_labels": ("input:app",),
        "desired_channel": ChannelId("desired/dev"),
        "observed_channel": ChannelId("observed/dev"),
        "candidate_channel": ChannelId("candidate/dev"),
        "source_request": None,
        "partition": None,
        "dry_run": False,
        "verbose": False,
    }
    values.update(overrides)
    return ApplyCommand(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("origin", " input:app", ValueError),
        ("content_id", "sha256:not-a-content-id", TypeError),
        ("_document_wire", "[]", TypeError),
        ("_issuance", object(), TypeError),
    ),
)
def test_change_set_revalidates_every_decoder_issued_document_after_tampering(
    field: str, value: object, error: type[Exception]
) -> None:
    document = _document()
    object.__setattr__(document, field, value)

    with pytest.raises(error):
        AuthoredChangeSet((document,))


@pytest.mark.parametrize(
    "document",
    (
        {"kind": "Terraform", "metadata": {"name": "app"}},
        {"apiVersion": "unit.gitopsctr.io/v1", "metadata": {"name": "app"}},
        {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "metadata": {}},
        {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "metadata": {"name": 1}},
    ),
)
def test_change_set_rejects_malformed_resource_identity(document: dict[str, object]) -> None:
    authored = _issue_authored_document("input:bad", cast(JsonObject, document), ContentId("sha256:bad"))

    with pytest.raises(ValueError, match="resource requires"):
        AuthoredChangeSet((authored,))


def test_change_set_requires_tuple_typed_documents_and_typed_source_snapshot() -> None:
    with pytest.raises(TypeError, match="tuple"):
        AuthoredChangeSet(cast(Any, [_document()]))
    with pytest.raises(TypeError, match="AuthoredDocument"):
        AuthoredChangeSet(cast(Any, (object(),)))
    with pytest.raises(TypeError, match="source_snapshot_id"):
        AuthoredChangeSet((_document(),), cast(Any, SnapshotId("not-a-source-snapshot")))


def test_change_set_rejects_duplicate_unit_family_but_keeps_distinct_root_families() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        AuthoredChangeSet((_document("same", kind="Terraform"), _document("same", kind="Kubernetes")))

    changes = AuthoredChangeSet((_document("same", kind="Stack"), _document("same", kind="Terraform")))
    assert tuple(item.document["kind"] for item in changes.documents) == ("Stack", "Terraform")


@pytest.mark.parametrize(
    "overrides",
    (
        {"environment_id": "dev"},
        {"input_labels": ["input:app"]},
        {"input_labels": (" input:app",)},
        {"input_labels": ("input\x00app",)},
        {"desired_channel": "desired/dev"},
        {"observed_channel": "observed/dev"},
        {"candidate_channel": "candidate/dev"},
        {"source_request": object()},
        {"partition": " team-a"},
        {"dry_run": 1},
        {"verbose": 0},
    ),
)
def test_apply_command_rejects_untyped_or_noncanonical_intent_fields(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _command(**overrides)


def test_apply_command_keeps_only_typed_source_selection() -> None:
    request = SourceRequest(SourceId("source-a"), "refs/heads/main")
    command = _command(source_request=request, partition="team-a", dry_run=True, verbose=True)

    assert command.source_request is request
    assert command.partition == "team-a"
    assert command.dry_run and command.verbose


def _publication_intent() -> PublicationIntent:
    channel = ChannelId("desired/dev")
    candidate = _issue_sealed_candidate(
        SealedCandidateHandle("candidate-handle"),
        CandidateStoreId("candidate-store"),
        SnapshotId("candidate-snapshot"),
        ContentId("candidate-content"),
    )
    return PublicationIntent(
        PublicationAttemptId("attempt"),
        channel,
        HeadObservation.absent(channel, "absent-incarnation"),
        candidate,
        (),
        OwnershipId("apply-owner"),
        (),
        PublicationTarget.ACCEPTED_DESIRED,
        PublicationMode.DIRECT_ACCEPTED,
    )


def test_apply_result_binds_the_sealed_candidate_snapshot_and_publication_mode() -> None:
    intent = _publication_intent()

    result = ApplyResult(intent.candidate.snapshot_id, intent.mode, intent)
    assert result.publication is intent
    with pytest.raises(ValueError, match="sealed publication candidate"):
        ApplyResult(SnapshotId("other-snapshot"), intent.mode, intent)
    with pytest.raises(ValueError, match="publication mode"):
        ApplyResult(intent.candidate.snapshot_id, PublicationMode.REVIEW_REQUIRED, intent)
    with pytest.raises(TypeError, match="SnapshotId"):
        ApplyResult(cast(Any, "snapshot"), None)


def test_retained_source_descriptor_detects_tampered_provenance_at_use_boundary() -> None:
    source_snapshot = SourceSnapshotId(SourceId("source-a"), SnapshotId("source-snapshot"))
    retained = _issue_retained_source(
        RetainedSourceHandle("retained-handle"),
        RetentionStoreId("retention-store"),
        source_snapshot,
        ContentId("source-content"),
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "primary",
        SourceBindingRole.PRIMARY_AUTHORED,
        "inputs/app.yaml",
        ContentId("selector-evidence"),
    )

    object.__setattr__(descriptor, "workspace_key", "inputs/other.yaml")
    with pytest.raises(TypeError, match="modified"):
        descriptor._validate()


@dataclass
class _Port:
    events: list[str]
    name: str
    close_error: Exception | None = None

    def close(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


@dataclass
class _Decoder(_Port):
    changes: AuthoredChangeSet | None = None
    decode_error: Exception | None = None

    def decode(self, command: ApplyCommand) -> AuthoredChangeSet:
        self.events.append(f"decode:{command.environment_id.value}")
        if self.decode_error is not None:
            raise self.decode_error
        assert self.changes is not None
        return self.changes


@dataclass
class _Apply(_Port):
    received: list[tuple[ApplyCommand, AuthoredChangeSet]] = field(default_factory=list)

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        self.events.append(f"apply:{command.environment_id.value}")
        self.received.append((command, changes))
        return ApplyResult(SnapshotId("applied"), None)


def _services(events: list[str], apply: _Apply | None, decoder: _Decoder | None) -> ApplicationServices:
    return ApplicationServices(
        cast(Any, _Port(events, "snapshot")),
        cast(Any, _Port(events, "validator")),
        cast(Any, _Port(events, "resources")),
        cast(Any, _Port(events, "status")),
        cast(Any, _Port(events, "dependencies")),
        apply,
        decoder,
    )


def test_apply_facade_decodes_once_then_dispatches_the_exact_command_and_change_set() -> None:
    events: list[str] = []
    changes = AuthoredChangeSet((_document(),), SourceSnapshotId(SourceId("source-a"), SnapshotId("source-snapshot")))
    decoder = _Decoder(events, "decoder", changes=changes)
    apply = _Apply(events, "apply")
    services = _services(events, apply, decoder)
    command = _command(source_request=SourceRequest(SourceId("foreign-source"), "main"))

    assert services.apply(command) == ApplyResult(SnapshotId("applied"), None)
    assert events == ["decode:dev", "apply:dev"]
    assert apply.received == [(command, changes)]


def test_apply_facade_supplied_changes_bypass_decoder_and_decoder_failure_short_circuits_dispatch() -> None:
    events: list[str] = []
    changes = AuthoredChangeSet((_document(),))
    decoder = _Decoder(events, "decoder", changes=changes, decode_error=RuntimeError("decode failed"))
    apply = _Apply(events, "apply")
    services = _services(events, apply, decoder)

    assert services.apply(_command(), changes).snapshot_id == SnapshotId("applied")
    assert events == ["apply:dev"]
    with pytest.raises(RuntimeError, match="decode failed"):
        services.apply(_command())
    assert events == ["apply:dev", "decode:dev"]
    assert len(apply.received) == 1


def test_apply_facade_rejects_missing_dependencies_before_attempting_decode() -> None:
    events: list[str] = []
    decoder = _Decoder(events, "decoder", changes=AuthoredChangeSet((_document(),)))

    with pytest.raises(RuntimeError, match="does not provide apply"):
        _services(events, None, decoder).apply(_command())
    assert events == []

    with pytest.raises(RuntimeError, match="does not provide authored input decoding"):
        _services(events, _Apply(events, "apply"), None).apply(_command())
    assert events == []


def test_apply_facade_closes_all_ports_in_fixed_order_after_decoder_close_failure() -> None:
    events: list[str] = []
    decoder = _Decoder(
        events,
        "decoder",
        close_error=RuntimeError("decoder close failed"),
        changes=AuthoredChangeSet((_document(),)),
    )
    apply = _Apply(events, "apply")
    services = _services(events, apply, decoder)

    with pytest.raises(RuntimeError, match="decoder close failed"):
        services.close()
    services.close()

    assert events == [
        "close:decoder",
        "close:apply",
        "close:dependencies",
        "close:status",
        "close:resources",
        "close:validator",
        "close:snapshot",
    ]
