"""Local-bare Git composition for authenticated review adoption."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitopsctr.adapters.git.apply import _ensure_retention_root, _load_identity_seed, local_bare_publication_authority
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.application.apply_orchestration import HmacApplyPublicationIdentityIssuer
from gitopsctr.application.model import (
    ChannelId,
    EnvironmentId,
    PublicationRecoveryLocator,
    SourceId,
)
from gitopsctr.application.review_adoption import (
    ReviewAdoptionCommand,
    ReviewAdoptionConfiguration,
    ReviewAdoptionCoordinator,
    ReviewAdoptionEnvironmentResolver,
    ReviewAdoptionResult,
)
from gitopsctr.errors import OperationError
from gitopsctr.formats import load_project_config, parse_document_bytes
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resources import ResourceCatalog


@dataclass(frozen=True, slots=True)
class GitReviewAdoptionEnvironmentResolver:
    """Validate one review channel against the repository Environment policy."""

    repository: Path

    def resolve_review_adoption(
        self,
        environment_id: EnvironmentId,
        desired_channel: ChannelId,
        candidate_channel: ChannelId,
    ) -> ReviewAdoptionConfiguration:
        project = load_project_config(self.repository)
        prefix = self.repository.joinpath(*project.environments_path.parts, environment_id.value, "environment")
        matches = tuple(
            path
            for path in (prefix.with_suffix(".yaml"), prefix.with_suffix(".yml"), prefix.with_suffix(".json"))
            if path.is_file()
        )
        if len(matches) != 1:
            raise OperationError("review adoption environment configuration is missing or ambiguous")
        document = parse_document_bytes(matches[0].read_bytes(), matches[0])
        catalog = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
        environment = catalog.normalize_environment(document, environment_id.value)
        if environment.get("changeGate", "none") != "pullRequest":
            raise OperationError("review adoption requires pullRequest change-gate policy")
        return ReviewAdoptionConfiguration(desired_channel, candidate_channel)


@dataclass(slots=True)
class GitReviewAdoptionService:
    """Expose reviewed adoption through the same local-bare authority as apply."""

    repository: Path
    environment_resolver: ReviewAdoptionEnvironmentResolver
    source_id: SourceId = SourceId("default-git-source")
    _coordinator: ReviewAdoptionCoordinator | None = field(default=None, init=False, repr=False)
    _source_repository: GitSourceRepository | None = field(default=None, init=False, repr=False)

    def adopt(self, command: ReviewAdoptionCommand) -> ReviewAdoptionResult:
        return self._application().adopt(command)

    def recover(self, locator: PublicationRecoveryLocator) -> ReviewAdoptionResult:
        return self._application().recover(locator)

    def close(self) -> None:
        if self._source_repository is not None:
            self._source_repository.close()

    def _application(self) -> ReviewAdoptionCoordinator:
        if self._coordinator is not None:
            return self._coordinator
        authority_root = local_bare_publication_authority(self.repository)
        source = GitSourceRepository.from_path(
            self.source_id,
            self.repository,
            _ensure_retention_root(self.repository),
        )
        self._source_repository = source
        authority = GitPublicationStore(authority_root, source)
        seed = _load_identity_seed(authority_root)
        self._coordinator = ReviewAdoptionCoordinator(
            authority,
            HmacApplyPublicationIdentityIssuer("default-git-review-adoption", seed),
            self.environment_resolver,
        )
        return self._coordinator
