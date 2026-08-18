"""The one default composition root for the local source-authored CLI."""

from __future__ import annotations

import base64
from pathlib import Path

from gitopsctr.adapters.filesystem.unit_projection import FilesystemUnitProjectionHost
from gitopsctr.adapters.git import (
    GitApplyService,
    GitAuthoredChangeDecoder,
    GitDependencyInspector,
    GitResourceInspector,
    GitReviewAdoptionEnvironmentResolver,
    GitReviewAdoptionService,
    GitSnapshotReader,
    GitStatusInspector,
)
from gitopsctr.adapters.git.apply import (
    GitApplyEnvironmentResolver,
    GitApplySourceEvidenceProvider,
    UnsupportedGitPublicationAuthority,
    _ensure_retention_root,
    local_bare_publication_authority,
)
from gitopsctr.adapters.git.remote_authority import (
    AuthorityHttpTransport,
    ControlledApplyPublicationIdentityIssuer,
    ControlledGitApplyService,
    ControlledGitPublicationAuthority,
    ControlledGitReviewAdoptionService,
    ControlledGitSourceRepository,
    ControlledGitSourceRetention,
    ControlledRootIncarnationIssuer,
    Ed25519EnvelopeVerifier,
    VerifiedAuthoritySession,
    authenticated_transport_from_git_config,
)
from gitopsctr.adapters.git.remote_inspection import (
    ControlledGitDependencyInspector,
    ControlledGitResourceInspector,
    ControlledGitStatusInspector,
)
from gitopsctr.adapters.git.source_lineage import GitSourceLineageRegistry
from gitopsctr.adapters.git.sources import GitSourceRepository
from gitopsctr.adapters.source_authored import SourceAuthoredSpecificationValidator
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    CatalogUnitProjectionCompiler,
)
from gitopsctr.application.apply_orchestration import ApplyCoordinator
from gitopsctr.application.model import SourceId
from gitopsctr.application.review_adoption import ReviewAdoptionCoordinator
from gitopsctr.application.services import ApplicationServices
from gitopsctr.formats import Project, load_project_config
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, RESOURCE_REGISTRY, UNIT_DRIVERS
from gitopsctr.resources import ResourceCatalog


def create_default_application(
    repository: Path,
    *,
    authority_transport: AuthorityHttpTransport | None = None,
) -> ApplicationServices:
    """Compose the local Git snapshot and source-authored validation adapters."""

    # Preserve a root symlink for the authored-path policy to reject.  Resolving
    # here would erase the security-relevant fact before validation observes it.
    repository_root = repository.absolute()
    project = load_project_config(repository_root)
    if project.publication_authority is not None:
        return _create_controlled_application(
            repository_root,
            project,
            authority_transport or authenticated_transport_from_git_config(repository_root),
        )
    try:
        snapshot_root = local_bare_publication_authority(repository_root)
    except UnsupportedGitPublicationAuthority:
        # Read-only commands remain available for repositories whose remote
        # authority adapter has not yet been implemented. Apply itself fails
        # closed when the lazy GitApplyService is invoked.
        snapshot_root = repository_root
    snapshot_reader = GitSnapshotReader.from_path(snapshot_root)
    return ApplicationServices(
        snapshot_reader,
        SourceAuthoredSpecificationValidator(repository_root),
        GitResourceInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitStatusInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitDependencyInspector(repository_root, snapshot_reader, RESOURCE_REGISTRY),
        GitApplyService(repository_root),
        GitAuthoredChangeDecoder(repository_root),
        GitReviewAdoptionService(
            repository_root,
            GitReviewAdoptionEnvironmentResolver(repository_root),
        ),
    )


def _create_controlled_application(
    repository_root: Path,
    project: Project,
    transport: AuthorityHttpTransport,
) -> ApplicationServices:
    configured = project.publication_authority
    if configured is None:
        raise ValueError("controlled authority composition requires Project publicationAuthority")
    verifier = Ed25519EnvelopeVerifier(
        configured.verification_key.key_id,
        base64.urlsafe_b64decode(configured.verification_key.public_key + "="),
    )
    session = VerifiedAuthoritySession(
        configured.endpoint,
        configured.authority_id,
        verifier,
        transport,
    )
    authority = ControlledGitPublicationAuthority(session)
    source_id = SourceId("default-git-source")
    local_source = GitSourceRepository.from_path(
        source_id,
        repository_root,
        _ensure_retention_root(repository_root),
    )
    source_repository = ControlledGitSourceRepository(
        local_source,
        ControlledGitSourceRetention(session),
    )
    catalog = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
    lineage = GitSourceLineageRegistry({source_id: "."})
    logical = CatalogLogicalUnitProjector(catalog, lineage, FilesystemUnitProjectionHost(catalog))
    publication_identities = ControlledApplyPublicationIdentityIssuer(session)
    apply = ControlledGitApplyService(
        ApplyCoordinator(
            authority,
            authority,
            source_repository,
            GitApplyEnvironmentResolver(repository_root, catalog),
            CatalogApplyDocumentValidator(catalog),
            CatalogUnitProjectionCompiler(catalog, logical),
            CatalogStackProjectionCompiler(catalog, logical, source_encoder=lineage),
            ControlledRootIncarnationIssuer(session),
            publication_identities,
            GitApplySourceEvidenceProvider(
                source_repository,
                source_id,
                repository_root,
                _ensure_retention_root(repository_root),
                lineage,
            ),
        )
    )
    adoption = ControlledGitReviewAdoptionService(
        ReviewAdoptionCoordinator(
            authority,
            publication_identities,
            GitReviewAdoptionEnvironmentResolver(repository_root),
        )
    )
    return ApplicationServices(
        authority,
        SourceAuthoredSpecificationValidator(repository_root),
        ControlledGitResourceInspector(repository_root, authority, RESOURCE_REGISTRY),
        ControlledGitStatusInspector(repository_root, authority, RESOURCE_REGISTRY),
        ControlledGitDependencyInspector(repository_root, RESOURCE_REGISTRY),
        apply,
        GitAuthoredChangeDecoder(repository_root, source_id, source_repository),
        adoption,
    )
