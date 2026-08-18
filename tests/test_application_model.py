from __future__ import annotations

import copy
import json
import pickle

import pytest

from gitopsctr.application import (
    AcceptedDesiredSnapshot,
    AuthorityIssuer,
    AuthorityObservation,
    ChannelId,
    ContentId,
    EffectAuthorization,
    EffectIntent,
    EffectKind,
    EffectLeaseToken,
    EnvironmentId,
    HeadObservation,
    OwnershipId,
    OwnershipObservation,
    PublicationAttemptId,
    PublicationIntent,
    PublicationMode,
    RetainedSource,
    RetainedSourceHandle,
    SealedCandidate,
    SealedCandidateHandle,
    SnapshotId,
    SourceId,
    SourceOwnershipRequirement,
    SourceSnapshotId,
    ValidateCommand,
)
from gitopsctr.application.model import _issue_accepted_desired_snapshot, _issue_effect_authorization


def accepted_snapshot() -> AcceptedDesiredSnapshot:
    channel = ChannelId("desired-production")
    snapshot = SnapshotId("snapshot-1")
    return _issue_accepted_desired_snapshot(
        EnvironmentId("production"),
        AuthorityIssuer("project-authority"),
        AuthorityObservation("authority-v1"),
        channel,
        HeadObservation.present(channel, snapshot, "head-incarnation-1"),
        snapshot,
    )


def forged_accepted_snapshot(accepted: AcceptedDesiredSnapshot) -> AcceptedDesiredSnapshot:
    """Build an object that resembles acceptance but has no trusted marker."""

    forged = object.__new__(AcceptedDesiredSnapshot)
    object.__setattr__(forged, "environment_id", accepted.environment_id)
    object.__setattr__(forged, "issuer", accepted.issuer)
    object.__setattr__(forged, "authority_observation", accepted.authority_observation)
    object.__setattr__(forged, "channel_id", accepted.channel_id)
    object.__setattr__(forged, "head_observation", accepted.head_observation)
    object.__setattr__(forged, "snapshot_id", accepted.snapshot_id)
    object.__setattr__(forged, "_issuance", object())
    return forged


def test_opaque_values_are_frozen_equal_by_type_and_canonically_serialized():
    snapshot = SnapshotId("state-a")

    assert snapshot == SnapshotId("state-a")
    assert snapshot != ContentId("state-a")
    assert snapshot.to_wire() == "state-a"
    with pytest.raises(AttributeError):
        snapshot.value = "state-b"  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", " whitespace", "whitespace ", "newline\nvalue", 3])
def test_opaque_values_reject_noncanonical_construction(value: object):
    with pytest.raises(ValueError):
        SnapshotId(value)  # type: ignore[arg-type]


def test_head_observations_include_absence_and_an_aba_fence():
    channel = ChannelId("desired-production")
    first = HeadObservation.present(channel, SnapshotId("state-a"), "incarnation-1")
    absent = HeadObservation.absent(channel, "incarnation-2")
    second = HeadObservation.present(channel, SnapshotId("state-a"), "incarnation-3")

    assert absent.is_absent
    assert absent.snapshot_id is None
    assert first != second
    assert first.to_wire() != second.to_wire()


def test_accepted_snapshot_is_issuance_controlled_and_has_no_double_encoded_wire_data():
    accepted = accepted_snapshot()
    assert accepted.snapshot_id == SnapshotId("snapshot-1")
    assert json.loads(accepted.to_wire())["head"] == {
        "channel": "desired-production",
        "incarnation": "head-incarnation-1",
        "snapshot": "snapshot-1",
    }

    with pytest.raises(TypeError, match="must be issued"):
        AcceptedDesiredSnapshot(
            accepted.environment_id,
            accepted.issuer,
            accepted.authority_observation,
            ChannelId("desired-staging"),
            accepted.head_observation,
            accepted.snapshot_id,
        )
    with pytest.raises(TypeError, match="issuance proof"):
        forged_accepted_snapshot(accepted).to_wire()
    with pytest.raises(TypeError, match="must not be copied"):
        copy.copy(accepted)
    with pytest.raises(TypeError, match="must not be copied"):
        copy.deepcopy(accepted)
    with pytest.raises(TypeError, match="must not be serialized"):
        pickle.dumps(accepted)
    assert forged_accepted_snapshot(accepted) != accepted
    assert len({forged_accepted_snapshot(accepted), accepted}) == 2

    with pytest.raises(TypeError, match="must not be subclassed"):

        class ForgedAcceptedSnapshot(AcceptedDesiredSnapshot):
            pass


def test_authority_issuance_validates_environment_authority_channel_and_head_binding():
    accepted = accepted_snapshot()

    with pytest.raises(ValueError, match="absent"):
        _issue_accepted_desired_snapshot(
            accepted.environment_id,
            accepted.issuer,
            accepted.authority_observation,
            accepted.channel_id,
            HeadObservation.absent(accepted.channel_id, "incarnation-2"),
            accepted.snapshot_id,
        )
    with pytest.raises(ValueError, match="match its observed"):
        _issue_accepted_desired_snapshot(
            accepted.environment_id,
            accepted.issuer,
            accepted.authority_observation,
            accepted.channel_id,
            accepted.head_observation,
            SnapshotId("different-snapshot"),
        )


def test_effect_and_publication_intents_preserve_authorization_and_exact_head():
    accepted = accepted_snapshot()
    effect = EffectIntent(
        EffectKind.RECONCILE,
        "stack/api",
        "resource-uid-1",
    )
    authorization = _issue_effect_authorization(
        effect,
        accepted,
        accepted.snapshot_id,
        EffectLeaseToken("lease-token-1"),
        2,
    )
    source = RetainedSource(
        RetainedSourceHandle("retained-source-1"),
        SourceSnapshotId(SourceId("source-repository"), SnapshotId("source-snapshot")),
    )
    requirement = SourceOwnershipRequirement(
        source, OwnershipObservation.present(OwnershipId("accepted-owner"), "owner-v1")
    )
    intent = PublicationIntent(
        PublicationAttemptId("publication-attempt-1"),
        accepted.channel_id,
        accepted.head_observation,
        SealedCandidate(
            SealedCandidateHandle("candidate-handle-1"),
            SnapshotId("candidate-snapshot"),
            ContentId("candidate-content"),
        ),
        (source,),
        OwnershipId("accepted-owner"),
        (requirement,),
        PublicationMode.FENCED_CONTINUATION,
        authorization,
    )
    assert intent.effect_authorization is authorization

    with pytest.raises(ValueError, match="expected head channel"):
        PublicationIntent(
            PublicationAttemptId("publication-attempt-2"),
            ChannelId("other-channel"),
            accepted.head_observation,
            intent.candidate,
            (),
            OwnershipId("accepted-owner"),
            (),
            PublicationMode.DIRECT_ACCEPTED,
        )
    with pytest.raises(TypeError, match="must be issued"):
        EffectAuthorization(
            effect,
            accepted,
            accepted.snapshot_id,
            EffectLeaseToken("forged-token"),
            2,
        )
    with pytest.raises(TypeError, match="must not be subclassed"):

        class ForgedEffectAuthorization(EffectAuthorization):
            pass

    with pytest.raises(ValueError, match="requires an effect authorization"):
        PublicationIntent(
            PublicationAttemptId("publication-attempt-3"),
            accepted.channel_id,
            accepted.head_observation,
            intent.candidate,
            (source,),
            OwnershipId("accepted-owner"),
            (requirement,),
            PublicationMode.FENCED_CONTINUATION,
        )


def test_effect_authorization_and_publication_ownership_reject_inconsistent_inputs():
    accepted = accepted_snapshot()
    effect = EffectIntent(EffectKind.TEARDOWN, "stack/api", "resource-uid-1")
    with pytest.raises(ValueError, match="match the accepted"):
        _issue_effect_authorization(
            effect,
            accepted,
            SnapshotId("other-input"),
            EffectLeaseToken("lease-token-1"),
            1,
        )

    source_id = SourceSnapshotId(SourceId("source-repository"), SnapshotId("source-snapshot"))
    retained = RetainedSource(RetainedSourceHandle("retained-source-1"), source_id)
    mismatched = RetainedSource(RetainedSourceHandle("retained-source-2"), source_id)
    with pytest.raises(ValueError, match="retained source handle"):
        PublicationIntent(
            PublicationAttemptId("publication-attempt-4"),
            accepted.channel_id,
            accepted.head_observation,
            SealedCandidate(
                SealedCandidateHandle("candidate-handle-1"),
                SnapshotId("candidate-snapshot"),
                ContentId("candidate-content"),
            ),
            (retained,),
            OwnershipId("accepted-owner"),
            (SourceOwnershipRequirement(mismatched, OwnershipObservation.absent("owner-v1")),),
            PublicationMode.DIRECT_ACCEPTED,
        )


def test_ownership_observations_fence_absence_aba_and_allow_idempotent_owner():
    owner = OwnershipId("accepted-owner")
    first = OwnershipObservation.present(owner, "owner-v1")
    absent = OwnershipObservation.absent("owner-v2")
    second = OwnershipObservation.present(owner, "owner-v3")
    assert absent.is_absent
    assert first != second
    assert first.to_wire() != second.to_wire()

    accepted = accepted_snapshot()
    retained = RetainedSource(
        RetainedSourceHandle("retained-source-1"),
        SourceSnapshotId(SourceId("source-repository"), SnapshotId("source-snapshot")),
    )
    publication = PublicationIntent(
        PublicationAttemptId("publication-attempt-5"),
        accepted.channel_id,
        accepted.head_observation,
        SealedCandidate(
            SealedCandidateHandle("candidate-handle-1"),
            SnapshotId("candidate-snapshot"),
            ContentId("candidate-content"),
        ),
        (retained,),
        owner,
        (SourceOwnershipRequirement(retained, first),),
        PublicationMode.DIRECT_ACCEPTED,
    )
    assert publication.publication_owner == first.owner

    other = RetainedSource(
        RetainedSourceHandle("retained-source-2"),
        SourceSnapshotId(SourceId("other-source"), SnapshotId("other-source-snapshot")),
    )
    with pytest.raises(ValueError, match="cover exactly"):
        PublicationIntent(
            PublicationAttemptId("publication-attempt-coverage"),
            accepted.channel_id,
            accepted.head_observation,
            publication.candidate,
            (retained, other),
            owner,
            (SourceOwnershipRequirement(retained, first),),
            PublicationMode.DIRECT_ACCEPTED,
        )


def test_publication_rejects_forged_nested_acceptance_proof():
    accepted = accepted_snapshot()
    effect = EffectIntent(EffectKind.RECONCILE, "stack/api", "resource-uid-1")
    authorization = _issue_effect_authorization(
        effect,
        accepted,
        accepted.snapshot_id,
        EffectLeaseToken("lease-token-1"),
        2,
    )
    object.__setattr__(authorization, "accepted_desired_snapshot", forged_accepted_snapshot(accepted))
    retained = RetainedSource(
        RetainedSourceHandle("retained-source-1"),
        SourceSnapshotId(SourceId("source-repository"), SnapshotId("source-snapshot")),
    )
    with pytest.raises(TypeError, match="issuance proof"):
        PublicationIntent(
            PublicationAttemptId("publication-attempt-6"),
            accepted.channel_id,
            accepted.head_observation,
            SealedCandidate(
                SealedCandidateHandle("candidate-handle-1"),
                SnapshotId("candidate-snapshot"),
                ContentId("candidate-content"),
            ),
            (retained,),
            OwnershipId("accepted-owner"),
            (SourceOwnershipRequirement(retained, OwnershipObservation.absent("owner-v1")),),
            PublicationMode.FENCED_CONTINUATION,
            authorization,
        )


def test_validate_command_uses_only_logical_input_labels():
    command = ValidateCommand(("stdin", "manifests/changeset-1"), (EnvironmentId("production"),), fail_fast=True)
    assert command.fail_fast

    with pytest.raises(ValueError, match="logical label"):
        ValidateCommand(("../project.yaml",))
