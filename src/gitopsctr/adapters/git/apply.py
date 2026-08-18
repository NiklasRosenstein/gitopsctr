"""Git incoming/configuration adapters for application-owned apply orchestration."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote, urlsplit

import yaml
from dulwich.repo import Repo

from gitopsctr.adapters.filesystem.unit_projection import FilesystemUnitProjectionHost
from gitopsctr.adapters.filesystem.workspace import FilesystemWorkspaceAdapter
from gitopsctr.adapters.git.publication import GitPublicationStore
from gitopsctr.adapters.git.snapshots import GitSnapshotEntryError, GitSnapshotReader
from gitopsctr.adapters.git.source_lineage import GitSourceLineageRegistry
from gitopsctr.adapters.git.sources import GitSourceRepository, GitSourceRetentionStore
from gitopsctr.application.apply import (
    ApplyCommand,
    ApplyResult,
    AuthoredChangeSet,
    _issue_authored_document,
    _issue_authored_source_acquisition,
)
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    CatalogUnitProjectionCompiler,
)
from gitopsctr.application.apply_orchestration import (
    ApplyCoordinationRequest,
    ApplyCoordinator,
    ApplyEnvironmentConfiguration,
    ApplySourceEvidence,
    CandidatePublicationCoordinator,
    CandidatePublicationRequest,
    HmacApplyPublicationIdentityIssuer,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionPolicy,
    ExactPlane,
    HmacRootIncarnationIssuer,
    RetainedSourceDescriptor,
    RetainedSourcePlane,
    SourceBindingRole,
    WorkspaceProjectionContext,
    _issue_retained_source_descriptor,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    HeadObservation,
    PublicationOutcomeState,
    RetainedSource,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.sources import (
    SourceError,
    SourceNotFoundError,
    SourceRepository,
    SourceRequest,
    SourceSnapshot,
)
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace, WorkspaceEntryKind
from gitopsctr.errors import OperationError
from gitopsctr.formats import PROJECT_CONFIG_NAMES, parse_document_bytes, validate_project_document
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import require_json_value
from gitopsctr.resources import ResourceCatalog
from gitopsctr.state import canonical_publication_ref

_DEFAULT_GIT_SOURCE_ID = SourceId("default-git-source")
_RETENTION_SUFFIX = ".gitopsctr-source-retention"


_IDENTITY_KEY_SUFFIX = ".gitopsctr-apply-key"
_EXACT_GIT_REVISION = re.compile(r"[0-9a-f]{40}$")


class UnsupportedGitPublicationAuthority(OperationError):
    """The configured origin cannot host the current durable publication store."""


class GitApplyCompatibilityWarning(RuntimeWarning):
    """Authenticated publication succeeded but a legacy read-side mirror did not."""


class GitApplyCleanupWarning(RuntimeWarning):
    """A definite nonpublication completed but retained-source cleanup degraded."""


@dataclass(slots=True)
class GitAuthoredChangeDecoder:
    """Decode local or exact retained source input without controller delegation."""

    repository: Path
    source_id: SourceId = _DEFAULT_GIT_SOURCE_ID
    source_repository: SourceRepository | None = None

    def close(self) -> None:
        if self.source_repository is not None:
            self.source_repository.close()

    def decode(self, command: ApplyCommand) -> AuthoredChangeSet:
        if command.source_request is None:
            documents = tuple(_load_live_documents(self.repository, command.input_labels))
            if not documents and command.partition is None:
                raise OperationError("apply produced zero documents")
            return AuthoredChangeSet(documents)
        _source_selector(command.source_request, self.source_id)
        source_repository = self._source()
        try:
            source = source_repository.resolve(command.source_request)
        except SourceError as exc:
            if isinstance(exc.__cause__, GitSnapshotEntryError):
                raise OperationError("apply input contains an invalid or looping symbolic link") from exc
            raise
        retained = source_repository.retain(source)
        try:
            documents = tuple(_load_workspace_documents(self.repository, source.workspace, command.input_labels))
            acquisition = _issue_authored_source_acquisition(source, retained)
            return AuthoredChangeSet(documents, source.source_snapshot_id, acquisition)
        except BaseException:
            source_repository.release(retained)
            raise

    def _source(self) -> SourceRepository:
        if self.source_repository is None:
            self.source_repository = GitSourceRepository.from_path(
                self.source_id, self.repository, _ensure_retention_root(self.repository)
            )
        return self.source_repository


@dataclass(frozen=True, slots=True)
class GitApplyEnvironmentResolver:
    """Resolve Project/Environment policy from exact source workspace bytes."""

    repository: Path
    catalog: ResourceCatalog

    def close(self) -> None:
        """No owned resources."""

    def resolve(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyEnvironmentConfiguration:
        _reject_direct_source_revisions(changes)
        if changes.source_acquisition is None:
            _reject_unretained_repository_sources(changes)
        workspace = (
            changes.source_acquisition.snapshot.workspace
            if changes.source_acquisition is not None
            else FilesystemWorkspaceAdapter().read(self.repository, excluded_top_level=frozenset((".git",)))
        )
        project_key, project_bytes = _one_workspace_document(workspace, tuple(PROJECT_CONFIG_NAMES), "Project")
        project = validate_project_document(
            parse_document_bytes(project_bytes, PurePosixPath(project_key)), PurePosixPath(project_key)
        )
        environment_prefix = f"{project.environments_path.as_posix()}/{command.environment_id.value}/environment"
        environment_key, environment_bytes = _one_workspace_document(
            workspace,
            tuple(f"{environment_prefix}{suffix}" for suffix in (".yaml", ".yml", ".json")),
            "Environment",
        )
        environment_document = parse_document_bytes(environment_bytes, PurePosixPath(environment_key))
        environment = self.catalog.normalize_environment(environment_document, command.environment_id.value)
        refs = environment.get("refs")
        refs = refs if isinstance(refs, dict) else {}
        configured_desired = ChannelId(
            canonical_publication_ref(
                cast(str, refs.get("desired") or project.environment_defaults.refs.desired).replace(
                    "{environment}", command.environment_id.value
                )
            )
        )
        desired = ChannelId(
            canonical_publication_ref(command.desired_channel.value)
            if command.desired_channel is not None
            else configured_desired.value
        )
        observed = ChannelId(
            canonical_publication_ref(command.observed_channel.value)
            if command.observed_channel is not None
            else canonical_publication_ref(
                cast(str, refs.get("observed") or project.environment_defaults.refs.observed).replace(
                    "{environment}", command.environment_id.value
                )
            )
        )
        if desired == observed:
            raise OperationError("desired and observed channels must differ")
        review = environment.get("changeGate", "none") == "pullRequest"
        operation_id = _operation_id(command, changes)
        template = cast(str, refs.get("candidate") or project.environment_defaults.refs.candidate)
        candidate = ChannelId(
            canonical_publication_ref(command.candidate_channel.value)
            if command.candidate_channel is not None
            else canonical_publication_ref(
                template.replace("{environment}", command.environment_id.value)
                .replace("{operation}", "apply")
                .replace("{id}", operation_id)
            )
        )
        if candidate in {desired, observed}:
            raise OperationError("candidate channel conflicts with deployment state")
        if command.candidate_channel is not None and not review:
            raise OperationError("an explicit candidate channel requires review policy")

        primary = None
        named = ()
        if changes.source_acquisition is not None:
            selector = command.source_request.selector if command.source_request is not None else "exact-source"
            selector_evidence = ContentId(f"sha256:{hashlib.sha256(selector.encode()).hexdigest()}")
            workspace_key = _first_source_workspace_key(self.repository, command.input_labels, workspace)
            primary = _issue_retained_source_descriptor(
                changes.source_acquisition.retained,
                "authored",
                SourceBindingRole.PRIMARY_AUTHORED,
                workspace_key,
                selector_evidence,
            )
            descriptors = [
                _issue_retained_source_descriptor(
                    changes.source_acquisition.retained,
                    binding,
                    SourceBindingRole.WORKLOAD,
                    path,
                    selector_evidence,
                )
                for binding, path in _workload_bindings(changes)
            ]
            named = tuple(descriptors)
        coordination = (
            (
                ApplyCoordinationRequest(
                    f"review-request/{candidate.value}",
                    f"apply:{operation_id}",
                ),
            )
            if review
            else ()
        )
        return ApplyEnvironmentConfiguration(
            desired,
            observed,
            candidate,
            ApplyProjectionPolicy(review_required=review),
            WorkspaceProjectionContext(project_bytes, environment_bytes),
            primary,
            named,
            coordination,
        )


@dataclass(frozen=True, slots=True)
class GitApplySourceEvidenceProvider:
    """Retain every explicitly selected current or historical workload source."""

    source_repository: SourceRepository
    source_id: SourceId
    repository: Path | None = None
    retention_root: Path | None = None
    lineage_registry: GitSourceLineageRegistry | None = None

    def prepare(
        self,
        command: ApplyCommand,
        changes: AuthoredChangeSet,
        configuration: ApplyEnvironmentConfiguration,
        desired: ExactPlane,
        observed: ExactPlane,
    ) -> ApplySourceEvidence:
        del command, observed
        acquisition = changes.source_acquisition
        grouped: dict[object, tuple[SourceSnapshot, RetainedSource, list[RetainedSourceDescriptor]]] = {}
        if acquisition is not None:
            acquisition._validate()
            grouped[acquisition.retained.handle] = (acquisition.snapshot, acquisition.retained, [])
        primary = configuration.primary_source
        if primary is not None and acquisition is not None:
            grouped[acquisition.retained.handle][2].append(primary)
        if acquisition is not None:
            for descriptor in configuration.named_sources:
                if descriptor.role is not SourceBindingRole.WORKLOAD:
                    grouped[acquisition.retained.handle][2].append(descriptor)

        current_revision = (
            acquisition.snapshot.source_snapshot_id.snapshot_id.value.removeprefix("git-source:")
            if acquisition is not None
            else None
        )
        retained_by_revision: dict[str, tuple[SourceSnapshot, RetainedSource]] = {}
        additional: list[RetainedSource] = []
        requested = set(_workload_source_bindings(changes))
        requested.update(_fanout_workload_source_bindings(changes, desired.workspace))
        external_selected: dict[tuple[SourceId, str], tuple[SourceSnapshot, RetainedSource]] = {}
        try:
            for external_binding in _external_source_bindings(changes, desired.workspace):
                if self.repository is None or self.retention_root is None or self.lineage_registry is None:
                    raise OperationError("external Git source acquisition is not configured")
                canonical_repository = _canonical_git_repository(external_binding.repository, self.repository)
                external_source_id = _source_id_for_repository(canonical_repository)
                self.lineage_registry.register(external_source_id, canonical_repository)
                selected = external_selected.get((external_source_id, external_binding.revision))
                if selected is None and _EXACT_GIT_REVISION.fullmatch(external_binding.revision) is not None:
                    retained_snapshot = GitSourceRetentionStore(self.retention_root).retained_snapshot(
                        SourceSnapshotId(external_source_id, SnapshotId(f"git-source:{external_binding.revision}"))
                    )
                    if retained_snapshot is not None:
                        retained, snapshot = retained_snapshot
                        selected = snapshot, retained
                if selected is None:
                    with tempfile.TemporaryDirectory(prefix="gitopsctr-external-source-") as temporary:
                        checkout = Path(temporary) / "repository.git"
                        completed = subprocess.run(
                            ("git", "clone", "--mirror", canonical_repository, str(checkout)),
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        if completed.returncode != 0:
                            raise OperationError(f"external Git source {canonical_repository!r} is unavailable")
                        external = GitSourceRepository.from_path(external_source_id, checkout, self.retention_root)
                        try:
                            snapshot = external.resolve(SourceRequest(external_source_id, external_binding.revision))
                            retained = external.retain(snapshot)
                        finally:
                            external.close()
                    additional.append(retained)
                    selected = snapshot, retained
                snapshot, retained = selected
                external_selected[(external_source_id, external_binding.revision)] = selected
                selector_evidence = ContentId(
                    f"sha256:{hashlib.sha256(external_binding.revision.encode()).hexdigest()}"
                )
                descriptors = [
                    _issue_retained_source_descriptor(
                        retained,
                        external_binding.template_name,
                        SourceBindingRole.STACK_TEMPLATE,
                        external_binding.template_path,
                        selector_evidence,
                    )
                ]
                descriptors.extend(
                    _issue_retained_source_descriptor(
                        retained,
                        binding,
                        SourceBindingRole.WORKLOAD,
                        path,
                        selector_evidence,
                    )
                    for binding, path in _external_workload_bindings(
                        external_binding, snapshot.workspace, changes, desired.workspace
                    )
                )
                record = grouped.setdefault(retained.handle, (snapshot, retained, []))
                record[2].extend(descriptors)

            for binding, path, requested_revision in sorted(requested):
                revision = requested_revision or current_revision
                if revision is None:
                    raise OperationError(
                        f"repository-backed Stack Unit {binding!r} requires an exact retained source revision"
                    )
                selected = retained_by_revision.get(revision)
                if selected is None:
                    if revision == current_revision and acquisition is not None:
                        selected = (acquisition.snapshot, acquisition.retained)
                    else:
                        try:
                            snapshot = self.source_repository.resolve(SourceRequest(self.source_id, revision))
                        except SourceNotFoundError as exc:
                            raise OperationError(f"source revision {revision!r} is unavailable in repository") from exc
                        retained = self.source_repository.retain(snapshot)
                        additional.append(retained)
                        selected = (snapshot, retained)
                    retained_by_revision[revision] = selected
                snapshot, retained = selected
                selector_evidence = ContentId(f"sha256:{hashlib.sha256(revision.encode()).hexdigest()}")
                descriptor = _issue_retained_source_descriptor(
                    retained,
                    binding,
                    SourceBindingRole.WORKLOAD,
                    path,
                    selector_evidence,
                )
                record = grouped.setdefault(retained.handle, (snapshot, retained, []))
                record[2].append(descriptor)

            planes = tuple(
                _retained_source_plane(snapshot, retained, tuple(descriptors))
                for snapshot, retained, descriptors in grouped.values()
            )
            named = tuple(
                descriptor for plane in planes for descriptor in plane.descriptors if descriptor is not primary
            )
            return ApplySourceEvidence(planes, primary, named, tuple(additional))
        except BaseException as primary_error:
            for retained in additional:
                try:
                    self.source_repository.release(retained)
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        f"also failed to release historical source handle {retained.handle.value!r}: {cleanup_error}"
                    )
            raise


@dataclass(slots=True)
class GitApplyService:
    """Git composition adapter delegating all coordination to the application."""

    repository: Path
    source_id: SourceId = _DEFAULT_GIT_SOURCE_ID
    _coordinator: ApplyCoordinator | None = field(default=None, init=False, repr=False)

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        result = self._application().apply(command, changes)
        if result.snapshot_id is not None:
            failures: list[str] = []
            try:
                _cache_published_snapshot(self.repository, result.snapshot_id.value.removeprefix("git-commit:"))
            except Exception as exc:
                failures.append(_nonthrowing_failure_description("snapshot cache", exc))
            try:
                _mirror_legacy_controller_pins(
                    self.repository,
                    local_bare_publication_authority(self.repository),
                    command.environment_id.value,
                    self._application().snapshot_reader.open_snapshot(result.snapshot_id).workspace,
                )
            except Exception as exc:
                failures.append(_nonthrowing_failure_description("controller pin mirror", exc))
            if failures:
                _report_nonthrowing_warning(
                    "authenticated apply publication is committed, but retryable compatibility work failed ("
                    + "; ".join(failures)
                    + ")",
                    GitApplyCompatibilityWarning,
                )
        return result

    def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.close()

    def recover(self, locator):  # type: ignore[no-untyped-def]
        return self._application().recover(locator)

    def _application(self) -> ApplyCoordinator:
        if self._coordinator is not None:
            return self._coordinator
        authority_root = local_bare_publication_authority(self.repository)
        source_repository = GitSourceRepository.from_path(
            self.source_id, self.repository, _ensure_retention_root(self.repository)
        )
        authority = GitPublicationStore(authority_root, source_repository)
        reader = GitSnapshotReader.from_path(authority_root)
        catalog = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
        seed = _load_identity_seed(authority_root)
        lineage = GitSourceLineageRegistry({self.source_id: "."})
        retention_root = _ensure_retention_root(self.repository)
        logical = CatalogLogicalUnitProjector(
            catalog,
            lineage,
            FilesystemUnitProjectionHost(catalog),
        )
        self._coordinator = ApplyCoordinator(
            reader,
            authority,
            source_repository,
            GitApplyEnvironmentResolver(self.repository, catalog),
            CatalogApplyDocumentValidator(catalog),
            CatalogUnitProjectionCompiler(catalog, logical),
            CatalogStackProjectionCompiler(catalog, logical, source_encoder=lineage),
            HmacRootIncarnationIssuer("default-git-apply", seed),
            HmacApplyPublicationIdentityIssuer("default-git-apply", seed),
            GitApplySourceEvidenceProvider(
                source_repository,
                self.source_id,
                self.repository,
                retention_root,
                lineage,
            ),
        )
        return self._coordinator


def source_request_for_git(value: str | None) -> SourceRequest | None:
    """Translate a default-Git selector into a backend-neutral source request."""

    return SourceRequest(_DEFAULT_GIT_SOURCE_ID, value) if value is not None else None


def local_bare_publication_authority(repository: Path) -> Path:
    """Resolve the one supported shared authority, rejecting remote-only origins."""

    completed = subprocess.run(
        ("git", "-C", str(repository), "remote", "get-url", "origin"),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise UnsupportedGitPublicationAuthority(
            "apply publication currently requires origin to be a local bare Git repository"
        )
    value = completed.stdout.strip()
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise UnsupportedGitPublicationAuthority(
                "apply publication to non-local origins awaits the durable remote authority adapter"
            )
        path = Path(unquote(parsed.path))
    else:
        if parsed.netloc or value.startswith(("git@", "ssh:")) or ":" in value.split("/", 1)[0]:
            raise UnsupportedGitPublicationAuthority(
                "apply publication to non-local origins awaits the durable remote authority adapter"
            )
        path = Path(value)
        if not path.is_absolute():
            path = repository / path
    try:
        root = path.resolve(strict=True)
        opened = Repo(root)
        try:
            if not opened.bare:
                raise ValueError
        finally:
            opened.close()
    except (OSError, ValueError) as exc:
        raise UnsupportedGitPublicationAuthority(
            "origin must resolve to an existing local bare Git repository"
        ) from exc
    return root


def publish_durable_candidate(
    repository: Path,
    environment: str,
    desired_channel: ChannelId,
    expected_revision: str,
    candidate: Path,
    *,
    candidate_channel: ChannelId | None = None,
) -> ApplyResult:
    """Publish durable projection output through the authenticated authority."""

    authority_root = local_bare_publication_authority(repository)
    source_repository = GitSourceRepository.from_path(
        _DEFAULT_GIT_SOURCE_ID,
        repository,
        _ensure_retention_root(repository),
    )
    authority = GitPublicationStore(authority_root, source_repository)
    fresh_retained: tuple[RetainedSource, ...] = ()
    published_or_ambiguous = False
    primary_error: BaseException | None = None
    try:
        expected_head = authority.prepare_head(desired_channel)
        if expected_head.snapshot_id != SnapshotId(f"git-commit:{expected_revision}"):
            raise OperationError("durable projection expected desired head is stale")
        filesystem_workspace = FilesystemWorkspaceAdapter().read(candidate)
        entries = filesystem_workspace.list_entries()
        if any(entry.kind is WorkspaceEntryKind.SYMLINK for entry in entries):
            raise OperationError("durable candidate cannot contain symbolic links")
        workspace = InMemoryWorkspace(
            tuple(entry for entry in entries if entry.kind is WorkspaceEntryKind.FILE),
            mutable=False,
        )
        source_evidence = _durable_candidate_retained_sources(
            workspace,
            repository,
            _ensure_retention_root(repository),
            source_repository,
        )
        fresh_retained = source_evidence.release_on_nonpublication
        review = candidate_channel is not None
        coordination = (
            (
                ApplyCoordinationRequest(
                    f"review-request/{candidate_channel.value}",
                    f"durable:{workspace.content_id.value}",
                ),
            )
            if candidate_channel is not None
            else ()
        )
        publisher = CandidatePublicationCoordinator(
            authority,
            HmacApplyPublicationIdentityIssuer(
                "default-git-durable-projection",
                _load_identity_seed(authority_root),
            ),
        )
        result = publisher.publish(
            CandidatePublicationRequest(
                environment,
                desired_channel,
                expected_head,
                workspace,
                review,
                candidate_channel,
                coordination,
                source_evidence.retained_sources,
            )
        )
        published_or_ambiguous = result.publication_outcome is not None and result.publication_outcome.state in {
            PublicationOutcomeState.COMMITTED,
            PublicationOutcomeState.UNKNOWN,
        }
        return result
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_failures = (
            ()
            if published_or_ambiguous
            else _release_retained_sources(
                source_repository,
                fresh_retained,
                primary_error=primary_error,
            )
        )
        close_failures: list[str] = []
        try:
            authority.close()
        except BaseException as close_error:
            close_failures.append(_nonthrowing_failure_description("publication authority close", close_error))
        try:
            source_repository.close()
        except BaseException as close_error:
            close_failures.append(_nonthrowing_failure_description("source repository close", close_error))
        if primary_error is not None:
            for failure in close_failures:
                try:
                    primary_error.add_note(f"also failed during cleanup: {failure}")
                except BaseException:
                    pass
        elif cleanup_failures or close_failures:
            _report_nonthrowing_warning(
                "durable publication follow-up cleanup remains retryable ("
                + "; ".join((*cleanup_failures, *close_failures))
                + ")",
                GitApplyCleanupWarning,
            )


def _source_selector(request: SourceRequest | None, expected_source_id: SourceId) -> str | None:
    if request is None:
        return None
    if request.source_id != expected_source_id:
        raise ValueError(f"Git apply is not configured for source {request.source_id!s}")
    return request.selector


def _retention_root(repository: Path) -> Path:
    root = repository.absolute()
    return root.parent / f".{root.name}{_RETENTION_SUFFIX}"


def _ensure_retention_root(repository: Path) -> Path:
    """Create the private target-side retention anchor without following a symlink."""

    root = _retention_root(repository)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise OperationError("Git apply retention root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OperationError("Git apply retention root must be a private directory")
    try:
        os.chmod(root, 0o700)
    except OSError as exc:
        raise OperationError("Git apply retention root cannot be secured") from exc
    return root


def _cache_published_snapshot(repository: Path, revision: str) -> None:
    """Fetch a committed authority object for legacy local materialization only."""

    completed = subprocess.run(
        ("git", "-C", str(repository), "fetch", "--no-tags", "origin", revision),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OperationError("committed apply snapshot cannot be cached from its local authority")


def _nonthrowing_failure_description(label: str, error: BaseException) -> str:
    try:
        detail = str(error)
    except BaseException:
        detail = "unreportable failure"
    return f"{label}: {detail}"


def _report_nonthrowing_warning(message: str, category: type[Warning]) -> None:
    """Report degraded follow-up work without crossing a committed boundary."""

    try:
        warnings.warn(message, category, stacklevel=3)
    except BaseException:
        # Warning filters and showwarning hooks are caller-owned and may raise.
        # They cannot turn a proven committed publication into an exception.
        pass


def _mirror_legacy_controller_pins(
    repository: Path,
    authority: Path,
    environment: str,
    workspace: ImmutableWorkspace,
) -> None:
    """Maintain legacy read-side pin refs; authenticated ownership remains authoritative."""

    templates: dict[str, tuple[str, str | None, str]] = {}
    stacks: dict[str, tuple[str, str, str]] = {}
    unit_revisions: dict[str, set[str]] = {}
    for entry in workspace.list_entries():
        if entry.kind is not WorkspaceEntryKind.FILE or not entry.key.endswith((".json", ".yaml", ".yml")):
            continue
        if not entry.key.startswith(("stack-templates/", "stacks/", "units/")):
            continue
        document = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        uid = metadata.get("uid") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not isinstance(uid, str) or not isinstance(specification, dict):
            continue
        if document.get("kind") == "StackTemplate":
            source = specification.get("sourceContext")
            revision = source.get("revision") if isinstance(source, dict) else None
            source_repository = source.get("repository") if isinstance(source, dict) else None
            templates[name] = (
                uid,
                revision if isinstance(revision, str) else None,
                source_repository if isinstance(source_repository, str) else ".",
            )
        elif document.get("kind") == "Stack":
            template = specification.get("templateRef")
            if (
                isinstance(template, dict)
                and isinstance(template.get("name"), str)
                and isinstance(template.get("uid"), str)
            ):
                stacks[name] = (uid, cast(str, template["name"]), cast(str, template["uid"]))
        elif entry.key.startswith("units/"):
            source = specification.get("source")
            revision = source.get("revision") if isinstance(source, dict) else None
            parts = entry.key.split("/")
            if len(parts) >= 3 and isinstance(revision, str):
                unit_revisions.setdefault(parts[1], set()).add(revision)

    pins: dict[str, tuple[str, str]] = {}
    for template_name, (template_uid, revision, source_repository) in templates.items():
        if revision is not None:
            pins[f"stack-templates/{environment}/{template_name}/{template_uid}/{revision}"] = (
                revision,
                source_repository,
            )
    for stack_name, revisions in unit_revisions.items():
        stack = stacks.get(stack_name)
        if stack is None:
            continue
        stack_uid, template_name, template_uid = stack
        template = templates.get(template_name)
        source_repository = template[2] if template is not None else "."
        for revision in revisions:
            pins[
                f"stack-templates/{environment}/{template_name}/{template_uid}/"
                f"stacks/{stack_name}/{stack_uid}/{revision}"
            ] = (revision, source_repository)
    if not pins:
        return
    for revision, source_repository in sorted(set(pins.values())):
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise OperationError("desired source revision cannot be mirrored to a legacy controller pin")
        fetch_repository = str(repository) if source_repository == "." else source_repository
        fetched = subprocess.run(
            ("git", "--git-dir", str(authority), "fetch", "--no-tags", fetch_repository, revision),
            check=False,
            capture_output=True,
            text=True,
        )
        if fetched.returncode != 0:
            raise OperationError("desired source revision cannot be retained in the local publication authority")
    commands = "".join(
        f"update refs/heads/gitopsctr/pins/{name} {revision}\n"
        for name, (revision, _source_repository) in sorted(pins.items())
    )
    updated = subprocess.run(
        ("git", "--git-dir", str(authority), "update-ref", "--stdin"),
        input=commands,
        check=False,
        capture_output=True,
        text=True,
    )
    if updated.returncode != 0:
        raise OperationError("legacy controller pin mirrors cannot be updated")


def _load_identity_seed(authority: Path) -> str:
    path = authority.parent / f".{authority.name}{_IDENTITY_KEY_SUFFIX}"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            os.write(descriptor, secrets.token_hex(32).encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    try:
        metadata = path.lstat()
        value = path.read_text()
    except OSError as exc:
        raise OperationError("Git apply identity key is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise OperationError("Git apply identity key must be a private regular file")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise OperationError("Git apply identity key is invalid")
    return value


def _operation_id(command: ApplyCommand, changes: AuthoredChangeSet) -> str:
    payload = {
        "environment": command.environment_id.value,
        "partition": command.partition,
        "source": changes.source_snapshot_id.to_wire() if changes.source_snapshot_id is not None else None,
        "documents": [(item.origin, item.content_id.value) for item in changes.documents],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class _ExternalTemplateBinding:
    """One exact external StackTemplate selector selected for this operation."""

    template_name: str
    repository: str
    revision: str
    template_path: str


def _external_source_bindings(
    changes: AuthoredChangeSet, workspace: ImmutableWorkspace
) -> tuple[_ExternalTemplateBinding, ...]:
    """Collect authored selectors and persisted external template context."""

    selected: dict[str, _ExternalTemplateBinding] = {}
    for entry in workspace.list_entries("stack-templates"):
        if entry.kind is not WorkspaceEntryKind.FILE:
            continue
        document = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        context = specification.get("sourceContext") if isinstance(specification, dict) else None
        acquisition = specification.get("acquisition") if isinstance(specification, dict) else None
        requested = acquisition.get("requestedSource") if isinstance(acquisition, dict) else None
        from_git = requested.get("fromGit") if isinstance(requested, dict) else None
        repository = context.get("repository") if isinstance(context, dict) else None
        revision = context.get("revision") if isinstance(context, dict) else None
        path = from_git.get("path") if isinstance(from_git, dict) else None
        if (
            isinstance(name, str)
            and isinstance(repository, str)
            and repository != "."
            and isinstance(revision, str)
            and isinstance(path, str)
        ):
            selected[name] = _ExternalTemplateBinding(name, repository, revision, path)
    for authored in changes.documents:
        document = authored.document
        if document.get("kind") != "StackTemplate":
            continue
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        source = specification.get("source") if isinstance(specification, dict) else None
        from_git = source.get("fromGit") if isinstance(source, dict) else None
        repository = from_git.get("repository") if isinstance(from_git, dict) else None
        revision = from_git.get("revision") if isinstance(from_git, dict) else None
        path = from_git.get("path") if isinstance(from_git, dict) else None
        if all(isinstance(value, str) for value in (name, repository, revision, path)):
            selected[cast(str, name)] = _ExternalTemplateBinding(
                cast(str, name), cast(str, repository), cast(str, revision), cast(str, path)
            )
    return tuple(selected[name] for name in sorted(selected))


def _external_workload_bindings(
    external: _ExternalTemplateBinding,
    source_workspace: ImmutableWorkspace,
    changes: AuthoredChangeSet,
    desired_workspace: ImmutableWorkspace,
) -> tuple[tuple[str, str], ...]:
    """Bind selected Stack children to paths in an acquired external template."""

    try:
        raw = source_workspace.read(external.template_path)
    except Exception as exc:
        raise OperationError(
            f"external StackTemplate path {external.template_path!r} is unavailable in its exact source"
        ) from exc
    document = parse_document_bytes(raw, PurePosixPath(external.template_path))
    specification = document.get("spec")
    units = specification.get("unitTemplates") if isinstance(specification, dict) else None
    if not isinstance(units, dict):
        # The compiler owns the authoritative resource error for recursive or
        # otherwise non-inline template documents.
        return ()

    stacks: dict[str, Mapping[str, object]] = {}
    for entry in desired_workspace.list_entries("stacks"):
        if entry.kind is not WorkspaceEntryKind.FILE:
            continue
        stack = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        metadata = stack.get("metadata")
        stack_specification = stack.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(name, str) and isinstance(stack_specification, dict):
            stacks[name] = stack_specification
    for authored in changes.documents:
        document = authored.document
        if document.get("kind") != "Stack":
            continue
        metadata = document.get("metadata")
        stack_specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(name, str) and isinstance(stack_specification, dict):
            stacks[name] = stack_specification

    bindings: set[tuple[str, str]] = set()
    for stack_name, stack in stacks.items():
        reference = stack.get("templateRef")
        template_name = reference.get("name") if isinstance(reference, dict) else stack.get("template")
        if template_name != external.template_name:
            continue
        selected_units = stack.get("units")
        names = (
            tuple(cast(list[str], selected_units))
            if isinstance(selected_units, list) and all(isinstance(item, str) for item in selected_units)
            else tuple(units)
        )
        parameters = stack.get("parameters")
        for unit_name in names:
            template = units.get(unit_name)
            unit_specification = template.get("spec") if isinstance(template, dict) else None
            source = unit_specification.get("source") if isinstance(unit_specification, dict) else None
            if isinstance(source, dict) and isinstance(source.get("fromParameter"), dict):
                parameter_name = cast(dict[str, object], source["fromParameter"]).get("name")
                parameter_value = parameters.get(parameter_name) if isinstance(parameters, dict) else None
                source = parameter_value if isinstance(parameter_value, dict) else None
            path = source.get("path") if isinstance(source, dict) else None
            if isinstance(path, str):
                bindings.add((f"{stack_name}/{unit_name}", path))
    return tuple(sorted(bindings))


def _canonical_git_repository(value: str, repository: Path) -> str:
    """Canonicalize local Git repository selectors into durable file URIs."""

    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            return Path(unquote(parsed.path)).resolve().as_uri()
        return value
    path = Path(value)
    if not path.is_absolute():
        path = repository / path
    return path.resolve().as_uri()


def _source_id_for_repository(repository: str) -> SourceId:
    if repository == ".":
        return _DEFAULT_GIT_SOURCE_ID
    return SourceId("external-git-" + hashlib.sha256(repository.encode()).hexdigest()[:32])


@dataclass(frozen=True, slots=True)
class _DurableSourceEvidence:
    retained_sources: tuple[RetainedSource, ...]
    release_on_nonpublication: tuple[RetainedSource, ...]


def _durable_candidate_retained_sources(
    workspace: ImmutableWorkspace,
    repository: Path,
    retention_root: Path,
    source_repository: GitSourceRepository,
) -> _DurableSourceEvidence:
    """Recover exact retained source capabilities referenced by a desired tree."""

    templates: dict[str, tuple[str, str]] = {}
    stacks: dict[str, str] = {}
    units: list[tuple[str, Mapping[str, object]]] = []
    for entry in workspace.list_entries():
        if entry.kind is not WorkspaceEntryKind.FILE or not entry.key.endswith((".json", ".yaml", ".yml")):
            continue
        if not entry.key.startswith(("stack-templates/", "stacks/", "units/")):
            continue
        try:
            document = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        except Exception as exc:
            raise OperationError(f"durable candidate resource {entry.key!r} is invalid") from exc
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not isinstance(specification, dict):
            continue
        kind = document.get("kind")
        if kind == "StackTemplate":
            source_context = specification.get("sourceContext")
            source_repository_value = source_context.get("repository") if isinstance(source_context, dict) else None
            revision = source_context.get("revision") if isinstance(source_context, dict) else None
            if isinstance(source_repository_value, str) and isinstance(revision, str):
                templates[name] = (source_repository_value, revision)
        elif kind == "Stack":
            template_ref = specification.get("templateRef")
            template_name = template_ref.get("name") if isinstance(template_ref, dict) else None
            if isinstance(template_name, str):
                stacks[name] = template_name
        elif entry.key.startswith("units/"):
            units.append((entry.key, specification))

    required: dict[SourceSnapshotId, str] = {}
    for source_repository_context, revision in templates.values():
        if _EXACT_GIT_REVISION.fullmatch(revision) is None:
            raise OperationError("durable candidate StackTemplate source revision is not exact")
        required[
            SourceSnapshotId(
                _source_id_for_repository(source_repository_context),
                SnapshotId(f"git-source:{revision}"),
            )
        ] = source_repository_context
    for key, specification in units:
        source = specification.get("source")
        revision = source.get("revision") if isinstance(source, dict) else None
        if revision is None:
            continue
        if not isinstance(revision, str) or _EXACT_GIT_REVISION.fullmatch(revision) is None:
            raise OperationError(f"durable candidate Unit {key!r} source revision is not exact")
        parts = key.split("/")
        source_repository_context = "."
        if len(parts) >= 3:
            template_name = stacks.get(parts[1])
            template_source = templates.get(template_name) if template_name is not None else None
            if template_source is not None:
                source_repository_context = template_source[0]
        required[
            SourceSnapshotId(
                _source_id_for_repository(source_repository_context),
                SnapshotId(f"git-source:{revision}"),
            )
        ] = source_repository_context

    store = GitSourceRetentionStore(retention_root)
    retained: list[RetainedSource] = []
    fresh: list[RetainedSource] = []
    try:
        for source_snapshot_id in sorted(required, key=lambda item: item.to_wire()):
            selected = store.retained_snapshot(source_snapshot_id)
            if selected is None:
                revision = source_snapshot_id.snapshot_id.value.removeprefix("git-source:")
                source_context = required[source_snapshot_id]
                try:
                    if source_context == ".":
                        snapshot = source_repository.resolve(SourceRequest(_DEFAULT_GIT_SOURCE_ID, revision))
                        capability = source_repository.retain(snapshot)
                    else:
                        canonical_repository = _canonical_git_repository(source_context, repository)
                        if _source_id_for_repository(canonical_repository) != source_snapshot_id.source_id:
                            raise OperationError("durable candidate source repository identity is inconsistent")
                        with tempfile.TemporaryDirectory(prefix="gitopsctr-durable-source-") as temporary:
                            checkout = Path(temporary) / "repository.git"
                            completed = subprocess.run(
                                ("git", "clone", "--mirror", canonical_repository, str(checkout)),
                                check=False,
                                capture_output=True,
                                text=True,
                            )
                            if completed.returncode != 0:
                                raise OperationError(
                                    f"durable candidate source {canonical_repository!r} is unavailable"
                                )
                            external = GitSourceRepository.from_path(
                                source_snapshot_id.source_id,
                                checkout,
                                retention_root,
                            )
                            try:
                                snapshot = external.resolve(SourceRequest(source_snapshot_id.source_id, revision))
                                capability = external.retain(snapshot)
                            finally:
                                external.close()
                except (SourceError, OSError) as exc:
                    raise OperationError("durable candidate exact source revision is unavailable or corrupt") from exc
                if capability.source_snapshot_id != source_snapshot_id:
                    raise OperationError("durable source acquisition resolved a different exact snapshot")
                fresh.append(capability)
            else:
                capability, _source = selected
            retained.append(capability)
    except BaseException as primary_error:
        _release_retained_sources(source_repository, tuple(fresh), primary_error=primary_error)
        raise
    return _DurableSourceEvidence(tuple(retained), tuple(fresh))


def _release_retained_sources(
    source_repository: GitSourceRepository,
    retained_sources: tuple[RetainedSource, ...],
    *,
    primary_error: BaseException | None = None,
) -> tuple[str, ...]:
    released: set[object] = set()
    cleanup_failures: list[str] = []
    for retained in retained_sources:
        if retained.handle in released:
            continue
        try:
            source_repository.release(retained)
        except BaseException as cleanup_error:
            description = _nonthrowing_failure_description(
                f"release durable source handle {retained.handle.value!r}",
                cleanup_error,
            )
            cleanup_failures.append(description)
            if primary_error is not None:
                try:
                    primary_error.add_note(f"also failed during cleanup: {description}")
                except BaseException:
                    pass
        released.add(retained.handle)
    return tuple(cleanup_failures)


def _workload_bindings(changes: AuthoredChangeSet) -> tuple[tuple[str, str], ...]:
    return tuple((binding, path) for binding, path, _revision in _workload_source_bindings(changes))


def _workload_source_bindings(changes: AuthoredChangeSet) -> tuple[tuple[str, str, str | None], ...]:
    direct: set[tuple[str, str, str | None]] = set()
    templates: dict[str, Mapping[str, object]] = {}
    stacks: list[tuple[str, Mapping[str, object]]] = []
    for authored in changes.documents:
        document = authored.document
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not isinstance(specification, dict):
            continue
        if document.get("kind") == "StackTemplate":
            units = specification.get("unitTemplates")
            if isinstance(units, dict):
                templates[name] = units
        elif document.get("kind") == "Stack":
            stacks.append((name, specification))
        else:
            source = specification.get("source")
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                revision = source.get("revision")
                direct.add((name, cast(str, source["path"]), revision if isinstance(revision, str) else None))
    for stack_name, stack in stacks:
        template_name = stack.get("template")
        units = templates.get(template_name) if isinstance(template_name, str) else None
        if units is None:
            continue
        selected = stack.get("units")
        names = (
            set(selected)
            if isinstance(selected, list) and all(isinstance(item, str) for item in selected)
            else set(units)
        )
        for unit_name in names:
            value = units.get(unit_name)
            source = (
                value.get("spec", {}).get("source")
                if isinstance(value, dict) and isinstance(value.get("spec"), dict)
                else None
            )
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                revision = source.get("revision")
                if isinstance(revision, dict):
                    parameter = revision.get("fromParameter")
                    parameter_name = parameter.get("name") if isinstance(parameter, dict) else None
                    parameters = stack.get("parameters")
                    revision = parameters.get(parameter_name) if isinstance(parameters, dict) else None
                direct.add(
                    (
                        f"{stack_name}/{unit_name}",
                        cast(str, source["path"]),
                        revision if isinstance(revision, str) else None,
                    )
                )
    return tuple(sorted(direct))


def _fanout_workload_source_bindings(
    changes: AuthoredChangeSet, workspace: ImmutableWorkspace
) -> tuple[tuple[str, str, str | None], ...]:
    """Recover exact persisted-template source selections for Stack fan-out."""

    templates: dict[str, tuple[Mapping[str, object], str | None, str | None]] = {}
    stacks: dict[str, Mapping[str, object]] = {}
    for authored in changes.documents:
        document = authored.document
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        units = specification.get("unitTemplates") if isinstance(specification, dict) else None
        if document.get("kind") == "StackTemplate" and isinstance(name, str) and isinstance(units, dict):
            templates[name] = (units, None, None)
        elif document.get("kind") == "Stack" and isinstance(name, str) and isinstance(specification, dict):
            stacks[name] = specification
    for entry in workspace.list_entries("stack-templates"):
        if entry.kind is not WorkspaceEntryKind.FILE:
            continue
        document = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        units = specification.get("unitTemplates") if isinstance(specification, dict) else None
        source_context = specification.get("sourceContext") if isinstance(specification, dict) else None
        revision = source_context.get("revision") if isinstance(source_context, dict) else None
        repository = source_context.get("repository") if isinstance(source_context, dict) else None
        if isinstance(name, str) and isinstance(units, dict) and name not in templates:
            templates[name] = (
                units,
                revision if isinstance(revision, str) else None,
                repository if isinstance(repository, str) else None,
            )
    for entry in workspace.list_entries("stacks"):
        if entry.kind is not WorkspaceEntryKind.FILE:
            continue
        document = parse_document_bytes(entry.content or b"", PurePosixPath(entry.key))
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(name, str) and isinstance(specification, dict) and name not in stacks:
            stacks[name] = specification

    bindings: set[tuple[str, str, str | None]] = set()
    for stack_name, specification in stacks.items():
        template_ref = specification.get("templateRef") if isinstance(specification, dict) else None
        template_name = template_ref.get("name") if isinstance(template_ref, dict) else specification.get("template")
        parameters = specification.get("parameters") if isinstance(specification, dict) else None
        template_record = templates.get(template_name) if isinstance(template_name, str) else None
        if template_record is None:
            continue
        units, inherited_revision, inherited_repository = template_record
        if inherited_repository not in {None, "."}:
            # External template/workload evidence is retained and bound by the
            # dedicated multi-source acquisition path above.
            continue
        for unit_name, template in units.items():
            unit_specification = template.get("spec") if isinstance(template, dict) else None
            source = unit_specification.get("source") if isinstance(unit_specification, dict) else None
            if isinstance(source, dict) and isinstance(source.get("fromParameter"), dict):
                parameter_name = cast(dict[str, object], source["fromParameter"]).get("name")
                parameter_value = parameters.get(parameter_name) if isinstance(parameters, dict) else None
                source = parameter_value if isinstance(parameter_value, dict) else None
            path = source.get("path") if isinstance(source, dict) else None
            revision = source.get("revision") if isinstance(source, dict) else None
            if isinstance(revision, dict):
                parameter = revision.get("fromParameter")
                parameter_name = parameter.get("name") if isinstance(parameter, dict) else None
                revision = parameters.get(parameter_name) if isinstance(parameters, dict) else None
            if not isinstance(revision, str):
                revision = inherited_revision
            if isinstance(unit_name, str) and isinstance(path, str) and isinstance(revision, str):
                bindings.add((f"{stack_name}/{unit_name}", path, revision))
    return tuple(sorted(bindings))


def _retained_source_plane(
    snapshot: SourceSnapshot,
    retained: RetainedSource,
    descriptors: tuple[RetainedSourceDescriptor, ...],
) -> RetainedSourcePlane:
    channel = ChannelId(f"retained-source/{snapshot.source_snapshot_id.source_id.value}")
    head = HeadObservation.present(
        channel,
        snapshot.source_snapshot_id.snapshot_id,
        f"retained:{retained.handle.value}",
    )
    view = SnapshotView(snapshot.source_snapshot_id.snapshot_id, snapshot.content_id, snapshot.workspace)
    return RetainedSourcePlane(retained, ExactPlane(head, snapshot.workspace, view), descriptors)


def _stack_template_source_bindings(changes: AuthoredChangeSet) -> tuple[tuple[str, str], ...]:
    bindings: set[tuple[str, str]] = set()
    for authored in changes.documents:
        document = authored.document
        if document.get("kind") != "StackTemplate":
            continue
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        source = specification.get("fromGit") if isinstance(specification, dict) else None
        path = source.get("path") if isinstance(source, dict) else None
        if isinstance(name, str) and isinstance(path, str):
            bindings.add((name, path))
    return tuple(sorted(bindings))


def _one_workspace_document(
    workspace: ImmutableWorkspace, candidates: tuple[str, ...], description: str
) -> tuple[str, bytes]:
    matches = tuple(key for key in candidates if _workspace_file(workspace, key))
    if len(matches) != 1:
        raise OperationError(f"expected exactly one {description} document")
    return matches[0], workspace.read(matches[0])


def _workspace_file(workspace: ImmutableWorkspace, key: str) -> bool:
    try:
        return workspace.get_entry(key).kind is WorkspaceEntryKind.FILE
    except Exception:
        return False


def _logical_source_key(repository: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        relative = Path(os.path.normpath(path)).relative_to(repository.absolute())
    except ValueError as exc:
        raise OperationError(
            f"source-backed apply input {value!r} is outside the project repository and cannot be used with "
            "--source-revision"
        ) from exc
    key = relative.as_posix()
    if not key or key == "." or ".." in relative.parts:
        raise OperationError(f"source-backed apply input {value!r} is invalid")
    return key


def _first_source_workspace_key(repository: Path, labels: tuple[str, ...], workspace: ImmutableWorkspace) -> str:
    if not labels:
        return next(
            (entry.key for entry in workspace.list_entries() if entry.kind is WorkspaceEntryKind.FILE), "gitopsctr.yaml"
        )
    key = _logical_source_key(repository, labels[0])
    if _workspace_file(workspace, key):
        return key
    prefix = f"{key.rstrip('/')}/"
    return next(
        (
            entry.key
            for entry in workspace.list_entries(key)
            if entry.kind is WorkspaceEntryKind.FILE and entry.key.startswith(prefix)
        ),
        key,
    )


def _load_workspace_documents(repository: Path, workspace: ImmutableWorkspace, labels: tuple[str, ...]):  # type: ignore[no-untyped-def]
    if not labels:
        raise OperationError("apply requires at least one input")
    if "-" in labels:
        raise OperationError("--source-revision cannot be used with standard input")
    selected: list[str] = []
    for label in labels:
        key = _logical_source_key(repository, label)
        if _workspace_file(workspace, key):
            if PurePosixPath(key).suffix.lower() not in {".json", ".yaml", ".yml"}:
                raise OperationError(f"apply input must be YAML or JSON: {label}")
            selected.append(key)
            continue
        prefix = f"{key.rstrip('/')}/"
        matches = [
            entry.key
            for entry in workspace.list_entries(key)
            if entry.kind is WorkspaceEntryKind.FILE
            and entry.key.startswith(prefix)
            and PurePosixPath(entry.key).suffix.lower() in {".json", ".yaml", ".yml"}
        ]
        if not matches:
            raise OperationError(f"apply input does not exist: {label}")
        selected.extend(matches)
    return [_issued_file_document(key, workspace.read(key)) for key in sorted(selected)]


def _load_live_documents(repository: Path, labels: tuple[str, ...]):  # type: ignore[no-untyped-def]
    if not labels:
        raise OperationError("apply requires at least one input")
    if labels.count("-") > 1:
        raise OperationError("standard input may be specified only once")
    documents = []
    paths: list[Path] = []
    for label in labels:
        if label == "-":
            documents.extend(_issued_stdin_documents(sys.stdin.read()))
            continue
        path = Path(label)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            checked = path.resolve(strict=True)
        except RuntimeError as exc:
            raise OperationError(f"apply input has an invalid or looping symbolic link: {label}") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OperationError(f"apply input has an invalid or looping symbolic link: {label}") from exc
            raise OperationError(f"apply input does not exist or is unsafe: {label}") from exc
        if checked.is_dir():
            paths.extend(
                child
                for child in sorted(checked.rglob("*"))
                if child.is_file() and child.suffix.lower() in {".json", ".yaml", ".yml"}
            )
        elif checked.suffix.lower() in {".json", ".yaml", ".yml"}:
            paths.append(checked)
        else:
            raise OperationError(f"apply input must be YAML or JSON: {label}")
    for path in paths:
        documents.append(_issued_file_document(str(path), path.read_bytes()))
    return documents


def _issued_file_document(origin: str, raw: bytes):  # type: ignore[no-untyped-def]
    try:
        document = parse_document_bytes(raw, PurePosixPath(origin))
    except Exception as exc:
        raise OperationError(f"{origin}: {exc}") from exc
    return _issue_authored_document(origin, document, ContentId(f"sha256:{hashlib.sha256(raw).hexdigest()}"))


def _reject_unretained_repository_sources(changes: AuthoredChangeSet) -> None:
    """Reject repository-backed Units before invoking a source selection port."""

    for authored in changes.documents:
        document = authored.document
        metadata = document.get("metadata")
        specification = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        source = specification.get("source") if isinstance(specification, dict) else None
        if isinstance(name, str) and isinstance(source, dict) and isinstance(source.get("path"), str):
            raise OperationError(f"Unit {name!r} requires --source-revision <commit>")


def _reject_direct_source_revisions(changes: AuthoredChangeSet) -> None:
    """Keep workload revision selection scoped to StackTemplate projection."""

    for authored in changes.documents:
        document = authored.document
        if document.get("kind") in {"Stack", "StackTemplate"}:
            continue
        metadata = document.get("metadata")
        specification = document.get("spec")
        source = specification.get("source") if isinstance(specification, dict) else None
        if (
            isinstance(metadata, dict)
            and set(metadata) == {"name"}
            and isinstance(source, dict)
            and source.get("revision") is not None
        ):
            raise OperationError("source.revision is supported only in a StackTemplate projection")


def _issued_stdin_documents(raw: str):  # type: ignore[no-untyped-def]
    try:
        values = list(yaml.safe_load_all(raw))
        nodes = list(yaml.compose_all(raw))
    except yaml.YAMLError as exc:
        raise OperationError(f"standard input is invalid YAML: {exc}") from exc
    parsed = [
        (value, node) for value, node in zip(values, nodes, strict=False) if value is not None and node is not None
    ]
    documents = []
    for index, (value, node) in enumerate(parsed, 1):
        try:
            document = require_json_value(value)
        except ValueError as exc:
            raise OperationError(f"standard input document {index} is invalid: {exc}") from exc
        if not isinstance(document, dict):
            raise OperationError(f"standard input document {index} must be a resource mapping")
        start = 0 if index == 1 else node.start_mark.index
        end = len(raw) if index == len(parsed) else parsed[index][1].start_mark.index
        exact = raw[start:end].encode()
        documents.append(
            _issue_authored_document(
                f"stdin#{index}", document, ContentId(f"sha256:{hashlib.sha256(exact).hexdigest()}")
            )
        )
    return documents
