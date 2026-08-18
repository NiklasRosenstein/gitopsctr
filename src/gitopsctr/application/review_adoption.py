"""Authenticated adoption of one committed review candidate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gitopsctr.application.apply_orchestration import ApplyPublicationAuthority, ApplyPublicationIdentityIssuer
from gitopsctr.application.model import (
    ChannelId,
    CoordinationChange,
    EnvironmentId,
    PublicationIntent,
    PublicationMode,
    PublicationOutcome,
    PublicationOutcomeState,
    PublicationProof,
    PublicationRecoveryLocator,
    PublicationTarget,
    SnapshotId,
    SourceOwnershipChange,
)
from gitopsctr.application.ports import PublicationExecutionUnknownError, PublicationRecoveryNotFoundError


class ReviewAdoptionError(ValueError):
    """A review candidate lacks the exact authority required for adoption."""


@dataclass(frozen=True, slots=True)
class ReviewAdoptionConfiguration:
    """Environment-owned channels permitted for reviewed adoption."""

    desired_channel: ChannelId
    candidate_channel: ChannelId

    def __post_init__(self) -> None:
        if not isinstance(self.desired_channel, ChannelId) or not isinstance(self.candidate_channel, ChannelId):
            raise TypeError("review adoption channels must be ChannelId values")
        if self.desired_channel == self.candidate_channel:
            raise ValueError("review candidate and accepted desired channels must differ")


class ReviewAdoptionEnvironmentResolver(Protocol):
    """Reauthorize the current gate for exact proof-bound review channels."""

    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration: ...


class ReviewAdoptionService(Protocol):
    """Incoming application port for reviewed candidate acceptance."""

    def adopt(self, command: ReviewAdoptionCommand) -> ReviewAdoptionResult: ...

    def recover(self, locator: PublicationRecoveryLocator) -> ReviewAdoptionResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReviewAdoptionCommand:
    """Select one authenticated committed review publication for acceptance."""

    environment_id: EnvironmentId
    review_publication: PublicationRecoveryLocator

    def __post_init__(self) -> None:
        if not isinstance(self.environment_id, EnvironmentId):
            raise TypeError("review adoption environment_id must be an EnvironmentId")
        if not isinstance(self.review_publication, PublicationRecoveryLocator):
            raise TypeError("review adoption requires a PublicationRecoveryLocator")


@dataclass(frozen=True, slots=True)
class ReviewAdoptionResult:
    """Closed outcome for the ownership-aware accepted publication attempt."""

    snapshot_id: SnapshotId | None
    publication: PublicationIntent
    outcome: PublicationOutcome
    recovery_locator: PublicationRecoveryLocator

    def __post_init__(self) -> None:
        if self.snapshot_id is not None and not isinstance(self.snapshot_id, SnapshotId):
            raise TypeError("review adoption snapshot_id must be a SnapshotId or None")
        if not isinstance(self.publication, PublicationIntent):
            raise TypeError("review adoption publication must be a PublicationIntent")
        self.publication._validate()
        if (
            self.publication.mode is not PublicationMode.REVIEW_ADOPTION
            or self.publication.target is not PublicationTarget.ACCEPTED_DESIRED
        ):
            raise ValueError("review adoption result requires an accepted review-adoption intent")
        if not isinstance(self.outcome, PublicationOutcome):
            raise TypeError("review adoption outcome must be a PublicationOutcome")
        self.outcome._validate()
        if not isinstance(self.recovery_locator, PublicationRecoveryLocator):
            raise TypeError("review adoption recovery locator must be a PublicationRecoveryLocator")
        if self.recovery_locator.attempt_id != self.publication.attempt_id:
            raise ValueError("review adoption recovery locator must bind the accepted attempt")
        acceptance = self.publication.review_acceptance
        assert acceptance is not None
        if self.recovery_locator.publication_store_id != acceptance.publication_store_id:
            raise ValueError("review adoption recovery locator belongs to another publication store")
        if self.outcome.state is PublicationOutcomeState.COMMITTED:
            proof = self.outcome.proof
            assert proof is not None
            if proof.intent != self.publication:
                raise ValueError("review adoption proof must bind the accepted intent")
            if self.snapshot_id != self.publication.candidate.snapshot_id:
                raise ValueError("committed review adoption must return the candidate snapshot")
        elif self.snapshot_id is not None:
            raise ValueError("uncommitted review adoption cannot return an accepted snapshot")


@dataclass(frozen=True, slots=True)
class ReviewAdoptionCoordinator:
    """Validate review proof and atomically adopt its exact candidate."""

    authority: ApplyPublicationAuthority
    identity_issuer: ApplyPublicationIdentityIssuer
    environment_resolver: ReviewAdoptionEnvironmentResolver

    def close(self) -> None:
        """The composition root owns the shared publication authority."""

    def adopt(self, command: ReviewAdoptionCommand) -> ReviewAdoptionResult:
        if not isinstance(command, ReviewAdoptionCommand):
            raise TypeError("command must be a ReviewAdoptionCommand")
        command.__post_init__()
        recovered = self.authority.recover_publication(command.review_publication)
        recovered.intent._validate()
        recovered.outcome._validate()
        proof = self._review_proof(recovered.intent, recovered.outcome)
        if recovered.intent.environment_id != command.environment_id:
            raise ReviewAdoptionError("review publication belongs to another environment")
        accepted_base = recovered.intent.review_base_head
        assert accepted_base is not None
        candidate = recovered.intent.candidate
        identity_scope = f"{recovered.intent.environment_id.value}/review-adoption/{proof.proof_id.value}"
        owner = self.identity_issuer.issue_owner(identity_scope, candidate)
        attempt_id = self.identity_issuer.issue_attempt(
            identity_scope, accepted_base.channel_id, accepted_base, candidate
        )
        locator = PublicationRecoveryLocator(command.review_publication.publication_store_id, attempt_id)
        try:
            prior = self.recover(locator)
        except PublicationRecoveryNotFoundError:
            pass
        else:
            prior_acceptance = prior.publication.review_acceptance
            assert prior_acceptance is not None
            if prior_acceptance.review_proof_id != proof.proof_id:
                raise ReviewAdoptionError("existing adoption attempt belongs to another review proof")
            return prior

        configuration = self.environment_resolver.resolve_review_adoption(
            command.environment_id,
            accepted_base.channel_id,
            recovered.intent.channel_id,
        )
        configuration.__post_init__()
        if (
            recovered.intent.channel_id != configuration.candidate_channel
            or accepted_base.channel_id != configuration.desired_channel
        ):
            raise ReviewAdoptionError("review publication belongs to another environment or channel policy")
        review_head = self.authority.prepare_head(recovered.intent.channel_id)
        if review_head != proof.resulting_head:
            raise ReviewAdoptionError("review candidate head no longer matches its authenticated proof")
        acceptance = self.authority.observe_review_acceptance(command.review_publication)
        acceptance._validate()
        if (
            acceptance.publication_store_id != command.review_publication.publication_store_id
            or acceptance.review_proof_id != proof.proof_id
            or acceptance.desired_channel != accepted_base.channel_id
            or acceptance.accepted_base_head != accepted_base
            or acceptance.candidate_snapshot_id != recovered.intent.candidate.snapshot_id
            or acceptance.candidate_content_id != recovered.intent.candidate.content_id
            or acceptance.environment_id != recovered.intent.environment_id
        ):
            raise ReviewAdoptionError("external acceptance does not bind the authenticated review proof")
        ownership = tuple(
            SourceOwnershipChange(change.retained_source, result.resulting_observation, owner)
            for change, result in zip(
                recovered.intent.source_ownership_changes,
                proof.ownership_results,
                strict=True,
            )
        )
        coordination = tuple(
            CoordinationChange(result.key, result.resulting_observation, original.expected.value)
            for original, result in zip(
                recovered.intent.coordination_changes,
                proof.coordination_results,
                strict=True,
            )
        )
        intent = PublicationIntent(
            attempt_id,
            accepted_base.channel_id,
            accepted_base,
            candidate,
            ownership,
            owner,
            coordination,
            PublicationTarget.ACCEPTED_DESIRED,
            PublicationMode.REVIEW_ADOPTION,
            review_acceptance=acceptance,
            environment_id=recovered.intent.environment_id,
        )
        if self.authority.recovery_locator(intent) != locator:
            raise ReviewAdoptionError("publication authority issued an inconsistent adoption recovery locator")
        verified = False
        try:
            outcome = self.authority.execute(intent)
        except PublicationExecutionUnknownError:
            outcome = self.authority.verify(intent)
            verified = True
        outcome._validate()
        if outcome.state is PublicationOutcomeState.UNKNOWN and not verified:
            outcome = self.authority.verify(intent)
            outcome._validate()
        return ReviewAdoptionResult(
            candidate.snapshot_id if outcome.state is PublicationOutcomeState.COMMITTED else None,
            intent,
            outcome,
            locator,
        )

    def recover(self, locator: PublicationRecoveryLocator) -> ReviewAdoptionResult:
        if not isinstance(locator, PublicationRecoveryLocator):
            raise TypeError("locator must be a PublicationRecoveryLocator")
        recovered = self.authority.recover_publication(locator)
        recovered.intent._validate()
        recovered.outcome._validate()
        if (
            recovered.intent.mode is not PublicationMode.REVIEW_ADOPTION
            or recovered.intent.target is not PublicationTarget.ACCEPTED_DESIRED
        ):
            raise ReviewAdoptionError("recovery locator does not identify a review adoption")
        snapshot = (
            recovered.intent.candidate.snapshot_id
            if recovered.outcome.state is PublicationOutcomeState.COMMITTED
            else None
        )
        return ReviewAdoptionResult(snapshot, recovered.intent, recovered.outcome, locator)

    @staticmethod
    def _review_proof(intent: PublicationIntent, outcome: PublicationOutcome) -> PublicationProof:
        if (
            outcome.state is not PublicationOutcomeState.COMMITTED
            or outcome.proof is None
            or outcome.proof.intent != intent
        ):
            raise ReviewAdoptionError("review publication has no authenticated committed proof")
        proof = outcome.proof
        proof._validate()
        if (
            intent.target is not PublicationTarget.REVIEW_CANDIDATE
            or intent.mode is not PublicationMode.REVIEW_REQUIRED
            or intent.review_base_head is None
        ):
            raise ReviewAdoptionError("publication proof does not target a review candidate")
        if proof.resulting_head.snapshot_id != intent.candidate.snapshot_id:
            raise ReviewAdoptionError("review proof does not bind the exact sealed candidate")
        if any(
            change.next_owner != intent.publication_owner
            or result.requested_next_owner != intent.publication_owner
            or result.resulting_observation.owner != intent.publication_owner
            for change, result in zip(
                intent.source_ownership_changes,
                proof.ownership_results,
                strict=True,
            )
        ):
            raise ReviewAdoptionError("review proof did not retain every source under its publication owner")
        return proof
