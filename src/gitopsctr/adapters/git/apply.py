"""Git implementation of the typed apply input and execution boundaries.

This module is intentionally the only place that translates opaque apply
selections back to the local Git implementation.  The application facade sees
neither paths nor Git command-line values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from gitopsctr.application.apply import ApplyCommand, ApplyResult, AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.model import ContentId, SnapshotId, SourceId, SourceSnapshotId
from gitopsctr.application.sources import SourceRequest

_PUBLISHED_SNAPSHOT_PREFIX = "git-commit:"
_DEFAULT_GIT_SOURCE_ID = SourceId("default-git-source")


@dataclass(frozen=True, slots=True)
class GitAuthoredChangeDecoder:
    """Decode all local authored input forms into one typed change set."""

    repository: Path
    source_id: SourceId = _DEFAULT_GIT_SOURCE_ID

    def close(self) -> None:
        """The decoder retains no resource handles."""

    def decode(self, command: ApplyCommand) -> AuthoredChangeSet:
        # The legacy parser is retained temporarily as the one normalizer for
        # all file/stdin/worktree spellings.  Import lazily so composition does
        # not create a controller-to-adapter import cycle.
        from gitopsctr import controller

        source_revision = _source_selector(command.source_request, self.source_id)
        source_snapshot_id = None
        if source_revision is not None:
            resolved = controller.git("rev-parse", f"{source_revision}^{{commit}}").stdout.strip()
            source_snapshot_id = SourceSnapshotId(self.source_id, SnapshotId(resolved))
            controller._validate_apply_input_selection(
                command.input_labels,
                resolved,
                operation="apply",
                revision_option="--source-revision",
            )
            with controller.tempfile.TemporaryDirectory(prefix="gitopsctr-authored-input-") as directory:
                source_root = Path(directory) / "source"
                controller.materialize_revision(resolved, source_root)
                documents = controller._load_apply_documents(
                    command.input_labels,
                    source_revision=resolved,
                    source_root=source_root,
                )
        else:
            documents = controller._load_apply_documents(command.input_labels)
        return AuthoredChangeSet(
            tuple(
                _issue_authored_document(item.origin, item.document, ContentId(item.document_digest))
                for item in documents
            ),
            source_snapshot_id,
        )


@dataclass(frozen=True, slots=True)
class GitApplyService:
    """Adapt the existing Git projection/publishing implementation to apply intent.

    The compatibility implementation is deliberately fed only decoded typed
    documents.  It is an intermediate seam while the individual source,
    workspace, retention, and publication operations are extracted from the
    mature Git workflow; the controller itself no longer coordinates them.
    """

    repository: Path
    source_id: SourceId = _DEFAULT_GIT_SOURCE_ID

    def close(self) -> None:
        """The underlying operation owns only per-call temporary resources."""

    def apply(self, command: ApplyCommand, changes: AuthoredChangeSet) -> ApplyResult:
        from gitopsctr import controller

        _source_selector(command.source_request, self.source_id)
        source_revision = _exact_source_revision(command.source_request, changes, self.source_id)
        arguments = SimpleNamespace(
            environment=str(command.environment_id),
            files=list(command.input_labels),
            partition=command.partition,
            source_revision=source_revision,
            desired_ref=str(command.desired_channel) if command.desired_channel is not None else None,
            observed_ref=str(command.observed_channel) if command.observed_channel is not None else None,
            candidate_ref=str(command.candidate_channel) if command.candidate_channel is not None else None,
            dry=command.dry_run,
            verbose=command.verbose,
        )
        # Source-backed documents are decoded only after the exact immutable
        # source is materialized.  The executor owns that source boundary.
        documents = controller._apply_documents_from_change_set(changes)
        revision = controller._execute_git_apply(arguments, documents=documents)
        return ApplyResult(
            SnapshotId(f"{_PUBLISHED_SNAPSHOT_PREFIX}{revision}") if revision is not None else None,
            None,
        )


def source_request_for_git(value: str | None) -> SourceRequest | None:
    """Translate a default-Git selector into a backend-neutral source request."""

    return SourceRequest(_DEFAULT_GIT_SOURCE_ID, value) if value is not None else None


def _source_selector(request: SourceRequest | None, expected_source_id: SourceId) -> str | None:
    """Validate that a request belongs to this configured Git source."""

    if request is None:
        return None
    if request.source_id != expected_source_id:
        raise ValueError(f"Git apply is not configured for source {request.source_id!s}")
    return request.selector


def _exact_source_revision(
    request: SourceRequest | None,
    changes: AuthoredChangeSet,
    expected_source_id: SourceId,
) -> str | None:
    """Consume only the decoder's exact source snapshot, never its selector."""

    source_snapshot_id = changes.source_snapshot_id
    if request is None:
        if source_snapshot_id is not None:
            raise ValueError("a source-backed authored change set requires a source request")
        return None
    if source_snapshot_id is None:
        raise ValueError("source-backed apply requires an exactly decoded source snapshot")
    if source_snapshot_id.source_id != expected_source_id:
        raise ValueError(f"decoded authored source does not belong to configured source {expected_source_id!s}")
    return source_snapshot_id.snapshot_id.value
