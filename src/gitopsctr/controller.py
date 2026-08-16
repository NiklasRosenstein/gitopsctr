"""Resolve deployment state and run registered drivers.

Desired and observed documents are the contract. This module is the main
controller API used by local callers and the command-line adapter.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import glob as globlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TextIO, cast

import yaml

from gitopsctr import operational
from gitopsctr.api import GVK, ApiError
from gitopsctr.artifacts import require_artifact_api
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    EXACT_REVISION_PATTERN,
    QUALIFIED_RESOURCE_NAME_PATTERN,
    ArtifactDescriptor,
    ArtifactImport,
    AuthoredSource,
    DeletionMetadata,
    DesiredOwnerReference,
    DesiredSource,
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    MaterializationDocument,
    ReceiptDesired,
    ResolvedArtifactImport,
    StackActiveProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackSpec,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateGitSpec,
    StackTemplateInlineSpec,
    StackTemplatePromotionSpec,
    StackTemplateReference,
    StackTemplateRequestedFromGit,
    StackTemplateRequestedFromInput,
    StackTemplateRequestedFromPromotion,
    StackTemplateResolvedFromGit,
    StackTemplateResolvedFromGitSource,
    StackTemplateResolvedFromInput,
    StackTemplateResolvedFromPromotion,
    StackTemplateResolvedFromPromotionSource,
    StackTemplateResource,
    StackTemplateSourceContext,
    StackTemplateSpec,
    StrictModel,
    scope_stack_template_resources,
    stack_generated_unit_name,
    with_schema,
)
from gitopsctr.contracts import StackProjection as StructuralStackProjection
from gitopsctr.dependencies import (
    convergence_order,
    convergence_scope,
    dependency_graph,
    desired_observation_reference_units,
    observation_reference_units,
)
from gitopsctr.document import ContractError, DocumentContract, JsonObject, JsonObjectValue, require_json_value
from gitopsctr.driver import (
    DriverError,
    MaterializationContext,
    MaterializationResult,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationOutput,
    TeardownCapability,
    TeardownContext,
    TeardownResult,
    TeardownUnsupported,
    UnitResolutionContext,
    VerificationContext,
    VerificationStatus,
)
from gitopsctr.errors import OperationError, ReferenceUnavailable
from gitopsctr.execution import DriverExecution
from gitopsctr.forges import (
    ChangeRequestResult,
    ChangeRequestSpec,
    ManualChangeRequest,
    ensure_change_request,
)
from gitopsctr.formats import (
    DEFAULT_CANDIDATE_REF_TEMPLATE,
    DEFAULT_DESIRED_REF_TEMPLATE,
    DEFAULT_OBSERVED_REF_TEMPLATE,
    PROJECT_CONFIG_NAMES,
    DocumentFormat,
    DocumentFormatError,
    SourceRevisionAction,
    SourceRevisionPolicy,
    SourceRevisionUnavailableWhen,
    document_candidates,
    load_document,
    load_project_config,
    project_config_path,
    project_environment_root,
    validate_project_document,
    write_document,
)
from gitopsctr.inspection import command_get as inspect_resources
from gitopsctr.inspection import identity_filter_options, inspectable_selectors
from gitopsctr.registry import (
    API_KINDS,
    DRIVER_GVKS,
    DRIVER_NAMES_BY_GVK,
    DRIVER_VERSIONS,
    MATERIALIZATION_DRIVERS,
    PLANNING_DRIVERS,
    RECONCILIATION_DRIVERS,
    RESOURCE_REGISTRY,
    UNIT_DRIVERS,
    VERIFICATION_DRIVERS,
    semantic_reconciliation_result,
)
from gitopsctr.resolution import (
    FingerprintedValue,
    PromotionReferenceSelection,
    ResolutionContext,
    TemplateResolution,
)
from gitopsctr.resolution import (
    resolve_template as resolve_template_value,
)
from gitopsctr.resource_model import ResourcePlane
from gitopsctr.resources import (
    CORE_API_VERSION,
    UNIT_API_VERSION,
    ReceiptResource,
    ReceiptSpec,
    ReceiptStatus,
    ReceiptSubject,
    ResourceCatalog,
    ResourceMetadata,
    StackResource,
    UnitResource,
    desired_unit_binding_digest,
    validate_desired_resource_graph,
)
from gitopsctr.schemas import encoded_schema, export_schemas, resource_schema_url, show_schema
from gitopsctr.state import (
    AcceptedDesiredTarget,
    ControllerPin,
    GatedCandidate,
    GitSourceRevision,
    GitStateStore,
    PublishedTree,
    canonical_publication_ref,
)
from gitopsctr.templates import (
    ArtifactReference as ArtifactReferenceExpression,
)
from gitopsctr.templates import (
    ArtifactReferenceTarget,
    ProjectionObject,
    TemplateError,
    TemplateValue,
    dump_template_value,
    parse_template_value,
)
from gitopsctr.templates import (
    contains_reference as template_contains_reference,
)
from gitopsctr.templates import (
    references as template_references,
)

GIT_AUTHOR_NAME = os.environ.get("GITOPSCTR_GIT_AUTHOR_NAME", "gitopsctr")
GIT_AUTHOR_EMAIL = os.environ.get(
    "GITOPSCTR_GIT_AUTHOR_EMAIL",
    "gitopsctr@users.noreply.github.com",
)
REPOSITORY_ROOT = Path.cwd().resolve()
DESIRED_TRANSITION_BLOCKS_PATH = PurePosixPath(".gitopsctr/transition-blocks.json")
DESIRED_CLEANUP_UNITS_PATH = PurePosixPath(".gitopsctr/cleanup/units")
DESIRED_EFFECT_LEASES_PATH = PurePosixPath(".gitopsctr/effect-leases/units")
DESIRED_RESOURCE_INCARNATIONS_PATH = PurePosixPath(".gitopsctr/incarnations/resources")
DESIRED_PROJECTION_CONTEXTS_PATH = PurePosixPath(".gitopsctr/projection-contexts")
OBSERVED_TEARDOWN_EVIDENCE_PATH = PurePosixPath(".gitopsctr/teardowns/units")
EFFECT_LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class PromotionContext:
    source_environment: str
    desired_ref: str
    desired_revision: str
    observed_ref: str
    observed_revision: str | None
    specification_revision: str
    desired_root: Path
    observed_root: Path | None = None

    def document(self) -> dict[str, Any]:
        return with_schema(
            {
                "source": {
                    "environment": self.source_environment,
                    "desiredRef": self.desired_ref,
                    "desiredRevision": self.desired_revision,
                    "observedRef": self.observed_ref,
                    "observedRevision": self.observed_revision,
                },
                "specificationRevision": self.specification_revision,
            },
            str(CORE_CONTRACTS["promotion"].json_schema()["$id"]),
        )


@dataclass(frozen=True)
class ApplyInputDocument:
    """One explicitly supplied document and the bytes used to acquire it."""

    origin: str
    document: JsonObject
    document_digest: str


@dataclass(frozen=True)
class UnitChangeExplanation:
    previous_desired_revision: str
    previous_source_revision: str | None
    current_source_revision: str | None
    causes: tuple[str, ...]
    commits: tuple[str, ...]
    files: tuple[str, ...]
    specification_paths: tuple[str, ...]


ANSI_RESET = "\x1b[0m"
ANSI_ROLES = {
    "heading": "\x1b[1;36m",
    "entity": "\x1b[1;36m",
    "revision": "\x1b[1;33m",
    "muted": "\x1b[2m",
    "success": "\x1b[1;32m",
    "warning": "\x1b[1;33m",
    "error": "\x1b[1;31m",
    "focus": "\x1b[1;36m",
    "label": "\x1b[1m",
    "environment": "\x1b[3;4m",
}
ANSI_ROLE_CLOSINGS = {"environment": "\x1b[23;24m"}
STATUS_ROLES = {
    "DONE": "success",
    "CLEAN": "success",
    "VALID": "success",
    "KEEP": "success",
    "UPDATE": "success",
    "OBSERVE": "success",
    "MATERIALIZED": "success",
    "WAIT": "warning",
    "REFRESH": "warning",
    "SKIP": "warning",
    "RETRY": "warning",
    "DRY": "warning",
    "MANUAL": "warning",
    "DRIFT": "warning",
    "UNSCOPED": "warning",
    "APPROVE": "warning",
    "WARN": "warning",
    "FAILED": "error",
    "ERROR": "error",
    "INVALID": "error",
    "READY": "focus",
    "CHANGED": "focus",
    "UP TO DATE": "success",
    "ADDED": "success",
    "UPDATED": "success",
    "UNCHANGED": "muted",
    "RUN": "focus",
    "NEXT": "focus",
    "PROMOTE": "focus",
    "PIN": "focus",
    "VERIFY": "focus",
    "PLAN": "focus",
    "REVIEW": "focus",
}


def _truthy_environment(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return bool(value) and value not in {"0", "false", "no", "off"}


def _is_regular_file(stream: TextIO) -> bool:
    try:
        return stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):
        return False


def color_enabled(stream: TextIO) -> bool:
    """Choose ANSI styling without contaminating redirected or machine output."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if _is_regular_file(stream) or os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        if stream.isatty():
            return True
    except (AttributeError, OSError, ValueError):
        pass
    return _truthy_environment("CI")


def style_text(text: str, role: str, stream: TextIO | None = None) -> str:
    """Apply a portable ANSI role only when the destination supports styling."""
    output_stream = stream or sys.stderr
    code = ANSI_ROLES[role]
    closing = ANSI_ROLE_CLOSINGS.get(role, ANSI_RESET)
    return f"{code}{text}{closing}" if color_enabled(output_stream) else text


def style_unit(unit_name: str, stream: TextIO | None = None) -> str:
    return style_text(unit_name, "entity", stream)


def style_branch(ref: str, stream: TextIO | None = None) -> str:
    return style_text(ref, "entity", stream)


def style_environment(environment: str, stream: TextIO | None = None) -> str:
    return style_text(environment, "environment", stream)


def style_units(unit_names: Sequence[str], stream: TextIO | None = None) -> str:
    return ", ".join(style_unit(unit_name, stream) for unit_name in unit_names)


def status_role(status: str, message: str) -> str:
    if status.upper() in {"RESULT", "APPLY", "PLAN"}:
        result = message.split(":", 1)[0].strip().upper()
        if result.startswith("FAILED"):
            return "error"
        if result == "DRIFT":
            return "warning"
        if result in {"CLEAN", "VALID"} or result.startswith("SUCCEEDED"):
            return "success"
    return STATUS_ROLES.get(status.upper(), "label")


def log_heading(message: str) -> None:
    """Write a visually distinct phase heading without polluting command result stdout."""
    print(f"\n==> {style_text(message, 'heading')}", file=sys.stderr, flush=True)


def log_status(status: str, message: str) -> None:
    """Write one consistently aligned deployment progress line."""
    padding = " " * (max(8 - len(status), 0) + 1)
    rendered_status = style_text(status, status_role(status, message))
    print(f"    {rendered_status}{padding}{message}", file=sys.stderr, flush=True)


def log_reconcile_outcome(
    status: str,
    reason: str,
    action: str,
    effects: Sequence[tuple[str, str]],
) -> None:
    """Render the at-a-glance result of reconciling one unit."""

    log_status(status, reason)
    log_status("ACTION", action)
    if effects:
        for effect_status, message in effects:
            log_status(effect_status, message)
    else:
        log_status("EFFECTS", "None")


def short_revision(revision: str | None) -> str:
    if revision is None:
        return "none"
    if revision.startswith("dry:"):
        return f"dry:{revision.removeprefix('dry:')[:12]}"
    return revision[:12]


def run(
    *args: str,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "check": check,
        "text": True,
        "input": input_text,
        "env": env,
        "cwd": cwd,
    }
    kwargs["capture_output"] = True
    return subprocess.run(args, **kwargs)


@cache
def commit_subject(repository_root: Path, revision: str) -> str | None:
    """Return a safe, single-line commit subject without disrupting CLI output on failure."""
    try:
        result = run(
            "git",
            "show",
            "--no-patch",
            "--format=%s",
            revision,
            check=False,
            cwd=repository_root,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    printable = "".join(character if character.isprintable() else " " for character in result.stdout)
    subject = " ".join(printable.split())
    if not subject:
        return None
    if len(subject) > 72:
        subject = subject[:71] + "…"
    return subject


def describe_revision(revision: str | None, stream: TextIO | None = None) -> str:
    """Render a shortened revision with its bounded commit subject when available."""
    shortened = short_revision(revision)
    if revision is None:
        return shortened
    resolved_revision = revision.removeprefix("dry:")
    subject = commit_subject(REPOSITORY_ROOT, resolved_revision)
    if not subject:
        return style_text(shortened, "revision", stream)
    return f"{style_text(shortened, 'revision', stream)} ({style_text(subject, 'muted', stream)})"


def git(*args: str, check: bool = True, input_text: str | None = None, env=None):
    return state_store().git(*args, check=check, input_text=input_text, env=env)


def working_tree_has_uncommitted_changes() -> bool:
    """Return whether tracked, staged, or untracked work is absent from a commit snapshot."""
    status = git("status", "--porcelain=v1", "--untracked-files=normal")
    return bool(status.stdout)


def warn_if_source_revision_excludes_changes(source_revision: str | None) -> None:
    if source_revision is None or not working_tree_has_uncommitted_changes():
        return
    log_status(
        "WARN",
        f"uncommitted working-tree changes are excluded from source revision "
        f"{describe_revision(source_revision)}; commit them and select the resulting commit to include them",
    )


@cache
def _state_store(root: Path) -> GitStateStore:
    return GitStateStore(root, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL)


def state_store() -> GitStateStore:
    return _state_store(REPOSITORY_ROOT)


def resolve_repository_root(repository: str | None) -> Path:
    candidate = Path(repository or os.environ.get("GITOPSCTR_REPOSITORY", Path.cwd())).resolve()
    result = run(
        "git",
        "-C",
        str(candidate),
        "rev-parse",
        "--show-toplevel",
        check=False,
    )
    if result.returncode != 0:
        raise OperationError(f"not inside a Git repository: {candidate}")
    return Path(result.stdout.strip()).resolve()


def fetch_ref(ref: str) -> str | None:
    return state_store().fetch(ref).revision


def resolve_ref(ref: str, revision: str | None = None) -> str:
    snapshot = state_store().resolve(ref, revision)
    assert snapshot.revision is not None
    return snapshot.revision


def materialize_revision(revision: str, output: Path) -> None:
    state_store().materialize(revision, output)


def refresh_materialized_root(revision: str, output: Path) -> None:
    """Replace a local desired tree with the exact revision used by an effect."""

    with tempfile.TemporaryDirectory() as temporary_directory:
        refreshed = Path(temporary_directory) / "desired"
        materialize_revision(revision, refreshed)
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(refreshed, output)


def command_read_tree(args: argparse.Namespace) -> None:
    head = fetch_ref(args.ref)
    if head is None:
        if args.allow_missing:
            return
        raise OperationError(f"ref {args.ref!r} does not exist")

    revision = head
    if args.revision:
        revision = git("rev-parse", f"{args.revision}^{{commit}}").stdout.strip()
        if args.require_ancestor:
            result = git("merge-base", "--is-ancestor", revision, head, check=False)
            if result.returncode != 0:
                raise OperationError(f"requested revision is not part of {args.ref} history")

    materialize_revision(revision, Path(args.output))
    print(revision)


def publish_tree(
    ref: str,
    directory: Path,
    parent: str | None,
    message: str,
    source_pins: Mapping[str, str] | None = None,
    *,
    expected_publication_head: str | None,
) -> str:
    store = state_store()
    if source_pins:
        return store.publish(
            ref,
            directory,
            parent,
            message,
            source_pins=source_pins,
            expected_publication_head=expected_publication_head,
        ).revision
    return store.publish(
        ref,
        directory,
        parent,
        message,
        expected_publication_head=expected_publication_head,
    ).revision


def parse_expected_publication_head(value: str) -> str | None:
    """Parse the CLI's explicit publication-head expectation."""

    return None if value == "absent" else value


def verify_gated_candidate(candidate_revision: str | None, target_revision: str | None) -> GatedCandidate:
    """Verify a change-gated candidate against the exact target head used to build it."""

    return state_store().verify_gated_candidate(candidate_revision, target_revision)


def command_publish_tree(args: argparse.Namespace) -> None:
    expected_publication_head = getattr(args, "expected_publication_head", args.parent)
    commit = publish_tree(
        args.ref,
        Path(args.directory),
        args.parent,
        args.message,
        expected_publication_head=expected_publication_head,
    )
    print(commit)


def command_schemas_show(args: argparse.Namespace) -> None:
    try:
        document = show_schema(args.driver, args.kind)
    except ValueError as exc:
        raise OperationError(str(exc)) from exc
    print(encoded_schema(document), end="")


def command_schemas_export(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    changed = export_schemas(directory, check=args.check)
    if args.check and changed:
        raise OperationError("generated schemas are stale: " + ", ".join(path.as_posix() for path in changed))
    if not args.check:
        print(directory.resolve())


RESOURCE_CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)


def load_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.load_document(path))


def normalize_environment_document(document: dict[str, Any], expected_name: str | None = None) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.normalize_environment(cast(JsonObject, document), expected_name))


def normalize_promotion_document(document: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.normalize_promotion(cast(JsonObject, document)))


def parse_authored_unit_document(
    document: dict[str, Any], expected_name: str | None = None
) -> UnitResource[StrictModel]:
    return RESOURCE_CATALOG.parse_unit(cast(JsonObject, document), profile="authored", expected_name=expected_name)


def parse_desired_unit_document(
    document: dict[str, Any], expected_name: str | None = None
) -> UnitResource[StrictModel]:
    return RESOURCE_CATALOG.parse_unit(cast(JsonObject, document), profile="desired", expected_name=expected_name)


def serialize_environment_document(document: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.serialize_environment(cast(JsonObject, document)))


def serialize_unit_document(
    unit: UnitResource[Any], *, profile: Literal["authored", "desired"] = "desired"
) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.serialize_unit(unit, profile=profile))


def load_receipt(path: Path, expected_unit: str | None = None) -> ReceiptResource[Any]:
    return RESOURCE_CATALOG.load_receipt(path, expected_unit)


resource_documents_enabled = RESOURCE_CATALOG.resource_documents_enabled
unit_document_path = RESOURCE_CATALOG.unit_document_path


def receipt_document_path(root: Path, qualified_name: str) -> Path:
    """Return the canonical observed Receipt path for a Unit address."""

    relative = PurePosixPath(qualified_name)
    candidates = document_candidates(root / "units" / relative.parent, relative.name)
    if len(candidates) > 1:
        raise OperationError(f"multiple Receipt document formats exist for {qualified_name!r}")
    if candidates:
        return candidates[0]
    if not resource_documents_enabled(REPOSITORY_ROOT):
        return root / "units" / relative.parent / f"{relative.name}.json"
    project = load_project_config(REPOSITORY_ROOT)
    return RESOURCE_REGISTRY.document_path(
        family="receipt",
        plane=ResourcePlane.OBSERVED,
        root=root,
        repository_root=REPOSITORY_ROOT,
        project=project,
        environment=None,
        qualified_name=qualified_name,
        suffix=project.write_format.suffix,
    )


def load_desired_unit(path: Path, expected_name: str | None = None) -> UnitResource[Any]:
    return RESOURCE_CATALOG.load_unit(path, expected_name, profile="desired")


def persisted_unit_driver_name(path: Path) -> str | None:
    """Inspect only envelope identity when an obsolete payload cannot be parsed."""

    document = RESOURCE_CATALOG.load_document(path)
    api_version, kind = document.get("apiVersion"), document.get("kind")
    if not isinstance(api_version, str) or not isinstance(kind, str):
        return None
    return DRIVER_NAMES_BY_GVK.get(f"{api_version}/{kind}")


@dataclass(frozen=True)
class PersistedUnitSourceIdentity:
    revision: str | None
    input_hash: str | None
    environment: str | None


def persisted_unit_source_identity(path: Path) -> PersistedUnitSourceIdentity:
    """Read persisted source identity without parsing the driver-specific desired payload."""

    document = RESOURCE_CATALOG.load_document(path)
    specification = document.get("spec")
    source = specification.get("source") if isinstance(specification, dict) else None
    revision = source.get("revision") if isinstance(source, dict) else None
    input_hash = source.get("inputHash") if isinstance(source, dict) else None
    environment = specification.get("environment") if isinstance(specification, dict) else None
    return PersistedUnitSourceIdentity(
        revision if isinstance(revision, str) else None,
        input_hash if isinstance(input_hash, str) else None,
        environment if isinstance(environment, str) else None,
    )


reference_document_path = RESOURCE_CATALOG.reference_document_path


def write_unit(path: Path, unit: UnitResource[Any], project_root: Path) -> Path:
    return RESOURCE_CATALOG.write_unit(path, unit, project_root)


def write_desired_candidate_unit(path: Path, unit: UnitResource[Any], project_root: Path) -> Path:
    """Write a canonical desired Unit using the configured repository format."""

    if resource_documents_enabled(project_root):
        return write_unit(path, unit, project_root)
    selected = DocumentFormat.YAML if path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    return write_document(path, serialize_unit_document(unit, profile="desired"), format=selected)


def _validate_desired_projection_context_records(
    root: Path,
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
) -> None:
    """Require every structural and active Stack context digest to be durable."""

    for resource in resources.values():
        if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
            continue
        if not isinstance(resource.spec, DesiredStackSpec):
            continue
        digests = {resource.spec.structuralProjection.identity.projectionContextDigest}
        if resource.spec.activeProjection is not None:
            digests.add(resource.spec.activeProjection.projectionContextDigest)
            digests.update(binding.projectionContextDigest for binding in resource.spec.activeProjection.units.values())
        for digest in sorted(digests):
            load_projection_context(root, digest)


def load_desired_resource_graph(
    root: Path, *, validate: bool = True
) -> dict[tuple[str, str, str], UnitResource[Any] | StackResource]:
    """Load and validate every desired resource in one desired ref before effects."""

    resources: dict[tuple[str, str, str], UnitResource[Any] | StackResource] = {}
    for qualified_name, path in _current_desired_unit_paths(root).items():
        unit = load_desired_unit(path, path.stem)
        key = (unit.gvk.api_version, unit.gvk.kind, qualified_name)
        if key in resources:
            raise OperationError(f"duplicate desired resource identity: {key!r}")
        resources[key] = unit
    for kind in ("StackTemplate", "Stack"):
        for resource_name, path in _current_desired_stack_paths(root, kind).items():
            resource = (
                RESOURCE_CATALOG.parse_stack_template(
                    RESOURCE_CATALOG.load_document(path), profile="desired", expected_name=resource_name
                )
                if kind == "StackTemplate"
                else RESOURCE_CATALOG.parse_stack(
                    RESOURCE_CATALOG.load_document(path), profile="desired", expected_name=resource_name
                )
            )
            key = (resource.gvk.api_version, resource.gvk.kind, resource.name)
            if key in resources:
                raise OperationError(f"duplicate desired resource identity: {key!r}")
            resources[key] = resource
    finalized_identities = {
        (tombstone.api_version, tombstone.kind, tombstone.qualified_name, tombstone.uid)
        for tombstone in load_resource_incarnation_evidence(root)
    }
    for key, resource in resources.items():
        if (
            resource.metadata.uid is not None
            and (resource.gvk.api_version, resource.gvk.kind, key[2], resource.metadata.uid) in finalized_identities
        ):
            raise OperationError(
                f"desired resource {resource.gvk.kind}/{resource.name} reuses finalized UID {resource.metadata.uid!r}"
            )
    if not validate:
        return resources
    try:
        validate_desired_resource_graph(resources)
    except ValueError as exc:
        # A deleting Stack remains in desired state while its owned Units are
        # finalized in reverse dependency order. Admit only missing generated
        # children of that deleting Stack; all other graph failures remain fatal.
        message = str(exc)
        missing = re.fullmatch(r"Stack '([^']+)' expansion is missing generated Unit '([^']+)'", message)
        if missing is not None:
            stack_name, unit_name = missing.groups()
            stack_key = (CORE_API_VERSION, "Stack", stack_name)
            stack_resource = resources.get(stack_key)
            template_resource = (
                resources.get((CORE_API_VERSION, "StackTemplate", stack_resource.spec.templateRef.name))
                if isinstance(stack_resource, StackResource) and isinstance(stack_resource.spec, DesiredStackSpec)
                else None
            )
            if (
                isinstance(stack_resource, StackResource)
                and resource_deletion(stack_resource) is not None
                and isinstance(template_resource, StackResource)
                and isinstance(template_resource.spec, StackTemplateSpec)
                and isinstance(stack_resource.spec, (StackSpec, DesiredStackSpec))
            ):
                missing_resources = {
                    (
                        resource.apiVersion,
                        resource.kind,
                        stack_generated_unit_name(stack_name, resource.name),
                    )
                    for resource in scope_stack_template_resources(
                        stack_name,
                        template_resource.spec.expand(stack_resource.spec.parameters),
                    )
                    if (
                        resource.apiVersion,
                        resource.kind,
                        stack_generated_unit_name(stack_name, resource.name),
                    )
                    not in resources
                }
                tombstones = load_resource_incarnation_evidence(root)
                active_bindings = (
                    {
                        (
                            binding.apiVersion,
                            binding.kind,
                            stack_generated_unit_name(stack_name, binding.name),
                        ): binding.uid
                        for binding in stack_resource.spec.activeProjection.units.values()
                    }
                    if isinstance(stack_resource.spec, DesiredStackSpec)
                    and stack_resource.spec.activeProjection is not None
                    else {}
                )

                def has_exact_tombstone(key: tuple[str, str, str]) -> bool:
                    expected_uid = active_bindings.get(key)
                    return any(
                        (tombstone.api_version, tombstone.kind, tombstone.qualified_name) == key
                        and (expected_uid is None or tombstone.uid == expected_uid)
                        for tombstone in tombstones
                    )

                if (
                    missing_resources
                    and any(PurePosixPath(name).name == unit_name for _api_version, _kind, name in missing_resources)
                    and all(has_exact_tombstone(key) for key in missing_resources)
                ):
                    _validate_desired_projection_context_records(root, resources)
                    return resources
        raise OperationError(f"invalid desired resource graph: {exc}") from exc
    _validate_desired_projection_context_records(root, resources)
    return resources


def qualified_unit_name(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    unit: UnitResource[Any],
) -> str:
    """Return the registry-defined operator address for one validated desired Unit."""

    owners = unit.metadata.ownerReferences
    if not owners:
        return unit.name
    if len(owners) != 1:
        raise OperationError(f"Unit {unit.name!r} does not have exactly one owner")
    owner = owners[0]
    stack = resources.get((owner.apiVersion, owner.kind, owner.name))
    # Unit-to-Unit ownership is used for teardown ordering but does not create
    # an operator-facing hierarchy. Only the registered Stack ownership
    # relationship contributes a parent address.
    if not isinstance(stack, StackResource):
        return unit.name
    if stack.metadata.uid != owner.uid:
        raise OperationError(f"Unit {unit.name!r} has no exact owning Stack")
    if not isinstance(stack.spec, DesiredStackSpec) or stack.spec.activeProjection is None:
        raise OperationError(f"Stack {stack.name!r} has no active projection for Unit {unit.name!r}")
    matches = tuple(
        logical_name
        for logical_name, binding in stack.spec.activeProjection.units.items()
        if (binding.apiVersion, binding.kind, binding.name, binding.uid)
        == (unit.gvk.api_version, unit.gvk.kind, unit.name, unit.metadata.uid)
    )
    if len(matches) != 1:
        raise OperationError(
            f"Stack {stack.name!r} has {len(matches)} active bindings for Unit {unit.name!r}; expected one"
        )
    return f"{stack.name}/{matches[0]}"


def qualified_unit_name_map(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
) -> dict[str, str]:
    """Map canonical operator addresses to concrete desired Unit names."""

    result: dict[str, str] = {}
    for key, resource in resources.items():
        if not isinstance(resource, UnitResource):
            continue
        qualified = qualified_unit_name(resources, resource)
        previous = result.get(qualified)
        if previous is not None and previous != key[2]:
            raise OperationError(f"duplicate Unit qualified name {qualified!r}")
        result[qualified] = key[2]
    return result


def resolve_unit_selectors(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    selectors: Sequence[str],
    tombstones: Sequence[ResourceIncarnationTombstone] = (),
) -> tuple[str, ...]:
    addresses = qualified_unit_name_map(resources)
    for tombstone in tombstones:
        if f"{tombstone.api_version}/{tombstone.kind}" not in DRIVER_NAMES_BY_GVK:
            continue
        qualified = tombstone.qualified_name or tombstone.name
        previous = addresses.get(qualified)
        if previous is not None and previous != qualified:
            raise OperationError(f"duplicate Unit qualified name {qualified!r}")
        addresses.setdefault(qualified, qualified)
    unknown = tuple(selector for selector in selectors if selector not in addresses)
    if unknown:
        available = ", ".join(sorted(addresses)) or "none"
        raise OperationError(f"unknown Unit qualified name(s): {', '.join(unknown)}; available Units: {available}")
    return tuple(addresses[selector] for selector in selectors)


def resolve_qualified_unit_values(selectors: Sequence[str], addresses: Mapping[str, str]) -> tuple[str, ...]:
    unknown = tuple(selector for selector in selectors if selector not in addresses)
    if unknown:
        available = ", ".join(sorted(addresses)) or "none"
        raise OperationError(f"unknown Unit qualified name(s): {', '.join(unknown)}; available Units: {available}")
    return tuple(addresses[selector] for selector in selectors)


def stack_dependency_edges(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    *,
    include_missing: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Return explicit StackTemplate dependency edges for materialized Units.

    These edges are controller-owned graph metadata, not driver input. They are
    normalized by Unit name because the existing convergence and teardown APIs
    operate on Unit names, while resource identity and UID fencing remain
    validated by ``load_desired_resource_graph``.
    """

    edges: dict[str, set[str]] = {}
    for stack in (
        resource
        for resource in resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack"
    ):
        if not isinstance(stack.spec, DesiredStackSpec):
            continue
        active = stack.spec.activeProjection
        use_active = active is not None and (
            active.sourceProjectionDigest != stack.spec.structuralProjection.identity.projectionDigest
        )
        if use_active:
            for binding in active.units.values():
                qualified_name = stack_generated_unit_name(stack.name, binding.name)
                if not include_missing and not any(
                    isinstance(item, UnitResource) and key[2] == qualified_name for key, item in resources.items()
                ):
                    continue
                edges.setdefault(qualified_name, set()).update(
                    stack_generated_unit_name(stack.name, dependency) for dependency in binding.dependsOn
                )
            continue
        for logical_name, projected in stack.spec.structuralProjection.units.items():
            generated_name = stack_generated_unit_name(stack.name, logical_name)
            if not include_missing and not any(
                isinstance(item, UnitResource) and key[2] == generated_name for key, item in resources.items()
            ):
                # A deleting Stack may intentionally omit a finalized child.
                continue
            edges.setdefault(generated_name, set()).update(
                stack_generated_unit_name(stack.name, dependency)
                for dependency in projected.dependsOn
                if include_missing
                or any(
                    isinstance(item, UnitResource) and key[2] == stack_generated_unit_name(stack.name, dependency)
                    for key, item in resources.items()
                )
            )
    return {name: tuple(sorted(dependencies)) for name, dependencies in sorted(edges.items())}


def desired_unit_names(root: Path) -> tuple[str, ...]:
    """Return materialized desired Unit names, including Stack-owned Units."""

    return tuple(sorted(_current_desired_unit_paths(root)))


def ensure_desired_units_materialized(root: Path) -> None:
    for unit_name, path in _current_desired_unit_paths(root).items():
        if raw_unit_contains_reference(load_json(path)):
            raise OperationError(f"{unit_name} desired state is not fully materialized")


parse_artifact_document = RESOURCE_CATALOG.parse_artifact


def validate_receipt_document(document: object, description: str) -> ReceiptResource[Any]:
    return RESOURCE_CATALOG.validate_receipt(document, description)


def validate_document(contract: DocumentContract, document: object, description: str) -> dict[str, Any]:
    try:
        return contract.validate(document)
    except ContractError as exc:
        raise OperationError(f"invalid {description}: {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_preferred_document(path: Path, value: dict[str, Any] | ReceiptResource[Any], project_root: Path) -> Path:
    return RESOURCE_CATALOG.write_preferred(
        path, value if isinstance(value, ReceiptResource) else cast(JsonObject, value), project_root
    )


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def safe_source_path(value: Any, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise OperationError(f"{description} must be a non-empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise OperationError(f"{description} must stay inside its source revision")
    return path


def hash_source_inputs(
    source_root: Path,
    source_path: str,
    inputs: list[str],
    identity: dict[str, Any],
) -> str:
    root = source_root / safe_source_path(source_path, "source path")
    files: dict[str, Path] = {}
    for input_name in inputs:
        relative = safe_source_path(input_name, "source input")
        if globlib.has_magic(input_name):
            try:
                targets = list(root.glob(input_name))
            except (OSError, ValueError) as exc:
                raise OperationError(f"invalid source input pattern: {source_path}/{input_name}: {exc}") from exc
            if not targets:
                raise OperationError(f"source input pattern does not match: {source_path}/{input_name}")
        else:
            targets = [root / relative]
        for target in targets:
            if target.is_symlink() or target.is_file():
                files[target.relative_to(root).as_posix()] = target
            elif target.is_dir():
                for path in target.rglob("*"):
                    if path.is_symlink() or path.is_file():
                        files[path.relative_to(root).as_posix()] = path
            else:
                raise OperationError(f"source input does not exist: {source_path}/{input_name}")

    entries = []
    for name, path in sorted(files.items()):
        if path.is_symlink():
            mode = "120000"
            content = os.readlink(path).encode()
        else:
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            content = path.read_bytes()
        entries.append({"path": name, "mode": mode, "contentHash": hashlib.sha256(content).hexdigest()})
    payload = {"inputHashVersion": 1, **identity, "files": entries}
    return f"sha256:{hashlib.sha256(canonical_json(payload)).hexdigest()}"


def directory_files(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()
    }


def unit_requires_reconciliation(unit: UnitResource[Any]) -> bool:
    return operational.unit_requires_reconciliation(unit)


def materialization_tree_digest(root: Path) -> str:
    return operational.materialization_tree_digest(root)


def validate_unit_materialization(desired_root: Path, unit_name: str, unit: UnitResource[Any]) -> None:
    operational.validate_unit_materialization(desired_root, unit_name, unit)


def copy_unit_materialization(source: Path, destination: Path, unit_name: str, unit: UnitResource[Any]) -> None:
    validate_unit_materialization(source, unit_name, unit)
    descriptor = getattr(unit.spec, "materialization", None)
    relative_path = descriptor.path if descriptor is not None else f"materialized/{unit_name}"
    target = destination / relative_path
    if target.exists():
        shutil.rmtree(target)
    if descriptor is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / relative_path, target)


def require_unit_specification(
    specification: UnitResource[Any], expected_name: str | None = None
) -> tuple[str, AuthoredSource | None]:
    if expected_name is not None and specification.name != expected_name:
        raise OperationError(f"invalid unit specification: {expected_name!r}")
    source = getattr(specification.spec, "source", None)
    if source is not None:
        if not isinstance(source, AuthoredSource):
            raise OperationError(f"{specification.name} has an invalid source")
        safe_source_path(source.path, f"{specification.name} source path")
    return specification.driver_name, source


def unit_input_hash(specification: UnitResource[Any], source_root: Path) -> str | None:
    driver, source = require_unit_specification(specification)
    if source is None:
        return None
    inputs = source.inputs
    if inputs is None:
        source_path = "."
        inputs = [source.path]
    else:
        source_path = source.path
    specification_identity = specification.driver.unit_contract.dump(specification.spec)
    # ``source.revision`` selects the checkout whose bytes are hashed; it is
    # not itself an input identity.  Drop it from the structural portion so
    # two exact revisions with identical selected bytes retain the same
    # inputHash.  This also preserves the legacy hash for direct Units whose
    # authored source omitted revision (the model default is not part of the
    # historical identity).
    if isinstance(specification_identity, dict):
        source_identity = specification_identity.get("source")
        if isinstance(source_identity, dict) and "revision" in source_identity:
            source_identity = dict(source_identity)
            source_identity.pop("revision", None)
            specification_identity = dict(specification_identity)
            specification_identity["source"] = source_identity
    return hash_source_inputs(
        source_root,
        source_path,
        inputs,
        {
            "kind": "unit",
            "driver": driver,
            "driverVersion": DRIVER_VERSIONS[driver],
            "specification": specification_identity,
        },
    )


def commit_is_available(revision: str) -> bool:
    """Return whether Git can resolve ``revision`` as a commit for materialization."""
    return git("cat-file", "-e", f"{revision}^{{commit}}", check=False).returncode == 0


def commit_is_ancestor(previous: str, candidate: str) -> bool:
    """Return whether ``previous`` is reachable from ``candidate``."""
    return git("merge-base", "--is-ancestor", previous, candidate, check=False).returncode == 0


def prior_unit_source(
    unit_name: str,
    current_desired: Path,
) -> tuple[str, str] | None:
    current_path = unit_document_path(current_desired, unit_name)
    if current_path.is_file():
        source = persisted_unit_source_identity(current_path)
        revision = source.revision
        input_hash = source.input_hash
        if isinstance(revision, str) and isinstance(input_hash, str):
            return revision, input_hash
    return None


def file_blob(path: Path) -> str:
    return git("hash-object", str(path)).stdout.strip()


def sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def resource_owner_reference(resource: UnitResource[Any] | StackResource) -> DesiredOwnerReference | None:
    """Return the single Kubernetes-shaped owner reference of a desired resource."""

    references = getattr(resource.metadata, "ownerReferences", None)
    if references is None:
        return None
    if not isinstance(references, (list, tuple)):
        raise OperationError(f"desired resource {resource.name!r} has invalid ownerReferences")
    if len(references) > 1:
        raise OperationError(f"desired resource {resource.name!r} has more than one ownerReference")
    return references[0] if references else None


def _unit_is_stack_owned(unit: UnitResource[Any]) -> bool:
    owner = resource_owner_reference(unit)
    return owner is not None and owner.kind == "Stack"


def _unit_owned_by_stack(unit: UnitResource[Any], name: str, uid: str | None) -> bool:
    owner = resource_owner_reference(unit)
    return owner is not None and owner.kind == "Stack" and owner.name == name and owner.uid == uid


def resource_deletion(resource: UnitResource[Any] | StackResource) -> DeletionMetadata | None:
    return getattr(resource.metadata, "deletion", None)


def _resource_document(resource: UnitResource[Any] | StackResource) -> JsonObject:
    if isinstance(resource, UnitResource):
        return serialize_unit_document(resource, profile="desired")
    return RESOURCE_CATALOG.serialize_stack_resource(resource, profile="desired")


def resource_content_digest(resource: UnitResource[Any] | StackResource) -> str:
    """Hash the retained resource without its deletion marker."""

    document = _resource_document(resource)
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata.pop("deletion", None)
        document = dict(document)
        document["metadata"] = metadata
    return f"sha256:{hashlib.sha256(canonical_json(document)).hexdigest()}"


@dataclass(frozen=True)
class ResourceFinalizationFence:
    api_version: str
    kind: str
    name: str
    uid: str
    deletion_generation: int


def _load_transition_resources(
    root: Path,
) -> tuple[
    dict[tuple[str, str, str], UnitResource[Any] | StackResource],
    dict[str, object],
]:
    """Load canonical resources and retain unparseable Unit payloads separately."""

    resources: dict[tuple[str, str, str], UnitResource[Any] | StackResource] = {}
    opaque_units: dict[str, object] = {}
    for qualified_name, path in _current_desired_unit_paths(root).items():
        try:
            resource = load_desired_unit(path, path.stem)
        except (DocumentFormatError, DriverError, KeyError, TypeError, ValueError, OperationError):
            opaque_units[qualified_name] = opaque_document_payload(path)
            continue
        resources[(resource.gvk.api_version, resource.gvk.kind, qualified_name)] = resource
    for kind in ("StackTemplate", "Stack"):
        for name, path in _current_desired_stack_paths(root, kind).items():
            resource = (
                RESOURCE_CATALOG.parse_stack_template(
                    RESOURCE_CATALOG.load_document(path), profile="desired", expected_name=name
                )
                if kind == "StackTemplate"
                else RESOURCE_CATALOG.parse_stack(
                    RESOURCE_CATALOG.load_document(path), profile="desired", expected_name=name
                )
            )
            key = (resource.gvk.api_version, resource.gvk.kind, resource.name)
            if key in resources:
                raise OperationError(f"duplicate desired resource identity: {key!r}")
            resources[key] = resource
    return resources, opaque_units


def validate_desired_resource_transition(
    current_root: Path,
    candidate_root: Path,
    finalized_resources: frozenset[ResourceFinalizationFence] = frozenset(),
) -> None:
    """Validate terminal deletion transitions between two desired trees."""

    current, raw_opaque_units = _load_transition_resources(current_root)
    candidate = load_desired_resource_graph(candidate_root, validate=False)
    for key, previous in current.items():
        next_resource = candidate.get(key)
        previous_deletion = resource_deletion(previous)
        if next_resource is None:
            if previous_deletion is None or previous.metadata.uid is None:
                raise OperationError(f"desired resource {previous.name!r} cannot be removed before deletion")
            fence = ResourceFinalizationFence(
                previous.gvk.api_version,
                previous.gvk.kind,
                key[2],
                previous.metadata.uid,
                previous_deletion.generation,
            )
            if fence not in finalized_resources:
                raise OperationError(f"desired resource {previous.name!r} can be removed only by finalization")
            continue
        if next_resource.metadata.uid != previous.metadata.uid:
            raise OperationError(f"desired resource {previous.name!r} changed UID without finalization")
        next_deletion = resource_deletion(next_resource)
        if previous_deletion is None:
            if next_deletion is not None:
                digest = resource_content_digest(previous)
                if next_deletion.resourceDigest != digest or resource_content_digest(next_resource) != digest:
                    raise OperationError(f"desired resource {previous.name!r} changed when deletion started")
            continue
        if (
            next_deletion != previous_deletion
            or resource_content_digest(next_resource) != previous_deletion.resourceDigest
        ):
            raise OperationError(f"desired resource {previous.name!r} changed after deletion started")

    current_opaque = load_desired_cleanup_roots(current_root)
    candidate_opaque = load_desired_cleanup_roots(candidate_root)
    for name, payload in raw_opaque_units.items():
        adopted = candidate_opaque.get(name)
        if adopted is None or adopted.payload != payload:
            raise OperationError(f"unparseable desired Unit {name!r} must be retained as an opaque cleanup root")
    for name, previous in current_opaque.items():
        next_opaque = candidate_opaque.get(name)
        next_resource = next((resource for resource in candidate.values() if resource.name == name), None)
        previous_deletion = previous.metadata.deletion
        if next_opaque is not None:
            if next_opaque.metadata.uid != previous.metadata.uid:
                raise OperationError(f"opaque cleanup root {name!r} changed UID")
            next_deletion = next_opaque.metadata.deletion
            if previous_deletion is None:
                if next_deletion is not None:
                    digest = opaque_cleanup_content_digest(previous)
                    if next_deletion.resourceDigest != digest or opaque_cleanup_content_digest(next_opaque) != digest:
                        raise OperationError(f"opaque cleanup root {name!r} changed when deletion started")
            elif (
                next_deletion != previous_deletion
                or opaque_cleanup_content_digest(next_opaque) != previous_deletion.resourceDigest
            ):
                raise OperationError(f"opaque cleanup root {name!r} changed after deletion started")
            continue
        if next_resource is not None:
            if next_resource.metadata.uid != previous.metadata.uid:
                raise OperationError(f"opaque cleanup recovery for {name!r} changed UID")
            next_deletion = resource_deletion(next_resource)
            if previous_deletion is None:
                if next_deletion is not None:
                    raise OperationError(f"opaque cleanup recovery for {name!r} added deletion unexpectedly")
            elif next_deletion is None or next_deletion.generation != previous_deletion.generation:
                raise OperationError(f"opaque cleanup recovery for {name!r} changed deletion generation")
            elif resource_content_digest(next_resource) != next_deletion.resourceDigest:
                raise OperationError(f"opaque cleanup recovery for {name!r} has an invalid deletion digest")
            continue
        if previous_deletion is None or previous.metadata.uid is None:
            raise OperationError(f"opaque cleanup root {name!r} cannot be removed before deletion")
        api_version, kind = opaque_resource_gvk(previous.payload)
        fence = ResourceFinalizationFence(
            api_version,
            kind,
            name,
            previous.metadata.uid,
            previous_deletion.generation,
        )
        if fence not in finalized_resources:
            raise OperationError(f"opaque cleanup root {name!r} can be removed only by finalization")


def mark_resource_for_deletion(
    resource: UnitResource[Any] | StackResource,
    *,
    generation: int | None = None,
) -> UnitResource[Any] | StackResource:
    """Return a retained resource with a deterministic deletion fence."""

    if resource.metadata.uid is None:
        raise OperationError(f"desired resource {resource.name!r} has no UID")
    current = resource_deletion(resource)
    if current is not None:
        return resource
    deletion = DeletionMetadata(
        generation=generation or 1,
        resourceDigest=resource_content_digest(resource),
    )
    return resource.with_metadata(replace(resource.metadata, deletion=deletion))


def deletion_reason(resource: UnitResource[Any] | StackResource) -> str:
    deletion = resource_deletion(resource)
    if deletion is None:
        raise OperationError(f"desired resource {resource.name!r} is not marked for deletion")
    return f"deletion pending finalization (UID {resource.metadata.uid}, generation {deletion.generation})"


def artifact_document_path(root: Path, unit_name: str, artifact_name: str) -> Path:
    directory = root / "artifacts" / PurePosixPath(unit_name)
    candidates = document_candidates(directory, artifact_name)
    if len(candidates) > 1:
        raise OperationError(f"multiple Artifact document formats exist for {unit_name}/{artifact_name}")
    if candidates:
        return candidates[0]
    if not resource_documents_enabled(REPOSITORY_ROOT):
        return directory / f"{artifact_name}.json"
    project = load_project_config(REPOSITORY_ROOT)
    return RESOURCE_REGISTRY.document_path(
        family="artifact",
        plane=ResourcePlane.OBSERVED,
        root=root,
        repository_root=REPOSITORY_ROOT,
        project=project,
        environment=None,
        qualified_name=f"{unit_name}/{artifact_name}",
        suffix=project.write_format.suffix,
    )


def write_artifact_documents(
    observed: Path,
    unit_name: str,
    driver_name: str,
    documents: Mapping[str, JsonObject],
) -> dict[str, dict[str, str]]:
    driver = UNIT_DRIVERS[driver_name]
    expected = set(driver.artifact_outputs)
    if set(documents) != expected:
        raise DriverError(f"{driver_name} returned artifact documents {sorted(documents)}; expected {sorted(expected)}")
    target = (artifact_document_path(observed, unit_name, "placeholder")).parent
    if target.exists():
        shutil.rmtree(target)
    if not documents:
        return {}
    selected = load_project_config(REPOSITORY_ROOT).write_format
    descriptors: dict[str, dict[str, str]] = {}
    for name, document in documents.items():
        artifact_kind = driver.artifact_outputs[name]
        artifact_api = require_artifact_api(artifact_kind)
        resource = parse_artifact_document(artifact_api, document, f"{driver_name} artifact {name}")
        schema_id = str(artifact_api.json_schema()["$id"])
        serialized = {"$schema": schema_id, **artifact_api.dump(resource)}
        path = write_document(artifact_document_path(observed, unit_name, name), serialized, format=selected)
        descriptors[name] = {
            "apiVersion": artifact_kind.gvk.api_version,
            "kind": artifact_kind.gvk.kind,
            "path": path.relative_to(observed).as_posix(),
            "digest": sha256_file(path),
            "mediaType": f"{artifact_api.media_type}+{selected.value}",
        }
    return descriptors


def validate_artifact_output_identity(
    driver_name: str,
    unit: UnitResource[Any],
    documents: Mapping[str, JsonObject],
    qualified_name: str | None = None,
) -> None:
    driver = UNIT_DRIVERS[driver_name]
    if set(documents) != set(driver.artifact_outputs):
        raise DriverError(
            f"{driver_name} returned artifact documents {sorted(documents)}; expected {sorted(driver.artifact_outputs)}"
        )
    if not documents:
        return
    source = getattr(unit.spec, "source", None)
    if not isinstance(source, DesiredSource):
        raise DriverError(f"{driver_name} desired unit has no source identity")
    for name, document in documents.items():
        artifact_api = require_artifact_api(driver.artifact_outputs[name])
        parse_artifact_document(artifact_api, document, f"{driver_name} artifact {name}")
        metadata = document.get("metadata")
        producer = document.get("producer")
        if not isinstance(metadata, dict) or metadata.get("name") != name:
            raise DriverError(f"{driver_name} artifact {name!r} has the wrong resource identity")
        if (
            not isinstance(producer, dict)
            or producer.get("apiVersion") != driver.api_version
            or producer.get("kind") != driver.kind
            or producer.get("name") != unit.name
            or producer.get("qualifiedName") != (qualified_name or unit.name)
            or producer.get("driverVersion") != driver.version
            or producer.get("sourceRevision") != source.revision
            or producer.get("inputHashVersion") != 1
            or producer.get("inputHash") != source.inputHash
        ):
            raise DriverError(f"{driver_name} artifact {name!r} has the wrong producer identity")


def load_artifact_document(
    observed: Path,
    unit: UnitResource[Any],
    receipt: ReceiptResource[Any],
    artifact_name: str,
    *,
    require_current_producer: bool = True,
) -> tuple[dict[str, Any], str]:
    driver_name = receipt.driver_name
    artifact_kind = UNIT_DRIVERS[driver_name].artifact_outputs.get(artifact_name)
    if artifact_kind is None:
        raise ReferenceUnavailable(f"unit does not produce artifact {artifact_name!r}")
    artifact_api = require_artifact_api(artifact_kind)
    descriptor = receipt.status.artifacts.get(artifact_name) if receipt.status.artifacts is not None else None
    if descriptor is None:
        raise ReferenceUnavailable(f"receipt does not describe artifact {artifact_name!r}")
    expected_path = artifact_document_path(observed, receipt.spec.subject.qualifiedName, artifact_name)
    recorded_path = descriptor.path
    if (
        not isinstance(recorded_path, str)
        or PurePosixPath(recorded_path).is_absolute()
        or ".." in PurePosixPath(recorded_path).parts
    ):
        raise ReferenceUnavailable(f"artifact {artifact_name!r} has an unsafe path")
    path = observed / recorded_path
    if path != expected_path or not path.is_file():
        raise ReferenceUnavailable(f"artifact {artifact_name!r} does not exist at its required path")
    if descriptor.apiVersion != artifact_kind.gvk.api_version or descriptor.kind != artifact_kind.gvk.kind:
        raise ReferenceUnavailable(f"artifact {artifact_name!r} has the wrong contract identity")
    expected_media_type = f"{artifact_api.media_type}+{'json' if path.suffix == '.json' else 'yaml'}"
    if descriptor.mediaType != expected_media_type:
        raise ReferenceUnavailable(f"artifact {artifact_name!r} has the wrong media type")
    digest = descriptor.digest
    if sha256_file(path) != digest:
        raise ReferenceUnavailable(f"artifact {artifact_name!r} does not match its digest")
    document = load_document(path)
    if not isinstance(document, dict):
        raise ReferenceUnavailable(f"artifact {artifact_name!r} is not an object")
    typed_resource = parse_artifact_document(
        artifact_api,
        document,
        f"persisted {driver_name} artifact {artifact_name}",
    )
    document = artifact_api.dump(typed_resource)
    if not require_current_producer:
        return document, digest
    producer = document.get("producer")
    metadata = document.get("metadata")
    source = getattr(unit.spec, "source", None)
    if not isinstance(metadata, dict) or metadata.get("name") != artifact_name:
        raise ReferenceUnavailable(f"artifact {artifact_name!r} has the wrong resource identity")
    if (
        not isinstance(producer, dict)
        or not isinstance(source, DesiredSource)
        or (
            producer.get("apiVersion") != UNIT_DRIVERS[driver_name].api_version
            or producer.get("kind") != UNIT_DRIVERS[driver_name].kind
            or producer.get("name") != unit.name
            or producer.get("qualifiedName") != receipt.spec.subject.qualifiedName
            or producer.get("driverVersion") != UNIT_DRIVERS[driver_name].version
            or producer.get("sourceRevision") != source.revision
            or producer.get("inputHash") != source.inputHash
        )
    ):
        raise ReferenceUnavailable(f"artifact {artifact_name!r} has stale producer identity")
    return document, digest


def validate_receipt_artifacts(
    observed: Path,
    unit: UnitResource[Any],
    receipt: ReceiptResource[Any],
) -> None:
    driver_name = unit.driver_name
    expected = set(UNIT_DRIVERS[driver_name].artifact_outputs)
    if receipt.driver_name != driver_name:
        raise OperationError(f"persisted receipt driver is not {driver_name!r}")
    descriptors = receipt.status.artifacts or {}
    if set(descriptors) != expected:
        raise OperationError(
            f"persisted {driver_name} receipt describes artifacts {sorted(descriptors)}; expected {sorted(expected)}"
        )
    directory = observed / "artifacts" / receipt.spec.subject.qualifiedName
    actual_paths = {path for path in directory.rglob("*") if path.is_file()} if directory.is_dir() else set()
    expected_paths = {artifact_document_path(observed, receipt.spec.subject.qualifiedName, name) for name in expected}
    if actual_paths != expected_paths:
        raise OperationError(f"persisted {driver_name} artifact files do not match its complete contract set")
    for artifact_name in expected:
        load_artifact_document(observed, unit, receipt, artifact_name)


def current_receipt(observed: Path, candidate_units: Path, unit_name: str) -> ReceiptResource[Any] | None:
    receipt_path = receipt_document_path(observed, unit_name)
    unit_path = unit_document_path(candidate_units.parent, unit_name)
    if not receipt_path.is_file() or not unit_path.is_file():
        return None
    receipt = load_receipt(receipt_path, unit_name)
    if receipt.spec.desired.unitBlob != file_blob(unit_path):
        return None
    validate_receipt_artifacts(observed, load_desired_unit(unit_path, unit_name), receipt)
    return receipt


def parse_artifact_reference(reference: object) -> ArtifactReferenceTarget:
    if not isinstance(reference, dict):
        raise OperationError("invalid fromArtifact reference")
    unit_name = reference.get("unit")
    artifact_name = reference.get("name")
    api_version = reference.get("apiVersion")
    kind = reference.get("kind")
    if not isinstance(unit_name, str) or not re.fullmatch(QUALIFIED_RESOURCE_NAME_PATTERN, unit_name):
        raise OperationError(f"invalid fromArtifact unit: {unit_name!r}")
    if not isinstance(artifact_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", artifact_name):
        raise OperationError(f"invalid fromArtifact name: {artifact_name!r}")
    if not isinstance(api_version, str) or not isinstance(kind, str):
        raise OperationError("fromArtifact requires string apiVersion and kind")
    try:
        gvk = GVK(api_version, kind)
    except ValueError as exc:
        raise OperationError(str(exc)) from exc
    target = ArtifactReferenceTarget(
        unit=unit_name,
        name=artifact_name,
        apiVersion=gvk.api_version,
        kind=gvk.kind,
    )
    validate_artifact_reference_target(target)
    return target


def validate_artifact_reference_target(reference: ArtifactReferenceTarget) -> None:
    """Require an artifact reference to target an installed artifact API."""

    api_kind = API_KINDS.get(reference.gvk)
    if api_kind is None:
        raise OperationError(f"fromArtifact references an unregistered API kind: {reference.gvk}")
    try:
        require_artifact_api(api_kind)
    except ApiError as exc:
        raise OperationError(f"fromArtifact API kind is not an artifact resource: {reference.gvk}") from exc


def resolve_template(
    value: object,
    candidate: Path,
    observed: Path,
    observed_revision: str | None,
    promotion: PromotionContext | None = None,
    target_unit: str | None = None,
    target_gvk: GVK | None = None,
    pointer: str = "",
    dry: bool = False,
    environment_document: Mapping[str, Any] | None = None,
    artifact_imports: Sequence[ArtifactImport] = (),
    target_stack_uid: str | None = None,
) -> TemplateResolution:
    def resolve_promotion(reference: PromotionReferenceSelection) -> FingerprintedValue:
        if promotion is None:
            raise ReferenceUnavailable(f"promotion context does not exist for source unit {reference.unit!r}")
        source = f"{promotion.source_environment} ({promotion.desired_ref}@{promotion.desired_revision[:12]})"
        target = target_unit or "<unknown>"
        path = unit_document_path(promotion.desired_root, reference.unit)
        if not path.is_file():
            raise OperationError(
                f"promotion from {source} for target unit {target!r} does not contain source unit {reference.unit!r}"
            )
        unit = load_desired_unit(path, reference.unit)
        if reference.pointer_inferred:
            if target_gvk is None:
                raise OperationError(
                    f"implicit fromPromotion at {reference.pointer!r} for target unit {target!r} "
                    "requires the target GVK"
                )
            if unit.gvk != target_gvk:
                raise OperationError(
                    f"implicit fromPromotion at {reference.pointer!r} for target unit {target!r} "
                    f"requires matching GVKs; source unit {reference.unit!r} is {unit.gvk}, "
                    f"target is {target_gvk}; set pointer explicitly to allow a cross-GVK reference"
                )
        document = unit.driver.desired_unit_contract.dump(unit.spec)
        try:
            resolved = json_pointer(document, reference.pointer)
        except OperationError as exc:
            raise OperationError(
                f"promotion from {source} source unit {reference.unit!r} cannot resolve pointer "
                f"{reference.pointer!r} for target unit {target!r}: {exc}"
            ) from exc
        return FingerprintedValue(
            resolved,
            file_blob(path),
        )

    def resolve_receipt(reference):
        if observed_revision is None:
            raise ReferenceUnavailable(f"receipt does not exist: {reference.unit}")
        receipt = current_receipt(observed, candidate / "units", reference.unit)
        if receipt is None:
            raise ReferenceUnavailable(f"receipt is stale: {reference.unit}")
        document = receipt.driver.result_contract.dump(receipt.status.result)
        return FingerprintedValue(
            json_pointer(document, reference.pointer), file_blob(receipt_document_path(observed, reference.unit))
        )

    def resolve_artifact(reference):
        if observed_revision is None and not artifact_imports:
            raise ReferenceUnavailable(f"receipt does not exist: {reference.unit}")
        validate_artifact_reference_target(reference)
        producer_path = unit_document_path(candidate, reference.unit)
        if not producer_path.is_file():
            matches = [
                item
                for item in artifact_imports
                if item.name == reference.name
                and item.apiVersion == reference.apiVersion
                and item.kind == reference.kind
                and (item.unit == reference.unit or reference.unit.endswith(f"--{item.unit}"))
            ]
            if len(matches) > 1:
                raise OperationError(f"artifact import is ambiguous for producer {reference.unit}")
            imported = matches[0] if matches else None
            if imported is None or promotion is None:
                raise ReferenceUnavailable(f"artifact producer is not selected: {reference.unit}")
            source_stack_path = document_candidates(promotion.desired_root / "stacks", imported.fromPromotion.stack)
            if len(source_stack_path) != 1:
                raise ReferenceUnavailable(f"promoted source Stack is unavailable: {imported.fromPromotion.stack}")
            source_stack = RESOURCE_CATALOG.parse_stack(
                RESOURCE_CATALOG.load_document(source_stack_path[0]),
                profile="desired",
                expected_name=imported.fromPromotion.stack,
            )
            source_unit_qualified_name = stack_generated_unit_name(imported.fromPromotion.stack, imported.unit)
            source_unit_path = unit_document_path(promotion.desired_root, source_unit_qualified_name)
            if not source_unit_path.is_file():
                source_unit_qualified_name = reference.unit
                source_unit_path = unit_document_path(promotion.desired_root, reference.unit)
            if not source_unit_path.is_file():
                chained = None
                if isinstance(source_stack.spec, DesiredStackSpec) and source_stack.spec.resolvedArtifactImports:
                    chained = next(
                        (
                            evidence
                            for key, evidence in source_stack.spec.resolvedArtifactImports.items()
                            if key == f"{imported.unit}/{imported.name}"
                            or key.endswith(f"--{imported.unit}/{imported.name}")
                        ),
                        None,
                    )
                if chained is not None:
                    if (
                        chained.artifactName != reference.name
                        or chained.apiVersion != reference.apiVersion
                        or chained.kind != reference.kind
                    ):
                        raise ReferenceUnavailable("promoted artifact evidence does not match the requested artifact")
                    artifact_kind = API_KINDS.get(reference.gvk)
                    if artifact_kind is None:
                        raise ReferenceUnavailable("promoted chained artifact API is not installed")
                    artifact_api = require_artifact_api(artifact_kind)
                    parse_artifact_document(
                        artifact_api,
                        cast(JsonObject, chained.artifactDocument),
                        f"promoted chained artifact {reference.name}",
                    )
                    if target_stack_uid is None:
                        raise ReferenceUnavailable("promoted artifact target has no Stack UID")
                    chained = replace(chained, targetStackUid=target_stack_uid)
                    return FingerprintedValue(
                        json_pointer(chained.artifactDocument, reference.pointer),
                        chained.artifactDigest,
                        imported=True,
                        evidence=cast(JsonObject, chained.to_dict()),
                    )
                if promotion.observed_root is None:
                    raise ReferenceUnavailable("promoted artifact observed state is unavailable")
                raise ReferenceUnavailable(f"promoted source Unit is unavailable: {reference.unit}")
            source_unit = load_desired_unit(source_unit_path, source_unit_path.stem)
            source_owner = resource_owner_reference(source_unit)
            source_uid = source_stack.metadata.uid
            if source_owner is None or source_uid is None or source_owner.uid != source_uid:
                raise ReferenceUnavailable("promoted artifact producer has an invalid Stack owner fence")
            if promotion.observed_root is None:
                raise ReferenceUnavailable("promoted artifact observed state is unavailable")
            source_receipt = current_receipt(
                promotion.observed_root,
                promotion.desired_root / "units",
                source_unit_qualified_name,
            )
            if source_receipt is None:
                raise ReferenceUnavailable(f"promoted artifact receipt is stale: {source_unit.name}")
            document, digest = load_artifact_document(
                promotion.observed_root, source_unit, source_receipt, reference.name
            )
            if (
                source_stack.metadata.uid is None
                or source_unit.metadata.uid is None
                or promotion.observed_revision is None
            ):
                raise ReferenceUnavailable("promoted artifact producer has no UID identity")
            if target_stack_uid is None:
                raise ReferenceUnavailable("promoted artifact target has no Stack UID")
            evidence = cast(
                JsonObject,
                ResolvedArtifactImport(
                    sourceStack=source_stack.name,
                    sourceStackUid=source_stack.metadata.uid,
                    sourceUnit=source_unit.name,
                    sourceUnitUid=source_unit.metadata.uid,
                    sourceDesiredRevision=promotion.desired_revision,
                    sourceObservedRevision=promotion.observed_revision,
                    receiptUnitBlob=source_receipt.spec.desired.unitBlob,
                    artifactName=reference.name,
                    apiVersion=reference.apiVersion,
                    kind=reference.kind,
                    artifactDigest=digest,
                    targetStackUid=target_stack_uid,
                    artifactDocument=JsonObjectValue(document),
                ).to_dict(),
            )
            return FingerprintedValue(
                json_pointer(document, reference.pointer),
                digest,
                imported=True,
                evidence=evidence,
            )
        receipt = current_receipt(observed, candidate / "units", reference.unit)
        if receipt is None:
            raise ReferenceUnavailable(f"receipt is stale: {reference.unit}")
        producer_unit = load_desired_unit(producer_path, reference.unit)
        producer_driver = producer_unit.driver
        artifact_kind = producer_driver.artifact_outputs.get(reference.name) if producer_driver is not None else None
        if artifact_kind is None:
            raise ReferenceUnavailable(f"unit {reference.unit!r} does not produce artifact {reference.name!r}")
        if artifact_kind.gvk != reference.gvk:
            raise ReferenceUnavailable(
                f"artifact {reference.unit}/{reference.name} is {artifact_kind.gvk}, not {reference.gvk}"
            )
        document, digest = load_artifact_document(observed, producer_unit, receipt, reference.name)
        return FingerprintedValue(json_pointer(document, reference.pointer), digest)

    def resolve_environment(pointer: str) -> FingerprintedValue:
        if environment_document is None:
            raise ReferenceUnavailable("Environment resource is unavailable")
        return FingerprintedValue(
            json_pointer(dict(environment_document), pointer),
            hashlib.sha256(canonical_json(environment_document)).hexdigest(),
        )

    return resolve_template_value(
        value,
        ResolutionContext(
            receipt=resolve_receipt,
            artifact=resolve_artifact,
            promotion=resolve_promotion,
            environment=resolve_environment,
            unit=target_unit,
            dry=dry,
        ),
        pointer,
    )


class SourceResolutionDisposition(StrEnum):
    """The source identity decision made while preparing one desired Unit."""

    UNCHANGED = "unchanged"
    INPUTS_CHANGED = "inputs-changed"
    REVISION_REFRESHED = "revision-refreshed"


@dataclass(frozen=True)
class ResolvedUnitSourceResult:
    """Resolved source plus the explicit input and provenance disposition."""

    source: DesiredSource | None
    refresh_reason: str | None = None
    disposition: SourceResolutionDisposition = SourceResolutionDisposition.UNCHANGED


class SourceRevisionUnavailableError(OperationError):
    """A retained source revision is unavailable under the selected project policy."""

    def __init__(self, unit_name: str, revision: str, operation: Literal["apply", "plan"]) -> None:
        self.unit_name = unit_name
        self.revision = revision
        self.operation = operation
        super().__init__(f"{unit_name} desired source {revision} is unavailable under project policy")


def resolved_unit_source(
    specification: UnitResource[Any],
    source_root: Path,
    source_revision: str | None,
    current_desired: Path,
    source_revision_policy: SourceRevisionPolicy | None = None,
    source_revision_operation: Literal["apply", "plan"] = "apply",
    preserve_prior_revision: bool = True,
) -> ResolvedUnitSourceResult:
    source_revision_policy = source_revision_policy or SourceRevisionPolicy()
    driver, source = require_unit_specification(specification)
    if source is None:
        return ResolvedUnitSourceResult(source=None, disposition=SourceResolutionDisposition.UNCHANGED)
    requested_revision = source.revision or source_revision
    if requested_revision is None:
        raise OperationError(
            f"Unit {specification.name!r} uses repository-backed source; apply it with --source-revision <commit>"
        )
    input_hash = unit_input_hash(specification, source_root)
    revision = requested_revision
    prior = prior_unit_source(specification.name, current_desired)
    disposition = SourceResolutionDisposition.INPUTS_CHANGED if prior is None else SourceResolutionDisposition.UNCHANGED
    refresh_reason: str | None = None
    if prior is not None and preserve_prior_revision and source.revision is None:
        prior_revision, prior_hash = prior
        previous_unit_path = unit_document_path(current_desired, specification.name)
        if (
            specification.driver_name == "oci-images"
            and getattr(specification.spec, "environment", None) is None
            and previous_unit_path.is_file()
        ):
            previous_environment = persisted_unit_source_identity(previous_unit_path).environment
            if isinstance(previous_environment, str):
                input_hash = prior_hash
        prior_available = commit_is_available(prior_revision)
        if prior_available and not prior_hash:
            with tempfile.TemporaryDirectory() as prior_directory:
                prior_root = Path(prior_directory) / "source"
                materialize_revision(prior_revision, prior_root)
                prior_hash = unit_input_hash(specification, prior_root)
        if prior_hash == input_hash:
            in_candidate_history = prior_available and (
                source_revision_policy.unavailable_when is SourceRevisionUnavailableWhen.MISSING
                or commit_is_ancestor(prior_revision, requested_revision)
            )
            if in_candidate_history:
                revision = prior_revision
            else:
                action = (
                    source_revision_policy.when_unavailable_during_plan
                    if source_revision_operation == "plan"
                    else source_revision_policy.when_unavailable_during_apply
                )
                if action is SourceRevisionAction.ERROR:
                    raise SourceRevisionUnavailableError(specification.name, prior_revision, source_revision_operation)
                disposition = SourceResolutionDisposition.REVISION_REFRESHED
                unavailable_reason = "is outside candidate history" if prior_available else "is unavailable"
                dry_suffix = " in the dry candidate only" if source_revision_operation == "plan" else ""
                refresh_reason = (
                    f"retained source {describe_revision(prior_revision)} {unavailable_reason}; "
                    f"use {describe_revision(source_revision)}{dry_suffix}"
                )
        else:
            disposition = SourceResolutionDisposition.INPUTS_CHANGED
    return ResolvedUnitSourceResult(
        source=DesiredSource(
            path=source.path,
            inputs=source.inputs,
            revision=revision,
            inputHash=input_hash,
            driverVersion=DRIVER_VERSIONS[driver],
        ),
        disposition=disposition,
        refresh_reason=refresh_reason,
    )


def load_environment(source_root: Path, environment_name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", environment_name):
        raise OperationError(f"invalid environment name: {environment_name!r}")
    try:
        environment_root = project_environment_root(source_root, environment_name)
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc
    environment_paths = document_candidates(environment_root, "environment")
    if len(environment_paths) != 1:
        raise OperationError(f"expected exactly one environment document for {environment_name}")
    environment_document = load_json(environment_paths[0])
    environment = normalize_environment_document(environment_document, environment_name)
    if environment.get("name") != environment_name:
        raise OperationError(f"invalid environment specification: {environment_name}")
    change_gate = environment.get("changeGate", "none")
    if change_gate not in {"none", "pullRequest"}:
        raise OperationError(f"{environment_name} changeGate must be 'none' or 'pullRequest'")
    promotion = environment.get("promotion")
    if promotion is not None:
        if not isinstance(promotion, dict) or set(promotion) != {"allowedSources"}:
            raise OperationError(f"{environment_name} promotion must contain allowedSources only")
        allowed_sources = promotion.get("allowedSources")
        if (
            not isinstance(allowed_sources, list)
            or not allowed_sources
            or not all(
                isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) for value in allowed_sources
            )
            or len(set(allowed_sources)) != len(allowed_sources)
        ):
            raise OperationError(f"{environment_name} promotion allowedSources must be unique environment names")
    promotion_policy = environment.get("promotionPolicy")
    if promotion_policy is not None and (
        not isinstance(promotion_policy, dict)
        or set(promotion_policy) != {"minimumEvidence"}
        or promotion_policy.get("minimumEvidence") not in {"reconciled", "materialized"}
    ):
        raise OperationError(
            f"{environment_name} promotionPolicy must contain minimumEvidence 'reconciled' or 'materialized'"
        )
    validate_document(CORE_CONTRACTS["environment"], environment, f"environment specification {environment_name}")
    return environment


_CONTEXT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _projection_context_digest(context: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in context.items() if key != "digest"}
    return f"sha256:{hashlib.sha256(canonical_json(payload)).hexdigest()}"


def _safe_context_basename(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise OperationError(f"projection context has an invalid {field_name}")
    if Path(value).is_absolute() or "/" in value or "\\" in value or ".." in PurePosixPath(value).parts:
        raise OperationError(f"projection context {field_name} must be a safe basename")
    if Path(value).name != value:
        raise OperationError(f"projection context {field_name} must be a safe basename")
    return value


def _projection_context_path(root: Path, digest: str) -> Path:
    if not _CONTEXT_DIGEST_RE.fullmatch(digest):
        raise OperationError("projection context digest is invalid")
    return root / DESIRED_PROJECTION_CONTEXTS_PATH / f"{digest.removeprefix('sha256:')}.json"


def capture_projection_context(
    source_root: Path,
    environment_name: str,
    promotion: PromotionContext | None = None,
) -> JsonObject:
    """Capture an immutable Project/Environment resolution context."""

    project_path = project_config_path(source_root)
    environment_root = project_environment_root(source_root, environment_name)
    environment_paths = document_candidates(environment_root, "environment")
    if len(environment_paths) != 1:
        raise OperationError(f"expected exactly one environment document for {environment_name}")
    project_document = load_document(project_path)
    validate_project_document(project_document, project_path)
    environment_document = load_document(environment_paths[0])
    normalize_environment_document(environment_document, environment_name)
    context: JsonObject = {
        "schema": 1,
        "kind": "ProjectionContext",
        "environment": environment_name,
        "projectFile": project_path.name,
        "environmentFile": environment_paths[0].name,
        "projectDocument": project_document,
        "environmentDocument": environment_document,
        "projectBytes": base64.b64encode(project_path.read_bytes()).decode("ascii"),
        "environmentBytes": base64.b64encode(environment_paths[0].read_bytes()).decode("ascii"),
    }
    if promotion is not None:
        context["promotionDocument"] = RESOURCE_CATALOG.serialize_promotion(promotion.document())
    context["digest"] = _projection_context_digest(context)
    return context


def _validate_projection_context(
    context: object,
    *,
    expected_digest: str | None = None,
    environment_name: str | None = None,
) -> JsonObject:
    if not isinstance(context, dict):
        raise OperationError("invalid durable projection context")
    required = {
        "schema",
        "kind",
        "digest",
        "environment",
        "projectFile",
        "environmentFile",
        "projectDocument",
        "environmentDocument",
        "projectBytes",
        "environmentBytes",
    }
    allowed = required | {"promotionDocument"}
    if (
        set(context) not in (required, allowed)
        or context.get("schema") != 1
        or context.get("kind") != "ProjectionContext"
    ):
        raise OperationError("invalid durable projection context")
    digest = context.get("digest")
    if not isinstance(digest, str) or not _CONTEXT_DIGEST_RE.fullmatch(digest):
        raise OperationError("projection context has an invalid digest")
    if expected_digest is not None and digest != expected_digest:
        raise OperationError("projection context digest binding does not match its record")
    if _projection_context_digest(cast(Mapping[str, Any], context)) != digest:
        raise OperationError("projection context content does not match its digest")
    environment = context.get("environment")
    if not isinstance(environment, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", environment):
        raise OperationError("projection context has an invalid environment")
    if environment_name is not None and environment != environment_name:
        raise OperationError("projection context targets a different environment")
    project_file = _safe_context_basename(context.get("projectFile"), "projectFile")
    environment_file = _safe_context_basename(context.get("environmentFile"), "environmentFile")
    if project_file not in PROJECT_CONFIG_NAMES:
        raise OperationError("projection context projectFile is not an allowed project basename")
    if environment_file not in {"environment.yaml", "environment.yml", "environment.json"}:
        raise OperationError("projection context environmentFile is not an allowed environment basename")
    for field_name in ("projectDocument", "environmentDocument"):
        if not isinstance(context.get(field_name), dict):
            raise OperationError(f"projection context has an invalid {field_name}")
    decoded: dict[str, bytes] = {}
    for field_name in ("projectBytes", "environmentBytes"):
        value = context.get(field_name)
        if not isinstance(value, str):
            raise OperationError(f"projection context has an invalid {field_name}")
        try:
            decoded[field_name] = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise OperationError(f"projection context has invalid {field_name}") from exc
    # Parse and validate the bytes in a private fixed layout before any caller
    # can use a persisted filename to write into a source tree.
    with tempfile.TemporaryDirectory(prefix="gitopsctr-context-verify-") as directory:
        verify_root = Path(directory)
        project_path = verify_root / project_file
        environment_path = verify_root / environment_file
        project_path.write_bytes(decoded["projectBytes"])
        environment_path.write_bytes(decoded["environmentBytes"])
        try:
            project_document = load_document(project_path)
            validate_project_document(project_document, project_path)
            parsed_environment = load_document(environment_path)
            normalize_environment_document(parsed_environment, environment)
        except (DocumentFormatError, OperationError, OSError) as exc:
            raise OperationError("projection context document bytes are invalid") from exc
    if project_document != context["projectDocument"] or parsed_environment != context["environmentDocument"]:
        raise OperationError("projection context bytes do not match stored documents")
    if "promotionDocument" in context:
        promotion_document = context["promotionDocument"]
        if not isinstance(promotion_document, dict):
            raise OperationError("projection context has an invalid promotionDocument")
        validate_document(
            CORE_CONTRACTS["promotion"],
            normalize_promotion_document(promotion_document),
            "projection context promotion",
        )
    return cast(JsonObject, dict(context))


def load_projection_context(
    root: Path,
    digest: str,
    environment_name: str | None = None,
) -> JsonObject:
    path = _projection_context_path(root, digest)
    if not path.is_file():
        raise OperationError(f"projection context record {digest!r} is missing")
    try:
        document = load_document(path)
    except (DocumentFormatError, OSError) as exc:
        raise OperationError(f"invalid durable projection context: {exc}") from exc
    return _validate_projection_context(document, expected_digest=digest, environment_name=environment_name)


def write_projection_context(root: Path, context: JsonObject) -> Path:
    validated = _validate_projection_context(context)
    digest = cast(str, validated["digest"])
    path = _projection_context_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_document(path, validated, format=DocumentFormat.JSON)


def copy_projection_context(current: Path, candidate: Path) -> None:
    source = current / DESIRED_PROJECTION_CONTEXTS_PATH
    if source.is_dir():
        target = candidate / DESIRED_PROJECTION_CONTEXTS_PATH
        shutil.copytree(source, target, dirs_exist_ok=True)


def change_gate(source_root: Path, environment_name: str) -> str:
    return str(load_environment(source_root, environment_name).get("changeGate", "none"))


def allowed_promotion_sources(source_root: Path, environment_name: str) -> set[str]:
    environment = load_environment(source_root, environment_name)
    promotion = environment.get("promotion")
    return set(promotion["allowedSources"]) if promotion is not None else set()


def minimum_promotion_evidence(source_root: Path, environment_name: str) -> str:
    policy = load_environment(source_root, environment_name).get("promotionPolicy")
    return str(policy["minimumEvidence"]) if policy is not None else "reconciled"


def deployment_refs(
    source_root: Path,
    environment_name: str,
    desired_override: str | None = None,
    observed_override: str | None = None,
) -> tuple[str, str]:
    environment = load_environment(source_root, environment_name)
    configured = environment.get("refs", {})
    if not isinstance(configured, dict) or set(configured) - {"desired", "observed", "candidate"}:
        raise OperationError(f"{environment_name} refs must contain desired, observed, and candidate only")
    project_refs = load_project_config(source_root).environment_defaults.refs
    desired_ref = (
        desired_override or configured.get("desired") or project_refs.desired.replace("{environment}", environment_name)
    )
    observed_ref = (
        observed_override
        or configured.get("observed")
        or project_refs.observed.replace("{environment}", environment_name)
    )
    if not all(isinstance(ref, str) and ref for ref in (desired_ref, observed_ref)):
        raise OperationError(f"{environment_name} desired and observed refs must be strings")
    return canonical_deployment_refs(environment_name, desired_ref, observed_ref)


def canonical_deployment_refs(environment_name: str, desired_ref: str, observed_ref: str) -> tuple[str, str]:
    """Validate and canonicalize public desired and observed deployment refs."""

    canonical_desired = canonical_publication_ref(desired_ref)
    canonical_observed = canonical_publication_ref(observed_ref)
    if canonical_desired == canonical_observed:
        raise OperationError(f"{environment_name} desired and observed refs must differ")
    return canonical_desired, canonical_observed


def canonical_deployment_ref_overrides(
    environment_name: str,
    desired_ref: str | None,
    observed_ref: str | None,
) -> tuple[str | None, str | None]:
    """Canonicalize supplied public ref overrides before controller work begins."""

    canonical_desired = canonical_publication_ref(desired_ref) if desired_ref is not None else None
    canonical_observed = canonical_publication_ref(observed_ref) if observed_ref is not None else None
    if canonical_desired is not None and canonical_observed is not None and canonical_desired == canonical_observed:
        raise OperationError(f"{environment_name} desired and observed refs must differ")
    return canonical_desired, canonical_observed


def effect_lease_ref(
    environment: str,
    desired_ref: str,
    configuration_root: Path | None = None,
) -> str | None:
    """Resolve the configured lease store for one environment.

    ``None`` disables leases. Low-level lease helpers keep their historical
    co-located behavior when called without this resolved value.
    """

    try:
        store = load_project_config(configuration_root or REPOSITORY_ROOT).effect_lease_store
    except DocumentFormatError:
        # Unit-level callers can exercise the lease primitives without a full
        # repository. Real controller entry points validate Project first.
        return desired_ref
    if store is None:
        return None
    ref = store.ref.replace("{environment}", environment)
    if "{unit}" in ref:
        raise OperationError("effect lease store refs with {unit} are not supported yet")
    if ref.startswith("refs/heads/"):
        ref = ref.removeprefix("refs/heads/")
    if not ref:
        raise OperationError("effect lease store branch ref must not be empty")
    return ref


def candidate_ref_template(source_root: Path, environment_name: str) -> str:
    environment = load_environment(source_root, environment_name)
    configured = environment.get("refs", {})
    if not isinstance(configured, dict) or set(configured) - {"desired", "observed", "candidate"}:
        raise OperationError(f"{environment_name} refs must contain desired, observed, and candidate only")
    template = configured.get("candidate") or load_project_config(source_root).environment_defaults.refs.candidate
    if not isinstance(template, str):
        raise OperationError(f"{environment_name} candidate ref template must be a string")
    return template


def candidate_identifier(
    operation: Literal[
        "promotion",
        "rollback",
        "deletion-progression",
        "apply",
        "delete",
        "resolve-opaque-unit",
    ],
    environment_name: str,
    candidate: Path,
    target_ref: str,
    target_revision: str,
    context: Mapping[str, Any],
) -> str:
    files = [
        {"path": path, "contentHash": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(directory_files(candidate).items())
    ]
    payload = {
        "candidateIdVersion": 1,
        "operation": operation,
        "environment": environment_name,
        "targetRef": target_ref,
        "targetRevision": target_revision,
        "context": dict(context),
        "files": files,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()[:12]


def candidate_ref_conflicts(candidate_ref: str, *deployment_refs: str) -> bool:
    """Compare a candidate ref with deployment refs using public short spellings."""

    canonical_candidate = canonical_publication_ref(candidate_ref)
    return canonical_candidate in {canonical_publication_ref(ref) for ref in deployment_refs}


def resolve_candidate_ref(
    source_root: Path,
    environment_name: str,
    operation: Literal[
        "promotion",
        "rollback",
        "deletion-progression",
        "apply",
        "delete",
        "resolve-opaque-unit",
    ],
    candidate_id: str,
    override: str | None = None,
) -> str:
    if override:
        candidate_ref = override
    else:
        template = candidate_ref_template(source_root, environment_name)
        candidate_ref = (
            template.replace("{environment}", environment_name)
            .replace("{operation}", operation)
            .replace("{id}", candidate_id)
        )
    return canonical_publication_ref(candidate_ref)


def load_environment_specifications(source_root: Path, environment_name: str) -> dict[str, UnitResource[Any]]:
    load_environment(source_root, environment_name)
    try:
        environment_root = project_environment_root(source_root, environment_name)
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc
    unit_paths: list[Path] = []
    stems = sorted(
        {path.stem for path in (environment_root / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}}
    )
    for stem in stems:
        candidates = document_candidates(environment_root / "units", stem)
        if len(candidates) > 1:
            raise OperationError(f"multiple document formats exist for unit {stem}")
        unit_paths.extend(candidates)
    specification_paths = {path.stem: path for path in unit_paths}
    specifications: dict[str, UnitResource[Any]] = {}
    for path in unit_paths:
        raw = load_json(path)
        template_value = raw.get("spec", raw)
        try:
            _template(template_value, "/spec")
        except OperationError as exc:
            raise OperationError(f"{path.relative_to(source_root)}: {exc}") from exc
        specifications[path.stem] = RESOURCE_CATALOG.parse_unit(
            cast(JsonObject, raw), profile="authored", expected_name=path.stem
        )
    for unit_name, specification in specifications.items():
        document = specification.driver.unit_contract.dump(specification.spec)
        try:
            _template(document, "/spec")
        except OperationError as exc:
            path = specification_paths[unit_name].relative_to(source_root)
            raise OperationError(f"{path}: {exc}") from exc
        require_unit_specification(specification, unit_name)
    for consumer, specification in specifications.items():
        document = specification.driver.unit_contract.dump(specification.spec)
        try:
            producers = observation_reference_units(document, "/spec")
            references = artifact_references(document, "/spec")
        except OperationError as exc:
            path = specification_paths[consumer].relative_to(source_root)
            raise OperationError(f"{path}: {exc}") from exc
        for producer in producers:
            if producer in specifications and not specifications[producer].driver.authored_reconciliation_required(
                specifications[producer].spec
            ):
                raise OperationError(f"{consumer} cannot observe materialization-only unit {producer!r}")
        for reference in references:
            producer = reference.unit
            artifact_name = reference.name
            if producer in specifications:
                driver = specifications[producer].driver
                if driver is not None and artifact_name not in driver.artifact_outputs:
                    raise OperationError(f"{consumer} references unknown artifact {producer}/{artifact_name}")
                elif driver is not None and driver.artifact_outputs[artifact_name].gvk != reference.gvk:
                    raise OperationError(
                        f"{consumer} expects artifact {producer}/{artifact_name} to be {reference.gvk}; "
                        f"producer declares {driver.artifact_outputs[artifact_name].gvk}"
                    )
    return specifications


def _document_paths(directory: Path) -> dict[str, Path]:
    """Return one deterministic document path per resource name in a directory."""

    paths: dict[str, Path] = {}
    stems = sorted(
        {path.stem for path in directory.glob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}}
    )
    for stem in stems:
        candidates = document_candidates(directory, stem)
        if len(candidates) > 1:
            raise OperationError(f"multiple document formats exist for resource {stem}")
        if candidates:
            paths[stem] = candidates[0]
    return paths


def _load_authored_stack_resources(
    source_root: Path, environment_name: str
) -> tuple[dict[str, StackResource], dict[str, StackResource]]:
    """Load project-level templates and environment-local Stack resources."""

    environment_root = project_environment_root(source_root, environment_name)
    templates: dict[str, StackResource] = {}
    project = load_project_config(source_root)
    template_root = source_root.joinpath(*project.stack_templates_path.parts)
    for name, path in _document_paths(template_root).items():
        templates[name] = RESOURCE_CATALOG.parse_stack_template(
            RESOURCE_CATALOG.load_document(path), profile="authored", expected_name=name
        )
    stacks: dict[str, StackResource] = {}
    for name, path in _document_paths(environment_root / "stacks").items():
        stacks[name] = RESOURCE_CATALOG.parse_stack(
            RESOURCE_CATALOG.load_document(path), profile="authored", expected_name=name
        )
    return templates, stacks


def _current_desired_stack_paths(root: Path, kind: Literal["StackTemplate", "Stack"]) -> dict[str, Path]:
    directory = root / ("stack-templates" if kind == "StackTemplate" else "stacks")
    return _document_paths(directory)


@dataclass(frozen=True)
class StackProjection:
    generated_units: dict[str, UnitResource[Any]]
    owners: dict[str, DesiredOwnerReference]
    dependencies: dict[str, tuple[str, ...]]
    artifact_imports: dict[str, tuple[ArtifactImport, ...]] = field(default_factory=dict)
    source_contexts: dict[str, StackUnitSourceContext] = field(default_factory=dict)
    applied_stacks: frozenset[str] = frozenset()
    structural_projections: dict[str, StructuralStackProjection] = field(default_factory=dict)


@dataclass(frozen=True)
class StackUnitSourceContext:
    """Materialized repository context for one projected Unit's effective source revision."""

    root: Path
    repository: str
    revision: str | None


@dataclass(frozen=True)
class StackUnitSourceTransport:
    """Process-only authenticated selector for an acquired Git template."""

    repository: str
    ref: str


@dataclass(frozen=True)
class AuthenticatedStackWorkloadRevision:
    """Repository-scoped proof established by an exact controller pin."""

    repository: str
    revision: str


@dataclass(frozen=True)
class AuthenticatedStackTemplateContext:
    """Repository-scoped proof established by materializing the acquired template commit."""

    repository: str
    revision: str


@dataclass(frozen=True)
class NormalizedStackUnitSource:
    """Normalized Unit spec and the checkout selected for its effective source."""

    spec: dict[str, Any]
    source_context: StackUnitSourceContext | None


@dataclass(frozen=True)
class StackWorkloadPinRequirement:
    """Exact controller alias used to hydrate one Stack-owned Unit source."""

    name: str
    revision: str


@dataclass(frozen=True)
class SourceDocumentImportResult:
    """The parsed document and its exact, non-retaining source checkout."""

    resource: StackResource
    source_path: Path
    checkout: Path


@dataclass(frozen=True)
class GitStackTemplateAcquisitionResult:
    """Resolved inline content and provenance for one Git-backed template."""

    inline_spec: StackTemplateInlineSpec
    acquisition: StackTemplateAcquisition
    source_context: StackTemplateSourceContext
    source_root: Path


@dataclass(frozen=True)
class PromotedStackTemplateAcquisitionResult:
    """Resolved inline content and provenance for one promotion source."""

    inline_spec: StackTemplateInlineSpec
    acquisition: StackTemplateAcquisition
    source_context: StackTemplateSourceContext | None


def _write_desired_stack_resource(path: Path, resource: StackResource, project_root: Path) -> Path:
    document = RESOURCE_CATALOG.serialize_stack_resource(resource, profile="desired")
    if resource_documents_enabled(project_root):
        selected = load_project_config(project_root).write_format
        path = path.with_suffix(selected.suffix)
    else:
        selected = DocumentFormat.YAML if path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    return write_document(path, document, format=selected)


def _template_has_repository_sources(spec: StackTemplateSpec) -> bool:
    """Return whether any inline Unit template needs repository-backed inputs."""

    def visit_source(value: object) -> bool:
        if getattr(value, "fromParameter", None) is not None:
            return True
        if isinstance(value, dict):
            if "path" in value or "fromParameter" in value:
                return True
            return any(visit_source(item) for item in value.values())
        if isinstance(value, list):
            return any(visit_source(item) for item in value)
        return False

    def visit(value: object) -> bool:
        if isinstance(value, dict):
            source = value.get("source")
            if source is not None and visit_source(source):
                return True
            return any(visit(item) for name, item in value.items() if name != "source")
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return False

    return any(visit(template.spec) for template in spec.unitTemplates.values())


def _stack_template_document_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _materialize_template_source_context(
    template: StackResource,
    source_root: Path,
    source_revision: str | None,
    candidate: Path,
    environment_name: str,
) -> tuple[Path, str | None]:
    """Select the exact source tree used by generated repository-backed Units."""

    assert isinstance(template.spec, DesiredStackTemplateSpec)
    context = template.spec.sourceContext
    if context is None:
        return source_root, source_revision
    if context.repository == "." and context.revision == source_revision:
        return source_root, context.revision
    checkout = candidate.parent / f".stack-template-source-{context.revision}"
    if not checkout.exists():
        try:
            materialize_revision(context.revision, checkout)
        except (OperationError, subprocess.CalledProcessError) as original:
            # A later Stack-only apply may run in a clone that has pruned the
            # original source object. Fetch the exact remote controller pin,
            # or an exact matching attempt claim left by a successful
            # publication whose canonical promotion failed, then retry by
            # immutable revision.
            if template.metadata.uid is None:
                raise OperationError(
                    f"StackTemplate {template.name!r} has no UID for source pin recovery"
                ) from original
            pin_name = _stack_template_source_pin_name(
                environment_name, template.name, template.metadata.uid, context.revision
            )
            store = state_store()
            hydrate = getattr(store, "hydrate_source_revision", None)
            if callable(hydrate):
                try:
                    hydrate(pin_name, context.revision)
                except OperationError:
                    raise OperationError(
                        f"StackTemplate {template.name!r} source context {context.revision!r} is not materializable"
                    ) from original
            else:
                # Compatibility for small test doubles; production hydration
                # uses canonical, publication-owner, then validated claim refs.
                pin_ref = f"refs/heads/gitopsctr/pins/{pin_name}"
                canonical = store.git("ls-remote", "--exit-code", "--refs", "origin", pin_ref, check=False)
                if canonical.returncode == 2:
                    list_pins = getattr(store, "list_controller_pins", None)
                    if callable(list_pins):
                        matching_claim = next(
                            (
                                pin
                                for pin in cast(tuple[ControllerPin, ...], list_pins())
                                if pin.name.startswith("claims/")
                                and "/".join(pin.name.split("/")[2:]) == pin_name
                                and pin.revision == context.revision
                            ),
                            None,
                        )
                        if matching_claim is not None:
                            pin_ref = matching_claim.ref
                fetched = store.git(
                    "fetch", "origin", f"{pin_ref}:refs/remotes/origin/gitopsctr/pins/{pin_name}", check=False
                )
                if fetched.returncode != 0:
                    raise OperationError(
                        f"StackTemplate {template.name!r} source context {context.revision!r} is not materializable"
                    ) from original
            materialize_revision(context.revision, checkout)
    return checkout, context.revision


def _materialize_stack_workload_revision(
    repository: str,
    revision: str,
    inherited_root: Path,
    inherited_revision: str,
    candidate: Path,
    cache: dict[tuple[str, str, str], Path],
    retention_pin_name: str | None = None,
    transport: StackUnitSourceTransport | None = None,
    authenticated: bool = False,
    authenticated_context: AuthenticatedStackTemplateContext | None = None,
) -> Path:
    """Resolve and materialize one exact workload revision in its template repository."""

    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise OperationError("Stack Unit source revision must be an exact lowercase 40-hex Git commit")
    if revision == inherited_revision:
        # The template acquisition already proved and materialized this exact
        # repository context. Reuse it without inventing another checkout.
        return inherited_root
    key = (repository, inherited_revision, revision)
    existing = cache.get(key)
    if existing is not None:
        return existing
    store = state_store()
    if (
        (authenticated or authenticated_context == AuthenticatedStackTemplateContext(repository, inherited_revision))
        and commit_is_available(revision)
        and commit_is_available(inherited_revision)
        and commit_is_ancestor(revision, inherited_revision)
    ):
        checkout = (
            candidate.parent
            / f".stack-workload-source-{hashlib.sha256(f'{repository}\0{revision}'.encode()).hexdigest()[:16]}"
        )
        if checkout.exists() and not checkout.is_dir():
            raise OperationError(f"workload source checkout {checkout.name!r} is not a directory")
        if checkout.exists():
            shutil.rmtree(checkout)
        materialize_revision(revision, checkout)
        cache[key] = checkout
        return checkout
    try:
        source = (
            store.resolve_source(transport.repository, transport.ref, revision=revision)
            if transport is not None
            else store.resolve_source(repository, revision)
        )
        source_key = hashlib.sha256(f"{repository}\0{source.revision}".encode()).hexdigest()[:16]
        checkout = candidate.parent / f".stack-workload-source-{source_key}"
        if checkout.exists():
            if not checkout.is_dir():
                raise OperationError(f"workload source checkout {checkout.name!r} is not a directory")
        else:
            materialize_source = getattr(store, "materialize_source", None)
            if callable(materialize_source):
                materialize_source(source, checkout)
            else:
                materialize_revision(source.revision, checkout)
        cache[key] = checkout
        return checkout
    except (OperationError, OSError, subprocess.CalledProcessError) as exc:
        hydrate = getattr(store, "hydrate_source_revision", None)
        if retention_pin_name is not None and callable(hydrate):
            checkout = (
                candidate.parent
                / f".stack-workload-source-{hashlib.sha256(f'{repository}\0{revision}'.encode()).hexdigest()[:16]}"
            )
            try:
                hydrate(retention_pin_name, revision)
                if not commit_is_available(inherited_revision) or not commit_is_ancestor(revision, inherited_revision):
                    raise OperationError(
                        f"Stack Unit source revision {revision!r} is outside the acquired template history"
                    )
                if checkout.exists() and not checkout.is_dir():
                    raise OperationError(f"workload source checkout {checkout.name!r} is not a directory")
                if checkout.exists():
                    shutil.rmtree(checkout)
                materialize_revision(revision, checkout)
                cache[key] = checkout
                return checkout
            except (OperationError, OSError, subprocess.CalledProcessError) as hydration_error:
                raise OperationError(
                    f"Stack Unit source revision {revision!r} is unavailable in repository {repository!r}"
                ) from hydration_error
        raise OperationError(
            f"Stack Unit source revision {revision!r} is unavailable in repository {repository!r}"
        ) from exc


def _normalize_stack_unit_source(
    spec: object,
    *,
    repository: str,
    inherited_revision: str,
    inherited_root: Path,
    candidate: Path,
    checkout_cache: dict[tuple[str, str, str], Path],
    retention_pin_prefix: str | None = None,
    transport: StackUnitSourceTransport | None = None,
    authenticated_revisions: frozenset[AuthenticatedStackWorkloadRevision] = frozenset(),
    authenticated_context: AuthenticatedStackTemplateContext | None = None,
) -> NormalizedStackUnitSource:
    """Persist the effective exact source revision and select its checkout."""

    raw = dump_template_value(cast(TemplateValue, spec))
    if not isinstance(raw, dict):
        raise OperationError("projected Unit spec must be an object")
    source = raw.get("source")
    if source is None:
        return NormalizedStackUnitSource(spec=raw, source_context=None)
    if not isinstance(source, dict):
        return NormalizedStackUnitSource(spec=raw, source_context=None)
    if not isinstance(source.get("path"), str):
        return NormalizedStackUnitSource(spec=raw, source_context=None)
    requested = source.get("revision")
    if requested is None:
        effective = inherited_revision
        selected_root = inherited_root
    else:
        if not isinstance(requested, str):
            raise OperationError("Stack Unit source revision must resolve to an exact Git commit")
        effective = requested
        selected_root = _materialize_stack_workload_revision(
            repository,
            requested,
            inherited_root,
            inherited_revision,
            candidate,
            checkout_cache,
            f"{retention_pin_prefix}{requested}" if retention_pin_prefix is not None else None,
            transport,
            AuthenticatedStackWorkloadRevision(repository, requested) in authenticated_revisions,
            authenticated_context,
        )
    source = dict(source)
    source["revision"] = effective
    raw["source"] = source
    return NormalizedStackUnitSource(
        spec=raw,
        source_context=StackUnitSourceContext(root=selected_root, repository=repository, revision=effective),
    )


def _canonical_stack_template_git_request(request: Any, repository: str) -> Any:
    """Return a desired-state-safe Git request without transport credentials."""

    return replace(request, repository=repository)


def _hydrate_stack_template_source_pin(environment: str, name: str, uid: str, revision: str) -> None:
    """Hydrate canonical, publication-owner, or claim-only source ownership."""

    pin_name = _stack_template_source_pin_name(environment, name, uid, revision)
    store = state_store()
    hydrate_source = getattr(store, "hydrate_source_revision", None)
    if callable(hydrate_source):
        hydrate_source(pin_name, revision)
        return
    hydrate = getattr(store, "hydrate_controller_pin", None)
    if callable(hydrate):
        hydrate(pin_name, revision)
        return
    pin_ref = f"refs/heads/gitopsctr/pins/{pin_name}"
    remote = store.git("ls-remote", "--exit-code", "--refs", "origin", pin_ref, check=False)
    if remote.returncode != 0 or not any(
        fields == [revision, pin_ref] for fields in (line.split() for line in remote.stdout.splitlines())
    ):
        raise OperationError("StackTemplate source pin is missing or points to an unexpected revision")
    local_ref = f"refs/remotes/origin/gitopsctr/pins/{pin_name}"
    fetched = store.git("fetch", "--no-tags", "--no-write-fetch-head", "origin", f"+{pin_ref}:{local_ref}", check=False)
    if fetched.returncode != 0:
        raise OperationError("could not hydrate StackTemplate source pin")


def _source_document_from_import(
    source: GitSourceRevision,
    path: str,
    target_name: str,
    candidate: Path,
) -> SourceDocumentImportResult:
    """Materialize one exact source revision and parse its requested document.

    No remote retention ref is created here. Durable ownership is acquired
    only after the complete candidate has passed local validation.
    """

    try:
        source_key = hashlib.sha256(f"{source.repository}\0{source.revision}".encode()).hexdigest()[:16]
        checkout = candidate.parent / f".stack-template-import-{source_key}"
        if checkout.exists():
            shutil.rmtree(checkout)
        store = state_store()
        materialize_source = getattr(store, "materialize_source", None)
        if callable(materialize_source):
            materialize_source(source, checkout)
        else:
            # Keep lightweight test/double stores compatible while the real
            # state store uses the non-retaining exact-source path above.
            materialize_revision(source.revision, checkout)
        source_path = checkout.joinpath(*PurePosixPath(path).parts)
        if not source_path.is_file() or source_path.is_symlink():
            raise OperationError(
                f"StackTemplate source document {path!r} is unavailable at exact revision {source.revision}"
            )
        document = RESOURCE_CATALOG.load_document(source_path)
        if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "StackTemplate":
            raise OperationError(f"StackTemplate source document {path!r} has the wrong GVK")
        resource = RESOURCE_CATALOG.parse_stack_template(document, profile="authored", expected_name=target_name)
    except (DocumentFormatError, OperationError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise OperationError(f"StackTemplate source document {path!r} is invalid: {exc}") from exc
    if not isinstance(resource.spec, StackTemplateInlineSpec):
        raise OperationError(f"StackTemplate source document {path!r} recursively selects another source")
    return SourceDocumentImportResult(resource, source_path, checkout)


def _acquire_git_stack_template(
    authored: StackResource,
    target_name: str,
    candidate: Path,
) -> GitStackTemplateAcquisitionResult:
    if not isinstance(authored.spec, StackTemplateGitSpec):
        raise OperationError(f"StackTemplate {target_name!r} has an invalid Git source")
    request = authored.spec.source.fromGit
    try:
        source = state_store().resolve_source(request.repository, request.revision)
    except (OperationError, OSError, subprocess.CalledProcessError) as exc:
        raise OperationError(f"could not resolve StackTemplate source for {target_name!r}: {exc}") from exc
    imported = _source_document_from_import(source, request.path, target_name, candidate)
    selected, source_path, checkout = imported.resource, imported.source_path, imported.checkout
    raw_digest = _stack_template_document_digest(source_path)
    if request.documentDigest is not None and request.documentDigest != raw_digest:
        raise OperationError(
            f"StackTemplate source documentDigest mismatch: expected {request.documentDigest}, got {raw_digest}"
        )
    assert isinstance(selected.spec, StackTemplateInlineSpec)
    identity = source.repository
    safe_request = _canonical_stack_template_git_request(request, identity)
    acquisition = StackTemplateAcquisition(
        documentDigest=raw_digest,
        requestedSource=StackTemplateRequestedFromGit(fromGit=safe_request),
        resolvedSource=StackTemplateResolvedFromGitSource(
            fromGit=StackTemplateResolvedFromGit(repository=identity, revision=source.revision, path=request.path)
        ),
    )
    context = StackTemplateSourceContext(repository=identity, revision=source.revision)
    return GitStackTemplateAcquisitionResult(selected.spec, acquisition, context, checkout)


def _acquire_promoted_stack_template(
    authored: StackResource,
    target_name: str,
    promotion: PromotionContext | None,
    authenticated_revisions: set[AuthenticatedStackWorkloadRevision] | None = None,
) -> PromotedStackTemplateAcquisitionResult:
    if authenticated_revisions is None:
        authenticated_revisions = set()
    if promotion is None:
        raise OperationError("StackTemplate source.fromPromotion is legal only in an explicit promote transaction")
    if not isinstance(authored.spec, StackTemplatePromotionSpec):
        raise OperationError(f"StackTemplate {target_name!r} has an invalid promotion source")
    source_name = authored.spec.source.fromPromotion.stack
    try:
        source_resources = load_desired_resource_graph(promotion.desired_root)
        authenticated_revisions.update(
            _hydrate_required_stack_workload_pins(
                promotion.source_environment,
                promotion.desired_root,
            )
        )
    except (OperationError, TypeError, ValueError) as exc:
        raise OperationError(f"promotion source desired snapshot is corrupt: {exc}") from exc
    source_stack = source_resources.get((CORE_API_VERSION, "Stack", source_name))
    if not isinstance(source_stack, StackResource) or not isinstance(source_stack.spec, DesiredStackSpec):
        raise OperationError(f"promotion source Stack {source_name!r} is missing or not desired")
    if resource_deletion(source_stack) is not None or source_stack.metadata.uid is None:
        raise OperationError(f"promotion source Stack {source_name!r} is inactive or deleting")
    source_template = source_resources.get((CORE_API_VERSION, "StackTemplate", source_stack.spec.templateRef.name))
    if not isinstance(source_template, StackResource) or not isinstance(source_template.spec, DesiredStackTemplateSpec):
        raise OperationError(f"promotion source Stack {source_name!r} has no desired StackTemplate")
    if resource_deletion(source_template) is not None:
        raise OperationError(f"promotion source StackTemplate {source_template.name!r} is deleting")
    if source_template.metadata.uid is None or source_template.name != target_name:
        raise OperationError(
            f"promotion source Stack {source_name!r} selects StackTemplate {source_template.name!r}, "
            f"not target {target_name!r}"
        )
    if source_stack.spec.templateRef.uid != source_template.metadata.uid:
        raise OperationError(f"promotion source Stack {source_name!r} has a stale StackTemplate UID fence")
    if source_stack.spec.templateRef.contentDigest != source_template.spec.contentDigest:
        raise OperationError(f"promotion source Stack {source_name!r} has a stale StackTemplate content fence")
    source_template_path = _current_desired_stack_paths(promotion.desired_root, "StackTemplate").get(
        source_template.name
    )
    if source_template_path is None:
        raise OperationError(f"promotion source StackTemplate {source_template.name!r} document is missing")
    exact_document_digest = _stack_template_document_digest(source_template_path)
    if source_template.spec.sourceContext is not None:
        try:
            _hydrate_stack_template_source_pin(
                promotion.source_environment,
                source_template.name,
                source_template.metadata.uid,
                source_template.spec.sourceContext.revision,
            )
        except OperationError:
            # Older source desired snapshots may have been published by a
            # trusted local runner before canonical source pins existed. Keep
            # that compatible path only when the exact local object is still
            # present; a fresh runner must use the canonical pin.
            if (
                source_template.spec.sourceContext.repository != "."
                or state_store()
                .git(
                    "rev-parse",
                    "--verify",
                    f"{source_template.spec.sourceContext.revision}^{{commit}}",
                    check=False,
                )
                .returncode
                != 0
            ):
                raise
    resolved = StackTemplateResolvedFromPromotion(
        environment=promotion.source_environment,
        desiredRef=promotion.desired_ref,
        desiredRevision=promotion.desired_revision,
        stack=source_name,
        stackUid=source_stack.metadata.uid,
        template=source_template.name,
        templateUid=source_template.metadata.uid,
        templateContentDigest=source_template.spec.contentDigest,
    )
    acquisition = StackTemplateAcquisition(
        documentDigest=exact_document_digest,
        requestedSource=StackTemplateRequestedFromPromotion(fromPromotion=authored.spec.source.fromPromotion),
        resolvedSource=StackTemplateResolvedFromPromotionSource(fromPromotion=resolved),
    )
    return PromotedStackTemplateAcquisitionResult(
        inline_spec=StackTemplateInlineSpec(
            parameters=list(source_template.spec.parameters),
            unitTemplates=dict(source_template.spec.unitTemplates),
        ),
        acquisition=acquisition,
        source_context=source_template.spec.sourceContext,
    )


def _stack_root_metadata(
    kind: Literal["StackTemplate", "Stack"],
    name: str,
    source_revision: str | None,
    current_desired: Path | None = None,
    partition: str | None = None,
) -> ResourceMetadata:
    if current_desired is not None:
        existing_path = _current_desired_stack_paths(current_desired, kind).get(name)
        if existing_path is not None:
            existing = (
                RESOURCE_CATALOG.parse_stack_template(
                    RESOURCE_CATALOG.load_document(existing_path), profile="desired", expected_name=name
                )
                if kind == "StackTemplate"
                else RESOURCE_CATALOG.parse_stack(
                    RESOURCE_CATALOG.load_document(existing_path), profile="desired", expected_name=name
                )
            )
            existing.metadata.validate_desired()
            if not existing.metadata.is_root:
                raise OperationError(f"applied {kind} {name!r} collides with an owned desired resource")
            if resource_deletion(existing) is not None:
                raise OperationError(f"desired {kind} {name!r} is deleting and cannot be applied")
            if partition is not None and existing.metadata.partition not in {None, partition}:
                raise OperationError(f"desired {kind} {name!r} belongs to partition {existing.metadata.partition!r}")
            return existing.metadata.with_partition(partition, preserve_existing=partition is None)
    provenance = json.dumps(
        {"apiVersion": CORE_API_VERSION, "kind": kind, "name": name, "sourceRevision": source_revision},
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata = ResourceMetadata.root_from_provenance(name, provenance, partition=partition)
    if current_desired is not None:
        finalized_uids = tuple(
            sorted(
                tombstone.uid
                for tombstone in load_resource_incarnation_evidence(current_desired)
                if tombstone.api_version == CORE_API_VERSION and tombstone.kind == kind and tombstone.name == name
            )
        )
        if finalized_uids:
            metadata = ResourceMetadata.root_from_provenance(
                name,
                provenance + "\0reincarnations:" + "\0".join(finalized_uids),
                partition=partition,
            )
    return metadata


def _stack_owned_metadata(name: str, owner: DesiredOwnerReference) -> ResourceMetadata:
    root = ResourceMetadata.root_from_provenance(
        name,
        json.dumps(
            {
                "apiVersion": UNIT_API_VERSION,
                "kind": "generated",
                "name": name,
                "stack": owner.name,
                "stackUid": owner.uid,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return ResourceMetadata(
        name=name,
        uid=root.uid,
        ownerReferences=[
            DesiredOwnerReference(
                apiVersion=owner.apiVersion,
                kind=owner.kind,
                name=owner.name,
                uid=owner.uid,
            ),
        ],
    )


def _bind_active_stack_projections(
    candidate: Path,
    current_desired: Path,
    blocked_transitions: Mapping[str, str],
    project_root: Path,
) -> set[str]:
    """Authenticate every concrete selected Stack Unit before graph validation."""

    current_resources = (
        load_desired_resource_graph(current_desired, validate=False) if any(current_desired.iterdir()) else {}
    )
    candidate_resources = load_desired_resource_graph(candidate, validate=False)
    atomically_retained_units: set[str] = set()
    for stack in tuple(candidate_resources.values()):
        if not isinstance(stack, StackResource) or stack.gvk.kind != "Stack":
            continue
        if not isinstance(stack.spec, DesiredStackSpec):
            continue
        structural = stack.spec.structuralProjection
        previous = current_resources.get((CORE_API_VERSION, "Stack", stack.name))
        previous_active = (
            previous.spec.activeProjection
            if isinstance(previous, StackResource) and isinstance(previous.spec, DesiredStackSpec)
            else None
        )
        generated_names = {stack_generated_unit_name(stack.name, logical_name) for logical_name in structural.units}
        blocked_names = generated_names.intersection(blocked_transitions)
        waiting = bool(blocked_names)
        staged = False
        if waiting and previous_active is not None:
            # A dependency producer must be able to advance before a consumer
            # can resolve the producer's new receipt/artifact. Stage that
            # producer while retaining only blocked bindings from the prior
            # active projection. Each binding carries its own exact projection
            # and context provenance, so the mixed transition remains fenced.
            compatible = all(
                logical_name in structural.units
                and structural.units[logical_name].apiVersion == binding.apiVersion
                and structural.units[logical_name].kind == binding.kind
                and logical_name == binding.name
                and sorted(binding.dependsOn) == sorted(structural.units[logical_name].dependsOn)
                for logical_name, binding in previous_active.units.items()
            )
            staged = compatible
            source_projection_digest = (
                structural.identity.projectionDigest if staged else previous_active.sourceProjectionDigest
            )
            active_names = {
                stack_generated_unit_name(stack.name, binding.name) for binding in previous_active.units.values()
            }
            stack_owner = (stack.gvk.api_version, stack.gvk.kind, stack.name, stack.metadata.uid)
            retained_bindings = (
                tuple(
                    binding
                    for binding in previous_active.units.values()
                    if stack_generated_unit_name(stack.name, binding.name) in blocked_names
                )
                if staged
                else tuple(previous_active.units.values())
            )
            if not staged:
                for resource_key, resource in tuple(candidate_resources.items()):
                    if not isinstance(resource, UnitResource) or resource_key[2] in active_names:
                        continue
                    owner = resource_owner_reference(resource)
                    if owner is None or (owner.apiVersion, owner.kind, owner.name, owner.uid) != stack_owner:
                        continue
                    extra_path = unit_document_path(candidate, resource_key[2])
                    if extra_path.is_file():
                        extra_path.unlink()
                    materialization = getattr(resource.spec, "materialization", None)
                    if materialization is not None:
                        materialized_path = candidate / materialization.path
                        if materialized_path.is_dir():
                            shutil.rmtree(materialized_path)
            for binding in retained_bindings:
                previous_name = stack_generated_unit_name(stack.name, binding.name)
                previous_path = unit_document_path(current_desired, previous_name)
                if not previous_path.is_file():
                    raise OperationError(
                        f"Stack {stack.name!r} active projection references missing Unit {previous_name!r}"
                    )
                candidate_path = unit_document_path(candidate, previous_name)
                if candidate_path.is_file():
                    candidate_path.unlink()
                target_path = unit_document_path(candidate, previous_name, project_root)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(previous_path, target_path)
                previous_unit = load_desired_unit(previous_path, previous_path.stem)
                if getattr(previous_unit.spec, "materialization", None) is not None:
                    copy_unit_materialization(current_desired, candidate, previous_name, previous_unit)
                atomically_retained_units.add(previous_name)
        elif waiting:
            source_projection_digest = structural.identity.projectionDigest
            # There is no previous active set to retain on a first projection.
            # Concrete siblings may bootstrap the graph; each first-time
            # blocked child remains absent/WAIT below.
        else:
            source_projection_digest = structural.identity.projectionDigest
        if waiting and previous_active is not None and not staged:
            # Keep the previous binding map as well as its source digest.  A
            # blocked structural transition may add, remove, or rename
            # logical children; the old active set remains authoritative until
            # the complete new projection is ready.
            active = previous_active
        else:
            bindings: dict[str, StackProjectionUnitBinding] = {}
            for logical_name, projected in structural.units.items():
                unit_name = stack_generated_unit_name(stack.name, logical_name)
                unit_path = unit_document_path(candidate, unit_name)
                if not unit_path.is_file():
                    continue
                unit = load_desired_unit(unit_path, unit_path.stem)
                owner = resource_owner_reference(unit)
                if (
                    resource_deletion(unit) is not None
                    or unit.gvk.api_version != projected.apiVersion
                    or unit.gvk.kind != projected.kind
                    or owner is None
                    or owner.apiVersion != stack.gvk.api_version
                    or owner.kind != stack.gvk.kind
                    or owner.name != stack.name
                    or owner.uid != stack.metadata.uid
                    or unit.metadata.uid is None
                ):
                    continue
                bindings[logical_name] = StackProjectionUnitBinding(
                    apiVersion=unit.gvk.api_version,
                    kind=unit.gvk.kind,
                    name=unit.name,
                    uid=unit.metadata.uid,
                    desiredDigest=desired_unit_binding_digest(unit),
                    sourceProjectionDigest=structural.identity.projectionDigest,
                    projectionContextDigest=structural.identity.projectionContextDigest,
                    dependsOn=list(projected.dependsOn),
                )
                if staged and unit_name in blocked_names and previous_active is not None:
                    previous_binding = previous_active.units.get(logical_name)
                    if previous_binding is not None:
                        bindings[logical_name] = previous_binding
            # A first projection publishes only the dependency-closed active
            # subset.  A concrete descendant of an unavailable Unit is not
            # executable merely because its own dynamic fields resolved.
            changed = True
            while changed:
                changed = False
                active_names = {binding.name for binding in bindings.values()}
                for logical_name, binding in tuple(bindings.items()):
                    if any(dependency not in active_names for dependency in binding.dependsOn):
                        bindings.pop(logical_name)
                        changed = True
            if waiting and (previous_active is None or staged):
                # A first projection has no prior active lineage to retain.
                # Remove concrete descendants that were resolved locally but
                # are not in the dependency-closed active subset; otherwise
                # the persisted graph would contain executable Units without
                # an authenticated active binding.
                active_names = set(bindings)
                for logical_name in set(structural.units) - active_names:
                    unit_name = stack_generated_unit_name(stack.name, logical_name)
                    unit_path = unit_document_path(candidate, unit_name)
                    if unit_path.is_file():
                        unit_path.unlink()
                    unit = candidate_resources.get((UNIT_API_VERSION, structural.units[logical_name].kind, unit_name))
                    materialization = getattr(getattr(unit, "spec", None), "materialization", None)
                    if materialization is not None:
                        materialized_path = candidate / materialization.path
                        if materialized_path.is_dir():
                            shutil.rmtree(materialized_path)
            active = StackActiveProjection.build(
                source_projection_digest=source_projection_digest,
                projection_context_digest=(
                    previous_active.projectionContextDigest
                    if waiting and previous_active is not None and not staged
                    else structural.identity.projectionContextDigest
                ),
                units=bindings,
            )
        updated = replace(stack, spec=replace(stack.spec, activeProjection=active))
        _write_desired_stack_resource(candidate / "stacks" / f"{stack.name}.json", updated, project_root)
    return atomically_retained_units


def _stack_template_source_pin_prefix(environment: str, template_name: str, uid: str) -> str:
    """Return the controller-pin namespace for one StackTemplate incarnation."""

    return f"stack-templates/{environment}/{template_name}/{uid}/"


def _stack_template_source_pin_name(environment: str, template_name: str, uid: str, revision: str) -> str:
    return f"{_stack_template_source_pin_prefix(environment, template_name, uid)}{revision}"


def _stack_workload_source_pin_name(
    environment: str,
    template_name: str,
    template_uid: str,
    stack_name: str,
    stack_uid: str,
    revision: str,
) -> str:
    """Return a Stack/template-incarnation-exact workload source pin name."""

    return (
        f"{_stack_template_source_pin_prefix(environment, template_name, template_uid)}"
        f"stacks/{stack_name}/{stack_uid}/{revision}"
    )


def _stack_workload_pin_for_unit(
    desired_root: Path,
    environment: str,
    unit: UnitResource[Any],
) -> StackWorkloadPinRequirement | None:
    source = getattr(unit.spec, "source", None)
    owner = resource_owner_reference(unit)
    if not isinstance(source, DesiredSource) or source.revision is None or owner is None or owner.kind != "Stack":
        return None
    resources = load_desired_resource_graph(desired_root, validate=False)
    stack = resources.get((CORE_API_VERSION, "Stack", owner.name))
    if not isinstance(stack, StackResource) or not isinstance(stack.spec, DesiredStackSpec):
        raise OperationError(f"Stack-owned Unit {unit.name!r} has no desired Stack owner")
    if stack.metadata.uid != owner.uid:
        raise OperationError(f"Stack-owned Unit {unit.name!r} has a stale Stack UID fence")
    template = resources.get((CORE_API_VERSION, "StackTemplate", stack.spec.templateRef.name))
    if not isinstance(template, StackResource) or template.metadata.uid != stack.spec.templateRef.uid:
        raise OperationError(f"Stack {stack.name!r} has a stale StackTemplate identity fence")
    if template.metadata.uid is None or stack.metadata.uid is None:
        raise OperationError(f"Stack {stack.name!r} has no UID for workload source retention")
    return StackWorkloadPinRequirement(
        name=_stack_workload_source_pin_name(
            environment,
            template.name,
            template.metadata.uid,
            stack.name,
            stack.metadata.uid,
            source.revision,
        ),
        revision=source.revision,
    )


def _hydrate_stack_workload_pin_for_unit(
    desired_root: Path,
    environment: str,
    unit: UnitResource[Any],
) -> None:
    """Hydrate a Stack Unit's exact workload object before using its source."""

    pin = _stack_workload_pin_for_unit(desired_root, environment, unit)
    if pin is None:
        return
    name, revision = pin.name, pin.revision
    store = state_store()
    hydrate = getattr(store, "hydrate_source_revision", None)
    if not callable(hydrate):
        hydrate = getattr(store, "hydrate_controller_pin", None)
    if callable(hydrate):
        try:
            hydrate(name, revision)
        except OperationError:
            legacy_name = _legacy_stack_template_pin_for_workload(
                desired_root,
                environment,
                name,
                revision,
            )
            if legacy_name is not None:
                try:
                    hydrate(legacy_name, revision)
                    return
                except OperationError:
                    pass
            if not commit_is_available(revision):
                raise


def _legacy_stack_template_pin_for_workload(
    desired_root: Path,
    environment: str,
    workload_pin_name: str,
    revision: str,
) -> str | None:
    """Return the pre-workload-alias template pin for an inherited revision."""

    parts = workload_pin_name.split("/")
    if len(parts) != 8 or parts[:2] != ["stack-templates", environment]:
        return None
    template_name, template_uid = parts[2], parts[3]
    resources = load_desired_resource_graph(desired_root, validate=False)
    template = resources.get((CORE_API_VERSION, "StackTemplate", template_name))
    if (
        not isinstance(template, StackResource)
        or not isinstance(template.spec, DesiredStackTemplateSpec)
        or template.metadata.uid != template_uid
        or template.spec.sourceContext is None
        or template.spec.sourceContext.revision != revision
    ):
        return None
    return _stack_template_source_pin_name(environment, template_name, template_uid, revision)


def _hydrate_required_stack_workload_pins(
    environment: str,
    desired_root: Path,
) -> frozenset[AuthenticatedStackWorkloadRevision]:
    """Hydrate every retained structural/active Stack workload revision."""

    store = state_store()
    hydrate = getattr(store, "hydrate_source_revision", None)
    if not callable(hydrate):
        hydrate = getattr(store, "hydrate_controller_pin", None)
    if not callable(hydrate):
        return frozenset()
    authenticated: set[AuthenticatedStackWorkloadRevision] = set()
    resources = load_desired_resource_graph(desired_root, validate=False)
    for name, revision in _required_stack_template_source_pins(environment, desired_root):
        if not _is_stack_workload_pin_name(name, environment):
            continue
        parts = name.split("/")
        template = resources.get((CORE_API_VERSION, "StackTemplate", parts[2]))
        if (
            not isinstance(template, StackResource)
            or not isinstance(template.spec, DesiredStackTemplateSpec)
            or template.metadata.uid != parts[3]
            or template.spec.sourceContext is None
        ):
            raise OperationError(f"workload source pin {name!r} has no matching StackTemplate context")
        proof = AuthenticatedStackWorkloadRevision(template.spec.sourceContext.repository, revision)
        try:
            hydrate(name, revision)
            authenticated.add(proof)
        except OperationError:
            legacy_name = _legacy_stack_template_pin_for_workload(
                desired_root,
                environment,
                name,
                revision,
            )
            if legacy_name is not None:
                try:
                    hydrate(legacy_name, revision)
                    authenticated.add(proof)
                    continue
                except OperationError:
                    pass
            if not commit_is_available(revision):
                raise
    return frozenset(authenticated)


def _required_stack_template_source_pins(environment: str, desired_root: Path) -> tuple[tuple[str, str], ...]:
    """Collect exact template and effective workload revisions needed by desired state."""

    required: set[tuple[str, str]] = set()
    resources = load_desired_resource_graph(desired_root, validate=False)
    finalized_identities = {
        (tombstone.api_version, tombstone.kind, tombstone.qualified_name, tombstone.uid)
        for tombstone in load_resource_incarnation_evidence(desired_root)
    }
    for resource in resources.values():
        if not isinstance(resource, StackResource) or resource.gvk.kind != "StackTemplate":
            continue
        if not isinstance(resource.spec, DesiredStackTemplateSpec) or resource.spec.sourceContext is None:
            continue
        if resource.metadata.uid is None:
            raise OperationError(f"StackTemplate {resource.name!r} has no UID")
        revision = resource.spec.sourceContext.revision
        required.add(
            (_stack_template_source_pin_name(environment, resource.name, resource.metadata.uid, revision), revision)
        )
    for resource in resources.values():
        if (
            not isinstance(resource, StackResource)
            or resource.gvk.kind != "Stack"
            or not isinstance(resource.spec, DesiredStackSpec)
        ):
            continue
        if resource.metadata.uid is None:
            raise OperationError(f"Stack {resource.name!r} has no UID")
        template = resources.get((CORE_API_VERSION, "StackTemplate", resource.spec.templateRef.name))
        if not isinstance(template, StackResource) or not isinstance(template.spec, DesiredStackTemplateSpec):
            raise OperationError(f"Stack {resource.name!r} has no desired StackTemplate for source retention")
        if template.metadata.uid != resource.spec.templateRef.uid:
            raise OperationError(f"Stack {resource.name!r} has a stale StackTemplate UID fence")
        template_name = resource.spec.templateRef.name
        template_uid = template.metadata.uid
        if template_uid is None:
            raise OperationError(f"StackTemplate {template_name!r} has no UID for source retention")
        stack_name = resource.name
        stack_uid = resource.metadata.uid

        def add_workload_revision(
            revision: object,
            *,
            stack_name: str = stack_name,
            stack_uid: str = stack_uid,
            template_name: str = template_name,
            template_uid: str = template_uid,
        ) -> None:
            if revision is None:
                return
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise OperationError(f"Stack {stack_name!r} has an invalid workload source revision")
            required.add(
                (
                    _stack_workload_source_pin_name(
                        environment,
                        template_name,
                        template_uid,
                        stack_name,
                        stack_uid,
                        revision,
                    ),
                    revision,
                )
            )

        # Structural projection is the immutable selected Unit-template
        # contract.  Do not re-expand the current template here: a carried
        # blocked transition may intentionally retain a structural revision
        # that differs from today's authored input.
        for projected in resource.spec.structuralProjection.units.values():
            source = projected.spec.get("source")
            revision = source.get("revision") if isinstance(source, Mapping) else None
            add_workload_revision(revision)

        # During a blocked transition activeProjection may still authenticate
        # concrete Units from the previous structural revision.  Retain those
        # exact sources too, but only after binding API/kind/name/UID and the
        # desired payload digest through the active projection.
        active = resource.spec.activeProjection
        if active is None:
            continue
        for binding in active.units.values():
            qualified_name = stack_generated_unit_name(resource.name, binding.name)
            unit = resources.get((binding.apiVersion, binding.kind, qualified_name))
            if not isinstance(unit, UnitResource):
                if (
                    resource_deletion(resource) is not None
                    and (
                        binding.apiVersion,
                        binding.kind,
                        qualified_name,
                        binding.uid,
                    )
                    in finalized_identities
                ):
                    # During child-first deletion the Stack retains its active
                    # projection until every child is finalized. An exact
                    # incarnation tombstone replaces the removed child's
                    # workload-retention requirement in that intermediate
                    # accepted snapshot.
                    continue
                raise OperationError(
                    f"Stack {resource.name!r} active Unit {binding.name!r} is missing from desired state"
                )
            if unit.metadata.uid != binding.uid or desired_unit_binding_digest(unit) != binding.desiredDigest:
                raise OperationError(f"Stack {resource.name!r} active Unit {binding.name!r} failed its binding fence")
            owner = resource_owner_reference(unit)
            if (
                owner is None
                or owner.kind != "Stack"
                or owner.name != resource.name
                or owner.uid != resource.metadata.uid
            ):
                raise OperationError(f"Stack {resource.name!r} active Unit {binding.name!r} has an invalid owner")
            source = getattr(unit.spec, "source", None)
            add_workload_revision(getattr(source, "revision", None))
    return tuple(sorted(required))


def _ensure_stack_template_source_pins(environment: str, desired_root: Path) -> tuple[ControllerPin, ...]:
    """Retain every source context before desired publication or a no-op result."""

    required = _required_stack_template_source_pins(environment, desired_root)
    if not required:
        return ()
    store = state_store()
    if hasattr(store, "materialize"):
        resources = load_desired_resource_graph(desired_root, validate=False)
        revisions = {revision for _name, revision in required}
        for revision in sorted(revisions):
            with tempfile.TemporaryDirectory(prefix="gitopsctr-template-source-check-") as directory:
                materialized = Path(directory) / "source"
                try:
                    store.materialize(revision, materialized)
                except (OperationError, OSError, subprocess.CalledProcessError) as exc:
                    hydrate = getattr(store, "hydrate_source_revision", None)
                    matching_name = next(name for name, pin_revision in required if pin_revision == revision)
                    if not callable(hydrate):
                        raise OperationError(
                            f"StackTemplate source context {revision!r} is not materializable"
                        ) from exc
                    try:
                        hydrate(matching_name, revision)
                        store.materialize(revision, materialized)
                    except (OperationError, OSError, subprocess.CalledProcessError) as hydration_error:
                        raise OperationError(
                            f"StackTemplate source context {revision!r} is not materializable"
                        ) from hydration_error
                for resource in resources.values():
                    if not isinstance(resource, StackResource) or resource.gvk.kind != "StackTemplate":
                        continue
                    if not isinstance(resource.spec, DesiredStackTemplateSpec) or resource.spec.sourceContext is None:
                        continue
                    if resource.spec.sourceContext.revision != revision:
                        continue
                    for unit_template in resource.spec.unitTemplates.values():
                        raw_spec = dump_template_value(cast(TemplateValue, unit_template.spec))
                        source = raw_spec.get("source") if isinstance(raw_spec, dict) else None
                        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                            continue
                        source_path = cast(str, source["path"])
                        safe_source_path(source_path, f"{resource.name} source path")
                        source_directory = materialized / source_path
                        if not source_directory.exists():
                            raise OperationError(
                                f"StackTemplate {resource.name!r} source path {source_path!r} "
                                f"is not available at {revision}"
                            )
                        inputs = source.get("inputs")
                        if isinstance(inputs, list) and all(isinstance(value, str) for value in inputs):
                            hash_source_inputs(materialized, source_path, cast(list[str], inputs), {})
    return store.create_controller_pins(dict(required))


def _seed_missing_stack_workload_revisions(environment: str, desired_root: Path) -> None:
    """Import historical workload objects before creating a new owner namespace."""

    store = state_store()
    hydrate = getattr(store, "hydrate_source_revision", None)
    resolve_source = getattr(store, "resolve_source", None)
    materialize_source = getattr(store, "materialize_source", None)
    if not callable(resolve_source) or not callable(materialize_source):
        return
    resources = load_desired_resource_graph(desired_root, validate=False)
    for name, revision in _required_stack_template_source_pins(environment, desired_root):
        if not _is_stack_workload_pin_name(name, environment):
            continue
        if callable(hydrate):
            try:
                hydrate(name, revision)
                continue
            except OperationError:
                legacy_name = _legacy_stack_template_pin_for_workload(
                    desired_root,
                    environment,
                    name,
                    revision,
                )
                if legacy_name is not None:
                    try:
                        hydrate(legacy_name, revision)
                        continue
                    except OperationError:
                        pass
        if commit_is_available(revision):
            continue
        parts = name.split("/")
        template = resources.get((CORE_API_VERSION, "StackTemplate", parts[2]))
        if (
            not isinstance(template, StackResource)
            or not isinstance(template.spec, DesiredStackTemplateSpec)
            or template.metadata.uid != parts[3]
            or template.spec.sourceContext is None
        ):
            raise OperationError(f"workload source pin {name!r} has no matching StackTemplate context")
        context = template.spec.sourceContext
        try:
            source = resolve_source(context.repository, context.revision, revision=revision)
        except (OperationError, OSError, subprocess.CalledProcessError):
            source = resolve_source(context.repository, revision)
        with tempfile.TemporaryDirectory(prefix="gitopsctr-workload-seed-") as directory:
            materialize_source(source, Path(directory) / "source")


@dataclass(frozen=True)
class ControllerPinAcquisition:
    """Pins acquired by one publication attempt and safe to roll back."""

    pins: tuple[ControllerPin, ...]
    newly_created: tuple[ControllerPin, ...]
    claims: tuple[ControllerPin, ...] = ()


def _acquire_stack_template_source_pins(environment: str, desired_root: Path) -> ControllerPinAcquisition:
    required = _required_stack_template_source_pins(environment, desired_root)
    if not required:
        return ControllerPinAcquisition(pins=(), newly_created=())
    _seed_missing_stack_workload_revisions(environment, desired_root)
    store = state_store()
    reap = getattr(store, "reap_expired_controller_pin_claims", None)
    if callable(reap):
        reap()
    claim_creator = getattr(store, "create_controller_pin_claims", None)
    if callable(claim_creator):
        token = uuid.uuid4().hex[:16]
        required_map = dict(required)
        claims = cast(
            tuple[ControllerPin, ...],
            claim_creator(required_map, token),
        )
        return ControllerPinAcquisition(pins=claims, newly_created=claims, claims=claims)
    existing_names: set[str] | None = None
    list_pins = getattr(store, "list_controller_pins", None)
    if callable(list_pins):
        existing_names = {pin.name for pin in cast(tuple[ControllerPin, ...], list_pins())}
    pins = _ensure_stack_template_source_pins(environment, desired_root)
    newly_created = tuple(pin for pin in pins if existing_names is None or pin.name not in existing_names)
    return ControllerPinAcquisition(pins=pins, newly_created=newly_created)


def _release_new_stack_template_source_pins(acquisition: ControllerPinAcquisition | None) -> None:
    if acquisition is not None and acquisition.claims:
        store = state_store()
        first_error: BaseException | None = None
        for claim in acquisition.claims:
            try:
                store.release_controller_pin(claim.name, claim.revision)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return
    # Compatibility fallback for lightweight stores that do not expose
    # attempt claims: deterministic pins remain monotonic and are released
    # only by exact UID finalization.
    if acquisition is not None and acquisition.newly_created:
        log_status(
            "KEEP",
            "retained StackTemplate source pins after failed publication without attempt-claim support",
        )


def _is_stack_workload_pin_name(name: str, environment: str) -> bool:
    parts = name.split("/")
    safe_name = r"[a-z0-9][a-z0-9-]{0,62}"
    return (
        len(parts) == 8
        and parts[0] == "stack-templates"
        and re.fullmatch(safe_name, parts[1]) is not None
        and parts[1] == environment
        and re.fullmatch(safe_name, parts[2]) is not None
        and re.fullmatch(safe_name, parts[3]) is not None
        and parts[4] == "stacks"
        and re.fullmatch(safe_name, parts[5]) is not None
        and re.fullmatch(safe_name, parts[6]) is not None
        and re.fullmatch(r"[0-9a-f]{40}", parts[7]) is not None
    )


def _gc_superseded_stack_workload_pins(
    environment: str,
    desired_root: Path,
    accepted_target: AcceptedDesiredTarget,
) -> None:
    """Remove obsolete workload aliases after an accepted snapshot is fenced.

    The desired snapshot is authoritative for structural and active source
    requirements.  Publication owners are a second fence: an old alias is
    removable only after every accepted or review publication retaining it is
    no longer live.
    """

    store = state_store()
    list_pins = getattr(store, "list_controller_pins", None)
    list_owners = getattr(store, "list_controller_publication_owners", None)
    release_pin = getattr(store, "release_controller_pin", None)
    release_owner = getattr(store, "release_publication_owner", None)
    owner_is_live = getattr(store, "publication_owner_is_live", None)
    candidate_is_live = getattr(store, "publication_owner_is_live_candidate", None)
    hydrate_source = getattr(store, "hydrate_source_revision", None)
    create_pins = getattr(store, "create_controller_pins", None)
    if not all(
        callable(item)
        for item in (list_pins, list_owners, release_pin, release_owner, owner_is_live, candidate_is_live)
    ):
        # Lightweight stores used by older callers do not expose the owner
        # fence.  They cannot safely perform accepted-snapshot GC.
        return

    required = dict(_required_stack_template_source_pins(environment, desired_root))
    list_pins_fn = cast(Callable[[], Sequence[ControllerPin]], list_pins)
    list_owners_fn = cast(Callable[[], Sequence[Any]], list_owners)
    release_pin_fn = cast(Callable[[str, str], Any], release_pin)
    release_owner_fn = cast(Callable[..., Any], release_owner)
    owner_is_live_fn = cast(Callable[[Any], bool], owner_is_live)
    candidate_is_live_fn = cast(Callable[[Any, AcceptedDesiredTarget], bool], candidate_is_live)
    pins_by_name = {pin.name: pin for pin in list_pins_fn() if _is_stack_workload_pin_name(pin.name, environment)}
    owners_by_name: dict[str, list[Any]] = {}
    for owner in list_owners_fn():
        if _is_stack_workload_pin_name(owner.source_pin_name, environment):
            owners_by_name.setdefault(owner.source_pin_name, []).append(owner)
    for name in sorted(set(pins_by_name) | set(owners_by_name)):
        if name in required:
            continue
        owners = tuple(owners_by_name.get(name, ()))
        protected = False
        for owner in owners:
            if owner.publication_ref == accepted_target.ref:
                if owner_is_live_fn(owner):
                    protected = True
                    break
            elif candidate_is_live_fn(owner, accepted_target):
                protected = True
                break
        if protected:
            continue
        pin = pins_by_name.get(name)
        if pin is None and owners:
            revisions = {owner.revision for owner in owners}
            if len(revisions) != 1 or not callable(hydrate_source) or not callable(create_pins):
                continue
            revision = next(iter(revisions))
            try:
                hydrate_source(name, revision)
                cast(Callable[[Mapping[str, str]], Any], create_pins)({name: revision})
            except (OperationError, OSError, subprocess.CalledProcessError):
                continue
            pin = ControllerPin(name, f"refs/heads/gitopsctr/pins/{name}", revision)
        for owner in owners:
            release_owner_fn(owner, accepted_target=accepted_target)
        remaining_owners = tuple(owner for owner in list_owners_fn() if owner.source_pin_name == name)
        if any(
            (owner.publication_ref == accepted_target.ref and owner_is_live_fn(owner))
            or (owner.publication_ref != accepted_target.ref and candidate_is_live_fn(owner, accepted_target))
            for owner in remaining_owners
        ):
            continue
        if any(owner.source_pin_name == name for owner in remaining_owners):
            # A stale owner that could not be released is an ownership fence;
            # leave the alias for a later accepted-snapshot repair.
            continue
        if pin is not None:
            release_pin_fn(pin.name, pin.revision)


def _verify_published_stack_template_change(
    ref: str,
    candidate: Path,
    parent: str | None,
    source_pins: Mapping[str, str] | None = None,
) -> PublishedTree | None:
    """Check whether a failed publication actually reached its remote ref."""

    store = state_store()
    verify_with_owners = getattr(store, "verify_published_tree_with_owners", None)
    if callable(verify_with_owners) and source_pins:
        return cast(
            PublishedTree | None,
            verify_with_owners(ref, candidate, parent, source_pins),
        )
    verify = getattr(store, "verify_published_tree", None)
    if not callable(verify):
        return None
    return cast(PublishedTree | None, verify(ref, candidate, parent))


def _promote_stack_template_source_pins(
    environment: str,
    desired_root: Path,
    acquisition: ControllerPinAcquisition | None,
    accepted_target: AcceptedDesiredTarget | None = None,
) -> None:
    """Install canonical retention pins after a desired/candidate publication."""

    if acquisition is not None and acquisition.claims:
        store = state_store()
        required = dict(_required_stack_template_source_pins(environment, desired_root))
        promotion_error: BaseException | None = None
        try:
            store.create_controller_pins(required)
        except BaseException as exc:
            promotion_error = exc
        # The publication-owner refs were created in the same atomic push as
        # the publication. Claims are therefore released by exact name even
        # when the later canonical alias promotion fails.
        release_error: BaseException | None = None
        for claim in acquisition.claims:
            try:
                store.release_controller_pin(claim.name, claim.revision)
            except BaseException as exc:
                if release_error is None:
                    release_error = exc
        if promotion_error is not None:
            raise promotion_error
        if release_error is not None:
            raise release_error
    if accepted_target is not None:
        try:
            _gc_superseded_stack_workload_pins(environment, desired_root, accepted_target)
        except (OperationError, OSError, subprocess.CalledProcessError):
            log_status("KEEP", "accepted Stack workload source cleanup remains retryable")


def _stack_workload_pin_belongs_to_stack(name: str, environment: str, stack_name: str, stack_uid: str) -> bool:
    parts = name.split("/")
    safe_name = r"[a-z0-9][a-z0-9-]{0,62}"
    return (
        len(parts) == 8
        and parts[0] == "stack-templates"
        and re.fullmatch(safe_name, parts[1]) is not None
        and parts[1] == environment
        and re.fullmatch(safe_name, parts[2]) is not None
        and re.fullmatch(safe_name, parts[3]) is not None
        and parts[4] == "stacks"
        and re.fullmatch(safe_name, parts[5]) is not None
        and parts[5] == stack_name
        and re.fullmatch(safe_name, parts[6]) is not None
        and parts[6] == stack_uid
        and re.fullmatch(r"[0-9a-f]{40}", parts[7]) is not None
    )


def _release_finalized_stack_workload_pins(
    environment: str,
    name: str,
    uid: str,
    deletion_generation: int,
    desired_root: Path,
    accepted_target: AcceptedDesiredTarget | None = None,
) -> bool:
    """Release only workload pins owned by one finalized Stack incarnation."""

    tombstone = finalized_incarnation_evidence(
        desired_root,
        CORE_API_VERSION,
        "Stack",
        name,
        uid,
        deletion_generation,
    )
    if tombstone is None:
        return False
    resources = load_desired_resource_graph(desired_root, validate=False)
    if any(
        isinstance(item, StackResource) and item.gvk.kind == "Stack" and item.name == name and item.metadata.uid == uid
        for item in resources.values()
    ):
        raise OperationError(f"Stack {name!r} is still present after finalization")
    store = state_store()
    owners = tuple(
        owner
        for owner in getattr(store, "list_controller_publication_owners", lambda: ())()
        if _stack_workload_pin_belongs_to_stack(owner.source_pin_name, environment, name, uid)
    )
    canonical_pins = tuple(
        pin
        for pin in store.list_controller_pins()
        if not pin.name.startswith("claims/") and _stack_workload_pin_belongs_to_stack(pin.name, environment, name, uid)
    )
    missing_owner_pins = {
        owner.source_pin_name: owner.revision
        for owner in owners
        if not any(pin.name == owner.source_pin_name and pin.revision == owner.revision for pin in canonical_pins)
    }
    if missing_owner_pins:
        hydrate = getattr(store, "hydrate_source_revision", None)
        create_pins = getattr(store, "create_controller_pins", None)
        if not callable(hydrate) or not callable(create_pins):
            return False
        for source_pin_name, revision in missing_owner_pins.items():
            hydrate(source_pin_name, revision)
        create_pins(missing_owner_pins)
        canonical_pins = tuple(
            pin
            for pin in store.list_controller_pins()
            if not pin.name.startswith("claims/")
            and _stack_workload_pin_belongs_to_stack(pin.name, environment, name, uid)
        )
    for owner in owners:
        if accepted_target is None:
            if store.publication_owner_is_live(owner):
                return False
        elif owner.publication_ref == accepted_target.ref:
            if store.publication_owner_is_live(owner):
                return False
        elif store.publication_owner_is_live_candidate(owner, accepted_target):
            return False
        if not any(pin.name == owner.source_pin_name and pin.revision == owner.revision for pin in canonical_pins):
            return False
    for owner in owners:
        if accepted_target is None:
            store.release_publication_owner(owner)
        else:
            store.release_publication_owner(owner, accepted_target=accepted_target)
    if any(
        _stack_workload_pin_belongs_to_stack(owner.source_pin_name, environment, name, uid)
        for owner in getattr(store, "list_controller_publication_owners", lambda: ())()
    ):
        return False
    for pin in canonical_pins:
        current_owners = getattr(store, "list_controller_publication_owners", lambda: ())()
        if any(owner.source_pin_name == pin.name for owner in current_owners):
            continue
        store.release_controller_pin(pin.name, pin.revision)
    return bool(owners or canonical_pins)


def _release_finalized_stack_template_pins(
    environment: str,
    name: str,
    uid: str,
    deletion_generation: int,
    desired_root: Path,
    accepted_target: AcceptedDesiredTarget | None = None,
) -> bool:
    """Release pins only after the exact StackTemplate tombstone is durable."""

    tombstone = finalized_incarnation_evidence(
        desired_root,
        CORE_API_VERSION,
        "StackTemplate",
        name,
        uid,
        deletion_generation,
    )
    if tombstone is None:
        return False
    resources = load_desired_resource_graph(desired_root, validate=False)
    if any(
        isinstance(item, StackResource)
        and item.gvk.kind == "StackTemplate"
        and item.name == name
        and item.metadata.uid == uid
        for item in resources.values()
    ):
        raise OperationError(f"StackTemplate {name!r} is still present after finalization")
    references = [
        item.name
        for item in resources.values()
        if isinstance(item, StackResource)
        and item.gvk.kind == "Stack"
        and isinstance(item.spec, DesiredStackSpec)
        and item.spec.templateRef.name == name
        and item.spec.templateRef.uid == uid
    ]
    if references:
        raise OperationError("Stacks reference this StackTemplate: " + ", ".join(sorted(references)))
    prefix = _stack_template_source_pin_prefix(environment, name, uid)
    store = state_store()
    owners = tuple(
        owner
        for owner in getattr(store, "list_controller_publication_owners", lambda: ())()
        if owner.source_pin_name.startswith(prefix)
        and re.fullmatch(r"[0-9a-f]{40}", owner.source_pin_name.removeprefix(prefix)) is not None
    )
    canonical_pins = tuple(
        pin
        for pin in store.list_controller_pins()
        if pin.name.startswith(prefix)
        and not pin.name.startswith("claims/")
        and re.fullmatch(r"[0-9a-f]{40}", pin.name.removeprefix(prefix)) is not None
    )
    for owner in owners:
        if accepted_target is None:
            if store.publication_owner_is_live(owner):
                # Without the accepted target fence, publication ownership is
                # ambiguous and finalization must retain it.
                return False
        elif owner.publication_ref == accepted_target.ref:
            if store.publication_owner_is_live(owner):
                # The accepted desired publication still relies on this
                # incarnation. Nothing in finalization may remove its
                # ownership.
                return False
        elif store.publication_owner_is_live_candidate(owner, accepted_target):
            # A current gated proposal still relies on this incarnation.
            return False
        if not any(pin.name == owner.source_pin_name and pin.revision == owner.revision for pin in canonical_pins):
            # Without an accepted equivalent canonical owner, retain the
            # publication owner rather than creating a retention gap.
            return False
    for owner in owners:
        if accepted_target is None:
            store.release_publication_owner(owner)
        else:
            store.release_publication_owner(owner, accepted_target=accepted_target)
    remaining_owners = tuple(
        owner
        for owner in getattr(store, "list_controller_publication_owners", lambda: ())()
        if owner.source_pin_name.startswith(prefix)
    )
    if remaining_owners:
        return False
    for pin in canonical_pins:
        suffix = pin.name.removeprefix(prefix)
        if re.fullmatch(r"[0-9a-f]{40}", suffix) is None or suffix != pin.revision:
            raise OperationError(f"StackTemplate {name!r}: controller source pin has an invalid identity")
        # A publication-owner cleanup normally removes its canonical ref in
        # the same atomic transaction. Keep a canonical pin that another
        # publication owner still uses; claims are deliberately never part of
        # this cleanup path.
        current_owners = getattr(store, "list_controller_publication_owners", lambda: ())()
        if any(owner.source_pin_name == pin.name for owner in current_owners):
            continue
        store.release_controller_pin(pin.name, pin.revision)
    return bool(owners or canonical_pins)


def project_stack_resources(
    source_root: Path,
    environment_name: str,
    source_revision: str | None,
    candidate: Path,
    project_root: Path,
    current_desired: Path | None = None,
    promotion: PromotionContext | None = None,
    partition: str | None = None,
    source_context_root: Path | None = None,
    stack_template_document_digests: Mapping[str, str] | None = None,
    projection_context: JsonObject | None = None,
    stack_names: frozenset[str] | None = None,
    authenticated_workload_revisions: frozenset[AuthenticatedStackWorkloadRevision] = frozenset(),
) -> StackProjection:
    """Build desired inline StackTemplate roots and project every Stack."""

    (candidate / "stack-templates").mkdir(parents=True, exist_ok=True)
    (candidate / "stacks").mkdir(parents=True, exist_ok=True)
    authored_templates, authored_stacks = _load_authored_stack_resources(source_root, environment_name)
    if projection_context is None and current_desired is None and (authored_templates or authored_stacks):
        projection_context = capture_projection_context(source_root, environment_name, promotion)
        write_projection_context(candidate, projection_context)
    if source_revision is None and projection_context is None and current_desired is not None:
        # Durable progression materializes authored-shaped files only to
        # satisfy path/configuration helpers.  The desired Stack and
        # StackTemplate documents remain the authoritative projection inputs.
        authored_templates = {}
        authored_stacks = {}
    project = load_project_config(source_root)
    template_root = source_root.joinpath(*project.stack_templates_path.parts)
    authored_template_paths = _document_paths(template_root)

    current_resources: dict[tuple[str, str, str], UnitResource[Any] | StackResource] = {}
    if current_desired is not None and current_desired.exists() and any(current_desired.iterdir()):
        current_resources = load_desired_resource_graph(current_desired)
    current_templates = {
        resource.name: resource
        for resource in current_resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate"
    }
    current_stacks = {
        resource.name: resource
        for resource in current_resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack"
    }

    desired_templates: dict[str, StackResource] = dict(current_templates)
    explicit_template_names = set(authored_templates)
    template_source_roots: dict[str, Path] = {}
    template_source_transports: dict[str, StackUnitSourceTransport] = {
        template.name: StackUnitSourceTransport(
            repository=template.spec.sourceContext.repository,
            ref=template.spec.sourceContext.revision,
        )
        for template in desired_templates.values()
        if isinstance(template.spec, DesiredStackTemplateSpec) and template.spec.sourceContext is not None
    }
    authenticated_revision_set = set(authenticated_workload_revisions)
    for name, authored in authored_templates.items():
        authored_path = authored_template_paths.get(name)
        if authored_path is None:
            raise OperationError(f"direct StackTemplate input {name!r} was not materialized")
        acquisition: StackTemplateAcquisition
        source_context: StackTemplateSourceContext | None
        if isinstance(authored.spec, StackTemplateInlineSpec):
            inline_spec = authored.spec
            digest = (
                stack_template_document_digests.get(name, _stack_template_document_digest(authored_path))
                if stack_template_document_digests is not None
                else _stack_template_document_digest(authored_path)
            )
            acquisition = StackTemplateAcquisition(
                documentDigest=digest,
                requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
                resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
            )
            source_context = None
            if _template_has_repository_sources(inline_spec):
                if source_revision is not None:
                    source_context = StackTemplateSourceContext(repository=".", revision=source_revision)
                else:
                    previous = current_templates.get(name)
                    if (
                        previous is not None
                        and isinstance(previous.spec, DesiredStackTemplateSpec)
                        and previous.spec.contentDigest == inline_spec.semantic_content_digest()
                        and previous.spec.sourceContext is not None
                    ):
                        source_context = previous.spec.sourceContext
                    else:
                        raise OperationError(
                            f"StackTemplate {name!r} contains repository-backed Unit sources; "
                            "apply it with --source-revision <commit>"
                        )
        elif isinstance(authored.spec, StackTemplateGitSpec):
            acquired = _acquire_git_stack_template(authored, name, candidate)
            inline_spec = acquired.inline_spec
            acquisition = acquired.acquisition
            source_context = acquired.source_context
            template_source_roots[name] = acquired.source_root
            template_source_transports[name] = StackUnitSourceTransport(
                repository=authored.spec.source.fromGit.repository,
                # The acquisition already resolved the authored selector. Fence
                # workload overrides to that exact acquired history rather than
                # re-reading a mutable branch or tag later in this transaction.
                ref=acquired.source_context.revision,
            )
        elif isinstance(authored.spec, StackTemplatePromotionSpec):
            acquired = _acquire_promoted_stack_template(
                authored,
                name,
                promotion,
                authenticated_revision_set,
            )
            inline_spec = acquired.inline_spec
            acquisition = acquired.acquisition
            source_context = acquired.source_context
        else:
            raise OperationError(f"StackTemplate {name!r} has an invalid authored specification")
        if source_context is not None and not isinstance(authored.spec, StackTemplateGitSpec):
            template_source_transports[name] = StackUnitSourceTransport(
                repository=source_context.repository,
                ref=source_context.revision,
            )
        desired_templates[name] = StackResource(
            authored.gvk,
            _stack_root_metadata("StackTemplate", name, source_revision, current_desired, partition),
            DesiredStackTemplateSpec(
                parameters=list(inline_spec.parameters),
                unitTemplates=dict(inline_spec.unitTemplates),
                contentDigest=inline_spec.semantic_content_digest(),
                acquisition=acquisition,
                sourceContext=source_context,
            ),
        )

    for name in explicit_template_names:
        template = desired_templates[name]
        _write_desired_stack_resource(
            candidate / "stack-templates" / f"{template.name}.json",
            template,
            project_root,
        )

    desired_stacks: dict[str, StackResource] = dict(current_stacks)
    explicit_stack_names = set(authored_stacks)
    for name, authored in authored_stacks.items():
        if not isinstance(authored.spec, StackSpec):
            raise OperationError(f"Stack {name!r} has an invalid authored specification")
        desired_stacks[name] = StackResource(
            authored.gvk,
            _stack_root_metadata("Stack", name, source_revision, current_desired, partition),
            authored.spec,
        )

    # Only explicitly supplied Stacks and current Stacks referring to an
    # explicitly updated template are reprojected. All other roots and owned
    # Units are copied later without parsing or rewriting them.
    reprojected_stack_names = set(explicit_stack_names)
    if stack_names is not None:
        reprojected_stack_names.update(stack_names)
    for name, stack in current_stacks.items():
        if name in reprojected_stack_names or resource_deletion(stack) is not None:
            continue
        if not isinstance(stack.spec, DesiredStackSpec):
            continue
        if stack.spec.templateRef.name in explicit_template_names:
            reprojected_stack_names.add(name)
    if stack_names is not None:
        reprojected_stack_names.intersection_update(stack_names)

    generated: dict[str, UnitResource[Any]] = {}
    owners: dict[str, DesiredOwnerReference] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    artifact_imports: dict[str, tuple[ArtifactImport, ...]] = {}
    source_contexts: dict[str, StackUnitSourceContext] = {}
    structural_projections: dict[str, StructuralStackProjection] = {}
    workload_source_checkouts: dict[tuple[str, str, str], Path] = {}

    for name in sorted(reprojected_stack_names):
        stack = desired_stacks[name]
        if resource_deletion(stack) is not None:
            # An explicitly applied deleting Stack is rejected by
            # _stack_root_metadata; a carried deleting Stack is not rebuilt.
            continue
        if not isinstance(stack.spec, (StackSpec, DesiredStackSpec)):
            raise OperationError(f"Stack {name!r} has an invalid specification")
        template_name = stack.spec.template if isinstance(stack.spec, StackSpec) else stack.spec.templateRef.name
        template = desired_templates.get(template_name)
        if template is None:
            raise OperationError(
                f"Stack {name!r} references missing desired StackTemplate {template_name!r} in environment "
                f"{environment_name!r}"
            )
        if resource_deletion(template) is not None:
            if name in explicit_stack_names:
                raise OperationError(f"Stack {name!r} references deleting StackTemplate {template_name!r}")
            continue
        if not isinstance(template.spec, DesiredStackTemplateSpec):
            raise OperationError(f"StackTemplate {template_name!r} is not a desired StackTemplate")
        if stack.metadata.uid is None or template.metadata.uid is None:
            raise OperationError(f"Stack {name!r} and StackTemplate {template_name!r} must have UIDs")

        expanded = template.spec.expand(stack.spec.parameters)
        selected_names = set(stack.spec.units or (resource.name for resource in expanded))
        known_names = {resource.name for resource in expanded}
        unknown = sorted(selected_names - known_names)
        if unknown:
            raise OperationError(f"Stack {name!r} selects unknown Unit templates: {', '.join(unknown)}")
        for resource in expanded:
            if resource.name not in selected_names:
                continue
            omitted = sorted(set(resource.dependsOn) - selected_names)
            if omitted:
                raise OperationError(
                    f"Stack {name!r} selects {resource.name!r} but omits dependencies: {', '.join(omitted)}"
                )
        expanded = tuple(resource for resource in expanded if resource.name in selected_names)
        inherited_root, inherited_revision = _materialize_template_source_context(
            template,
            template_source_roots.get(template.name, source_context_root or source_root),
            source_revision,
            candidate,
            environment_name,
        )
        source_context = template.spec.sourceContext
        authenticated_context = (
            AuthenticatedStackTemplateContext(source_context.repository, source_context.revision)
            if source_context is not None and inherited_revision == source_context.revision
            else None
        )
        normalized_specs: dict[str, NormalizedStackUnitSource] = {}
        if source_context is not None:
            for resource in expanded:
                normalized_specs[resource.name] = _normalize_stack_unit_source(
                    resource.spec,
                    repository=source_context.repository,
                    inherited_revision=source_context.revision,
                    inherited_root=inherited_root,
                    candidate=candidate,
                    checkout_cache=workload_source_checkouts,
                    retention_pin_prefix=(
                        f"{_stack_template_source_pin_prefix(environment_name, template.name, template.metadata.uid)}"
                        f"stacks/{name}/{stack.metadata.uid}/"
                        if template.metadata.uid is not None and stack.metadata.uid is not None
                        else None
                    ),
                    transport=template_source_transports.get(template.name),
                    authenticated_revisions=frozenset(authenticated_revision_set),
                    authenticated_context=authenticated_context,
                )
        projection_units = {
            resource.name: StackProjectionUnit(
                apiVersion=resource.apiVersion,
                kind=resource.kind,
                spec=ProjectionObject(
                    normalized_specs.get(
                        resource.name,
                        NormalizedStackUnitSource(
                            spec=cast(dict[str, Any], dump_template_value(cast(TemplateValue, resource.spec))),
                            source_context=None,
                        ),
                    ).spec
                ),
                dependsOn=list(resource.dependsOn),
            )
            for resource in expanded
        }
        bound_context_digest: str
        if (
            projection_context is not None
            and promotion is None
            and isinstance(stack.spec, DesiredStackSpec)
            and current_desired is not None
        ):
            # An explicitly supplied Stack is a new operation root and must
            # bind to this operation's immutable Project/Environment record.
            # A Stack reached only through template fan-out is carried state;
            # preserve its independent context and promotion lineage.
            if name in explicit_stack_names:
                bound_context_digest = cast(str, projection_context["digest"])
                context_root = candidate
            else:
                bound_context_digest = stack.spec.structuralProjection.identity.projectionContextDigest
                context_root = current_desired
            load_projection_context(context_root, bound_context_digest, environment_name)
        elif projection_context is not None:
            bound_context_digest = cast(str, projection_context["digest"])
        elif isinstance(stack.spec, DesiredStackSpec):
            bound_context_digest = stack.spec.structuralProjection.identity.projectionContextDigest
            load_projection_context(
                current_desired if current_desired is not None else project_root,
                bound_context_digest,
                environment_name,
            )
        else:
            raise OperationError(
                f"Stack {stack.name!r} has no persisted projection context binding; refusing to fabricate one"
            )
        projection = StructuralStackProjection.build(
            stack_uid=stack.metadata.uid,
            template_uid=template.metadata.uid,
            template_content_digest=template.spec.contentDigest,
            units=projection_units,
            context_digest=bound_context_digest,
        )
        desired_stack = StackResource(
            stack.gvk,
            stack.metadata,
            DesiredStackSpec(
                templateRef=StackTemplateReference(
                    name=template.name,
                    uid=template.metadata.uid,
                    contentDigest=template.spec.contentDigest,
                ),
                parameters=stack.spec.parameters,
                units=stack.spec.units,
                artifactImports=stack.spec.artifactImports,
                structuralProjection=projection,
                activeProjection=(stack.spec.activeProjection if isinstance(stack.spec, DesiredStackSpec) else None),
            ),
        )
        structural_projections[name] = projection
        desired_stacks[name] = desired_stack
        _write_desired_stack_resource(candidate / "stacks" / f"{name}.json", desired_stack, project_root)

        scoped_resources = scope_stack_template_resources(
            name,
            tuple(
                StackTemplateResource(
                    apiVersion=item.apiVersion,
                    kind=item.kind,
                    name=item.name,
                    spec=item.spec,
                    dependsOn=item.dependsOn,
                )
                for item in expanded
            ),
        )
        for original_resource, resource in zip(expanded, scoped_resources, strict=True):
            qualified_name = stack_generated_unit_name(name, resource.name)
            normalized = normalized_specs.get(
                original_resource.name,
                NormalizedStackUnitSource(
                    spec=cast(dict[str, Any], dump_template_value(cast(TemplateValue, resource.spec))),
                    source_context=None,
                ),
            )
            normalized_spec = cast(dict[str, Any], dump_template_value(cast(TemplateValue, resource.spec)))
            normalized_source = normalized.spec.get("source")
            if normalized_source is not None:
                normalized_spec = dict(normalized_spec)
                normalized_spec["source"] = normalized_source
            selected_source_context = normalized.source_context
            document: JsonObject = {
                "apiVersion": resource.apiVersion,
                "kind": resource.kind,
                "metadata": {"name": resource.name},
                "spec": cast(JsonObjectValue, normalized_spec),
            }
            unit = RESOURCE_CATALOG.parse_unit(document, profile="authored", expected_name=resource.name)
            require_unit_specification(unit, resource.name)
            if qualified_name in generated:
                raise OperationError(f"generated Unit {qualified_name!r} is produced more than once")
            generated[qualified_name] = unit
            dependencies[qualified_name] = tuple(
                stack_generated_unit_name(name, dependency) for dependency in resource.dependsOn
            )
            artifact_imports[qualified_name] = tuple(stack.spec.artifactImports)
            owners[qualified_name] = DesiredOwnerReference(
                apiVersion=stack.gvk.api_version,
                kind=stack.gvk.kind,
                name=stack.name,
                uid=stack.metadata.uid,
            )
            if selected_source_context is not None:
                source_contexts[qualified_name] = selected_source_context

    return StackProjection(
        generated_units=generated,
        owners=owners,
        dependencies=dependencies,
        artifact_imports=artifact_imports,
        source_contexts=source_contexts,
        applied_stacks=frozenset(reprojected_stack_names),
        structural_projections=structural_projections,
    )


@dataclass(frozen=True)
class ConvergenceSpecifications:
    units: dict[str, UnitResource[Any]]
    dependencies: dict[str, tuple[str, ...]]
    qualified_names: dict[str, str]


def load_convergence_specifications(
    source_root: Path,
    environment_name: str,
    current_desired: Path,
    projection_revision: str,
    projection_root: Path,
) -> ConvergenceSpecifications:
    """Load source and desired-only Units participating in convergence.

    Source Unit documents remain the authored authority. Stack-generated and
    already-applied Units are added from the desired snapshot for inspection.
    """

    specifications = load_environment_specifications(source_root, environment_name)
    dependency_edges: dict[str, tuple[str, ...]] = {}
    qualified_names = {name: name for name in specifications}
    if _current_desired_stack_paths(current_desired, "Stack"):
        # A desired Stack is an immutable projection. Reconcile must not
        # rebuild it from a mutable source branch or remote repository.
        resources = load_desired_resource_graph(current_desired)
        qualified_names.update(
            {concrete: qualified for qualified, concrete in qualified_unit_name_map(resources).items()}
        )
        dependency_edges.update(stack_dependency_edges(resources))
        transition_blocks = load_desired_transition_blocks(current_desired)
        for key, resource in resources.items():
            if not isinstance(resource, UnitResource) or resource_deletion(resource) is not None:
                continue
            unit_name = key[2]
            if unit_name in transition_blocks:
                continue
            owner = resource_owner_reference(resource)
            is_stack_owned = owner is not None and owner.kind == "Stack"
            if not (is_stack_owned or owner is None):
                continue
            existing = specifications.get(unit_name)
            if existing is not None and existing.gvk != resource.gvk:
                raise OperationError(f"desired-only Unit {unit_name!r} collides with a source Unit")
            specifications[unit_name] = resource
    else:
        projection = project_stack_resources(
            source_root,
            environment_name,
            projection_revision,
            projection_root,
            source_root,
            current_desired,
        )
        for name, generated in projection.generated_units.items():
            if name in specifications:
                raise OperationError(f"generated Stack Unit {name!r} collides with a source Unit")
            specifications[name] = generated
        for stack_name, structural in projection.structural_projections.items():
            for logical_name in structural.units:
                concrete = stack_generated_unit_name(stack_name, logical_name)
                if concrete in projection.generated_units:
                    qualified_names[concrete] = f"{stack_name}/{logical_name}"
        dependency_edges.update(projection.dependencies)

    return ConvergenceSpecifications(
        specifications,
        {name: tuple(sorted(values)) for name, values in dependency_edges.items()},
        qualified_names,
    )


def reconciliation_statuses(unit_names: Sequence[str], desired: Path, observed: Path) -> list[tuple[str, str, str]]:
    transition_blocks = load_desired_transition_blocks(desired)
    resources = load_desired_resource_graph(desired, validate=False)
    deleting = {
        resource.name: resource
        for resource in resources.values()
        if isinstance(resource, UnitResource) and resource_deletion(resource) is not None
    }
    cleanup_names = {path.stem for path in desired_cleanup_root_paths(desired)}
    unit_names = tuple(dict.fromkeys((*unit_names, *sorted(cleanup_names), *sorted(deleting))))
    statuses = []
    for unit_name in unit_names:
        unit_path = unit_document_path(desired, unit_name)
        receipt_path = receipt_document_path(observed, unit_name)
        if unit_name in deleting:
            state = operational.classify_before_observation(
                desired,
                unit_name,
                None,
                deleting[unit_name],
            )
            assert state is not None
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        if unit_name in transition_blocks:
            state = operational.classify_before_observation(
                desired,
                unit_name,
                None,
                None,
                transition_blocks[unit_name],
            )
            assert state is not None
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        if not unit_path.is_file():
            state = operational.classify_before_observation(desired, unit_name, None, None)
            assert state is not None
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        document = load_json(unit_path)
        if raw_unit_contains_reference(document):
            state = operational.classify_before_observation(desired, unit_name, document, None)
            assert state is not None
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        unit = load_desired_unit(unit_path, unit_name)
        state = operational.classify_before_observation(desired, unit_name, document, unit)
        if state is not None:
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        if not receipt_path.is_file():
            state = operational.classify_observation(operational.ObservationEvidence.MISSING)
            statuses.append((unit_name, state.reconciliation.value, state.reason))
            continue
        receipt = load_receipt(receipt_path, unit_name)
        if receipt.spec.desired.unitBlob == file_blob(unit_path):
            validate_receipt_artifacts(observed, unit, receipt)
            state = operational.classify_observation(operational.ObservationEvidence.CURRENT)
        else:
            state = operational.classify_observation(operational.ObservationEvidence.STALE)
        statuses.append((unit_name, state.reconciliation.value, state.reason))
    return statuses


def desired_cleanup_root_paths(root: Path) -> tuple[Path, ...]:
    cleanup_root = root / DESIRED_CLEANUP_UNITS_PATH
    if not cleanup_root.is_dir():
        return ()
    paths = sorted(
        path for path in cleanup_root.iterdir() if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}
    )
    by_name: dict[str, Path] = {}
    for path in paths:
        previous = by_name.get(path.stem)
        if previous is not None:
            raise OperationError(
                f"multiple cleanup document formats exist for {path.stem!r}: {previous.name} and {path.name}"
            )
        by_name[path.stem] = path
    return tuple(paths)


def load_desired_transition_blocks(root: Path) -> dict[str, str]:
    return operational.load_desired_transition_blocks(root)


def write_desired_transition_blocks(root: Path, blocks: Mapping[str, str]) -> None:
    path = root / DESIRED_TRANSITION_BLOCKS_PATH
    if not blocks:
        if path.is_file():
            path.unlink()
        return
    write_document(
        path,
        {"schema": 1, "blocks": dict(sorted(blocks.items()))},
        format=DocumentFormat.JSON,
    )


def changed_json_paths(previous: Any, current: Any, prefix: str = "") -> list[str]:
    if isinstance(previous, dict) and isinstance(current, dict):
        paths = []
        for key in sorted(set(previous) | set(current)):
            path = f"{prefix}/{key}"
            if key not in previous or key not in current:
                paths.append(path)
            else:
                paths.extend(changed_json_paths(previous[key], current[key], path))
        return paths
    if isinstance(previous, list) and isinstance(current, list):
        if previous == current:
            return []
        return [prefix or "/"]
    return [] if previous == current else [prefix or "/"]


def unit_source_paths(source: dict[str, Any]) -> list[str]:
    source_path = source.get("path")
    if not isinstance(source_path, str):
        return []
    inputs = source.get("inputs")
    if not isinstance(inputs, list):
        return [source_path]
    paths = []
    for value in inputs:
        path = str(PurePosixPath(source_path) / value)
        paths.append(f":(glob){path}" if globlib.has_magic(value) else path)
    return paths


def source_change_evidence(
    previous_source: dict[str, Any], current_source: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    previous_revision = previous_source.get("revision")
    current_revision = current_source.get("revision")
    if not isinstance(previous_revision, str) or not isinstance(current_revision, str):
        return (), ()
    paths = unit_source_paths(current_source)
    if not paths:
        return (), ()
    diff = git(
        "diff",
        "--name-status",
        previous_revision,
        current_revision,
        "--",
        *paths,
        check=False,
    )
    files = tuple(line for line in diff.stdout.splitlines() if line)
    history = git(
        "log",
        "--format=%h %s",
        "--no-merges",
        f"{previous_revision}..{current_revision}",
        "--",
        *paths,
        check=False,
    )
    commits = tuple(line for line in history.stdout.splitlines() if line)
    return commits, files


def classify_unit_change(
    previous: UnitResource[Any],
    current: UnitResource[Any],
    previous_desired_revision: str,
) -> UnitChangeExplanation:
    previous_source = getattr(previous.spec, "source", None)
    current_source = getattr(current.spec, "source", None)
    causes = []
    if previous.driver_name != current.driver_name or getattr(previous_source, "driverVersion", None) != getattr(
        current_source, "driverVersion", None
    ):
        causes.append("reconciliation driver changed")
    source_fingerprint_changed = getattr(previous_source, "inputHash", None) != getattr(
        current_source, "inputHash", None
    )
    commits, files = (
        source_change_evidence(
            previous_source.to_dict() if previous_source is not None else {},
            current_source.to_dict() if current_source is not None else {},
        )
        if source_fingerprint_changed
        else ((), ())
    )
    if files:
        causes.append("source inputs changed")
    previous_inputs_model = getattr(previous.spec, "resolvedInputs", None)
    current_inputs_model = getattr(current.spec, "resolvedInputs", None)
    previous_inputs = previous_inputs_model.to_dict() if previous_inputs_model is not None else {}
    current_inputs = current_inputs_model.to_dict() if current_inputs_model is not None else {}
    previous_observed: dict[str, Any] = {}
    current_observed: dict[str, Any] = {}
    for category in ("receipts", "artifacts"):
        previous_category = previous_inputs.get(category, {})
        current_category = current_inputs.get(category, {})
        if isinstance(previous_category, dict):
            previous_observed.update(previous_category)
        if isinstance(current_category, dict):
            current_observed.update(current_category)
    if previous_observed != current_observed:
        changed = sorted(set(previous_observed) | set(current_observed))
        causes.append("upstream observations changed: " + ", ".join(Path(path).stem for path in changed))
    previous_promotion = previous_inputs.get("promotions", {})
    current_promotion = current_inputs.get("promotions", {})
    if not isinstance(previous_promotion, dict):
        previous_promotion = {}
    if not isinstance(current_promotion, dict):
        current_promotion = {}
    if previous_promotion != current_promotion:
        changed = sorted(
            key
            for key in set(previous_promotion) | set(current_promotion)
            if previous_promotion.get(key) != current_promotion.get(key)
        )
        causes.append("reviewed promotion inputs changed: " + ", ".join(changed))
    ignored = {"source", "resolvedInputs"}
    previous_document = previous.driver.desired_unit_contract.dump(previous.spec)
    current_document = current.driver.desired_unit_contract.dump(current.spec)
    previous_specification = {key: value for key, value in previous_document.items() if key not in ignored}
    current_specification = {key: value for key, value in current_document.items() if key not in ignored}
    specification_paths = tuple(changed_json_paths(previous_specification, current_specification))
    if specification_paths:
        causes.append("unit specification changed")
    if source_fingerprint_changed and not files and not causes:
        causes.append("source input fingerprint changed")
    if not causes:
        causes.append("desired unit content changed")
    return UnitChangeExplanation(
        previous_desired_revision=previous_desired_revision,
        previous_source_revision=getattr(previous_source, "revision", None),
        current_source_revision=getattr(current_source, "revision", None),
        causes=tuple(causes),
        commits=commits,
        files=files,
        specification_paths=specification_paths,
    )


def unit_change_explanation(unit_name: str, desired: Path, observed: Path) -> UnitChangeExplanation | None:
    receipt_path = receipt_document_path(observed, unit_name)
    current_path = unit_document_path(desired, unit_name)
    if not receipt_path.is_file() or not current_path.is_file():
        return None
    receipt = load_receipt(receipt_path, unit_name)
    previous_revision = receipt.spec.desired.revision
    if not isinstance(previous_revision, str):
        return None
    previous_result = git("show", f"{previous_revision}:units/{unit_name}.json", check=False)
    if previous_result.returncode != 0:
        return None
    try:
        previous = json.loads(previous_result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(previous, dict):
        return None
    try:
        previous_unit = RESOURCE_CATALOG.parse_unit(
            cast(JsonObject, previous), profile="desired", expected_name=unit_name
        )
    except OperationError:
        return None
    return classify_unit_change(previous_unit, load_desired_unit(current_path, unit_name), previous_revision)


def style_commit_evidence(value: str, stream: TextIO | None = None) -> str:
    match = re.match(r"^(?P<revision>[0-9a-f]+)(?: (?P<subject>.*))?$", value)
    if match is None:
        return style_text(value, "muted", stream)
    revision = style_text(match.group("revision"), "revision", stream)
    subject = match.group("subject")
    return f"{revision} {style_text(subject, 'muted', stream)}" if subject else revision


def log_bounded_items(
    status: str,
    values: tuple[str, ...],
    verbose: bool,
    formatter: Callable[[str], str] | None = None,
) -> None:
    limit = len(values) if verbose else 5
    for value in values[:limit]:
        log_status(status, formatter(value) if formatter is not None else value)
    if len(values) > limit:
        log_status(status, f"... and {len(values) - limit} more; use --verbose to show all")


def log_unit_change_explanation(
    unit_name: str,
    desired_revision: str,
    desired: Path,
    observed: Path,
    verbose: bool,
) -> None:
    explanation = unit_change_explanation(unit_name, desired, observed)
    if explanation is None:
        log_status("CAUSE", "no prior desired unit is available for comparison")
        return
    log_status(
        "LAST",
        f"desired {describe_revision(explanation.previous_desired_revision)}; "
        f"source {describe_revision(explanation.previous_source_revision)}",
    )
    log_status(
        "CURRENT",
        f"desired {describe_revision(desired_revision)}; source {describe_revision(explanation.current_source_revision)}",
    )
    for cause in explanation.causes:
        log_status("CAUSE", cause)
    log_bounded_items("COMMIT", explanation.commits, verbose, style_commit_evidence)
    log_bounded_items("FILE", explanation.files, verbose)
    log_bounded_items("FIELD", explanation.specification_paths, verbose)


def log_reconciliation_status(
    environment_name: str,
    statuses: list[tuple[str, str, str]],
    desired_revision: str | None = None,
    desired: Path | None = None,
    observed: Path | None = None,
    verbose: bool = False,
) -> None:
    log_heading(f"Reconciliation status for {style_environment(environment_name)}")
    for unit_name, status, reason in statuses:
        log_status(status, f"{style_unit(unit_name)}: {reason}")
        if status == "READY" and desired_revision is not None and desired is not None and observed is not None:
            log_unit_change_explanation(unit_name, desired_revision, desired, observed, verbose)
    ready = [unit_name for unit_name, status, _ in statuses if status == "READY"]
    if ready:
        log_status("NEXT", style_units(ready))
    elif any(status == "WAIT" for _, status, _ in statuses):
        log_status("NEXT", "none ready; waiting for upstream observations")
    elif any(status == "MATERIALIZED" for _, status, _ in statuses):
        log_status("NEXT", "none; all units are complete")
    else:
        log_status("NEXT", "none; all units are clean")


def bounded_evidence(values: tuple[str, ...]) -> str | None:
    if not values:
        return None
    remainder = f" (+{len(values) - 1} more)" if len(values) > 1 else ""
    return values[0] + remainder


def log_convergence_action(
    unit_name: str,
    reason: str,
    desired_revision: str,
    desired: Path,
    observed: Path,
    observed_ref: str,
) -> None:
    unit = load_desired_unit(unit_document_path(desired, unit_name), unit_name)
    driver = unit.driver_name
    explanation = unit_change_explanation(unit_name, desired, observed)
    log_heading(f"Next action: {style_unit(unit_name)}")
    log_status("DRIVER", driver)
    if explanation is None:
        log_status("CAUSE", reason)
    else:
        if explanation.previous_source_revision or explanation.current_source_revision:
            log_status(
                "SOURCE",
                f"{describe_revision(explanation.previous_source_revision)} -> "
                f"{describe_revision(explanation.current_source_revision)}",
            )
        for cause in explanation.causes:
            log_status("CAUSE", cause)
        if commit := bounded_evidence(explanation.commits):
            log_status("COMMIT", style_commit_evidence(commit))
        if file := bounded_evidence(explanation.files):
            log_status("FILE", file)
        if field := bounded_evidence(explanation.specification_paths):
            log_status("FIELD", field)
    log_status("WRITES", f"driver effects; receipt to {style_branch(observed_ref)} on success")


def materialize_resolved_unit(
    environment_name: str,
    resolved: UnitResource[Any],
    qualified_name: str,
    source_root: Path,
    source_revision: str | None,
    current_desired: Path,
    candidate: Path,
) -> UnitResource[Any]:
    unit_name = qualified_name
    plugin_name = resolved.driver_name
    plugin = MATERIALIZATION_DRIVERS.get(plugin_name)
    if plugin is None:
        return resolved

    previous_path = unit_document_path(current_desired, unit_name)
    if previous_path.is_file():
        previous = load_desired_unit(previous_path, unit_name)
        if previous.driver_name == plugin_name and plugin.resolved_from_desired(previous.spec) == resolved.spec:
            validate_unit_materialization(current_desired, unit_name, previous)
            copy_unit_materialization(current_desired, candidate, unit_name, previous)
            previous_descriptor = previous.spec.materialization
            previous_metadata = previous_descriptor.metadata
            descriptor = MaterializationDocument(
                path=previous_descriptor.path,
                digest=previous_descriptor.digest,
                mediaType=previous_descriptor.mediaType,
                metadata=JsonObjectValue(
                    previous_metadata.to_dict() if hasattr(previous_metadata, "to_dict") else dict(previous_metadata)
                ),
            )
            return resolved.with_spec(plugin.finalize_materialization(resolved.spec, descriptor))

    output_root = candidate / "materialized" / unit_name
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    source = resolved.spec.source
    selected_revision = source.revision
    source_path = source.path
    if not isinstance(selected_revision, str) or not isinstance(source_path, str):
        raise OperationError(f"{unit_name} has an invalid materialization source")

    def run_materializer(selected_source_root: Path) -> MaterializationResult:
        result = plugin.materialize(
            MaterializationContext(
                environment=environment_name,
                source_root=selected_source_root,
                source_revision=selected_revision,
                source_path=source_path,
                unit_name=resolved.name,
                unit=resolved.spec,
                output_root=output_root,
                qualified_name=unit_name,
                execution=DriverExecution.console(),
            )
        )
        if not isinstance(result, MaterializationResult):
            raise DriverError(f"{plugin_name} returned an invalid materialization result")
        return result

    if selected_revision == source_revision:
        result = run_materializer(source_root)
    else:
        with tempfile.TemporaryDirectory(prefix="gitopsctr-materialization-source-") as directory:
            selected_source_root = Path(directory) / "source"
            materialize_revision(selected_revision, selected_source_root)
            result = run_materializer(selected_source_root)
    if not result.media_type:
        raise DriverError(f"{plugin_name} returned an empty materialization media type")
    descriptor = MaterializationDocument(
        path=f"materialized/{unit_name}",
        digest=materialization_tree_digest(output_root),
        mediaType=result.media_type,
        metadata=JsonObjectValue(result.metadata),
    )
    desired = resolved.with_spec(plugin.finalize_materialization(resolved.spec, descriptor))
    validate_unit_materialization(candidate, unit_name, desired)
    return desired


def carry_forward_refreshed_unit(
    project_root: Path,
    current_desired: Path,
    candidate: Path,
    candidate_units: Path,
    authored: UnitResource[Any],
    source_resolution: ResolvedUnitSourceResult,
) -> bool:
    """Carry a validated desired Unit across a provenance-only source refresh."""

    if source_resolution.disposition is not SourceResolutionDisposition.REVISION_REFRESHED:
        return False
    source = source_resolution.source
    previous_path = unit_document_path(current_desired, authored.name)
    if source is None or not previous_path.is_file():
        return False
    try:
        previous = load_desired_unit(previous_path, authored.name)
        previous_source = getattr(previous.spec, "source", None)
        authored_source = getattr(authored.spec, "source", None)
        if (
            previous.driver_name != authored.driver_name
            or previous.gvk != authored.gvk
            or not isinstance(previous_source, DesiredSource)
            or not isinstance(authored_source, AuthoredSource)
            or previous_source.inputHash != source.inputHash
            or previous_source.path != source.path
            or previous_source.inputs != source.inputs
            or previous_source.driverVersion != source.driverVersion
            or source.driverVersion != DRIVER_VERSIONS[authored.driver_name]
            or authored_source.path != source.path
            or authored_source.inputs != source.inputs
            or unit_contains_reference(previous)
        ):
            return False
        validate_unit_materialization(current_desired, authored.name, previous)
        carried = previous.with_spec(replace(previous.spec, source=source))
        copy_unit_materialization(current_desired, candidate, authored.name, previous)
        validate_unit_materialization(candidate, authored.name, carried)
        write_desired_candidate_unit(candidate_units / f"{authored.name}.json", carried, project_root)
    except (DriverError, OperationError, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class BuildDesiredResult:
    """Outcome of desired-state construction, including units blocked by unavailable inputs."""

    blocked: Mapping[str, str]
    cleanup_inputs: Mapping[str, DesiredCleanupInput] = field(default_factory=dict)
    blocked_transitions: Mapping[str, str] = field(default_factory=dict)
    refreshes: Mapping[str, str] = field(default_factory=dict)
    reprojected_stacks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DesiredCleanupInput:
    """Retained source identity needed before a source-absent unit can be finalized."""

    unit_name: str
    desired: UnitResource[Any] | None
    source: DesiredSource | None
    raw_document: JsonObject | None = None


@dataclass(frozen=True)
class OpaqueCleanupRoot:
    """Unparseable desired input retained outside executable desired Units."""

    path: Path
    payload: object
    metadata: ResourceMetadata
    source: DesiredSource | None


@dataclass(frozen=True, kw_only=True)
class ResourceIncarnationTombstone:
    """Durable fence for one finalized desired resource incarnation."""

    api_version: str
    kind: str
    name: str
    uid: str
    deletion_generation: int
    qualified_name: str | None = None
    partition: str | None = None
    effect_lease_ref: str | None = None

    def __post_init__(self) -> None:
        if self.qualified_name is None:
            object.__setattr__(self, "qualified_name", self.name)

    def document(self) -> JsonObject:
        document: JsonObject = {
            "schema": 1,
            "kind": "ResourceIncarnationTombstone",
            "resource": {
                "apiVersion": self.api_version,
                "kind": self.kind,
                "name": self.name,
                "uid": self.uid,
                "deletionGeneration": self.deletion_generation,
                "qualifiedName": self.qualified_name or self.name,
            },
        }
        if self.partition is not None:
            cast(dict[str, object], document["resource"])["partition"] = self.partition
        cast(dict[str, object], document["resource"])["effectLeaseRef"] = self.effect_lease_ref
        return document

    @classmethod
    def from_document(cls, document: object) -> ResourceIncarnationTombstone:
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "kind", "resource"}
            or document.get("schema") != 1
            or document.get("kind") != "ResourceIncarnationTombstone"
        ):
            raise ValueError("invalid resource incarnation tombstone")
        resource = document.get("resource")
        required = {
            "apiVersion",
            "kind",
            "name",
            "uid",
            "deletionGeneration",
            "qualifiedName",
            "effectLeaseRef",
        }
        allowed = required | {"partition"}
        if not isinstance(resource, dict) or not required <= set(resource) or not set(resource) <= allowed:
            raise ValueError("invalid resource incarnation identity")
        api_version = resource.get("apiVersion")
        kind = resource.get("kind")
        name = resource.get("name")
        uid = resource.get("uid")
        deletion_generation = resource.get("deletionGeneration")
        qualified_name = resource.get("qualifiedName")
        partition = resource.get("partition")
        effect_lease_ref = resource.get("effectLeaseRef")
        if not all(isinstance(value, str) and value for value in (api_version, kind, name, uid, qualified_name)):
            raise ValueError("invalid resource incarnation identity")
        GVK(cast(str, api_version), cast(str, kind))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cast(str, name)):
            raise ValueError("invalid resource incarnation name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", cast(str, uid)):
            raise ValueError("invalid resource incarnation UID")
        if type(deletion_generation) is not int or deletion_generation < 1:
            raise ValueError("invalid resource incarnation deletion generation")
        if partition is not None and (
            not isinstance(partition, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", partition)
        ):
            raise ValueError("invalid resource incarnation partition")
        if effect_lease_ref is not None and (not isinstance(effect_lease_ref, str) or not effect_lease_ref):
            raise ValueError("invalid resource incarnation effect lease ref")
        if re.fullmatch(QUALIFIED_RESOURCE_NAME_PATTERN, cast(str, qualified_name)) is None:
            raise ValueError("invalid resource incarnation qualified name")
        return cls(
            api_version=cast(str, api_version),
            kind=cast(str, kind),
            name=cast(str, name),
            uid=cast(str, uid),
            deletion_generation=deletion_generation,
            qualified_name=cast(str, qualified_name),
            partition=cast(str | None, partition),
            effect_lease_ref=cast(str | None, effect_lease_ref),
        )


class EffectLeaseUnavailable(OperationError):
    """The desired Git state grants the effect lease to another runner."""


@dataclass(frozen=True)
class EffectLeaseSnapshot:
    """Immutable identity of the desired and cleanup inputs fenced by a lease."""

    unit_path: str | None
    unit_blob: str | None
    api_version: str | None
    kind: str | None
    driver: str | None
    source_revision: str | None
    cleanup_path: str | None
    cleanup_blob: str | None

    def document(self) -> JsonObject:
        return {
            "unitPath": self.unit_path,
            "unitBlob": self.unit_blob,
            "apiVersion": self.api_version,
            "kind": self.kind,
            "driver": self.driver,
            "sourceRevision": self.source_revision,
            "cleanupPath": self.cleanup_path,
            "cleanupBlob": self.cleanup_blob,
        }

    @classmethod
    def from_document(cls, document: object) -> EffectLeaseSnapshot:
        expected = {
            "unitPath",
            "unitBlob",
            "apiVersion",
            "kind",
            "driver",
            "sourceRevision",
            "cleanupPath",
            "cleanupBlob",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid effect lease snapshot")
        values = {
            "unit_path": document.get("unitPath"),
            "unit_blob": document.get("unitBlob"),
            "api_version": document.get("apiVersion"),
            "kind": document.get("kind"),
            "driver": document.get("driver"),
            "source_revision": document.get("sourceRevision"),
            "cleanup_path": document.get("cleanupPath"),
            "cleanup_blob": document.get("cleanupBlob"),
        }
        if not all(value is None or isinstance(value, str) for value in values.values()):
            raise ValueError("invalid effect lease snapshot values")
        for key in ("unit_blob", "cleanup_blob"):
            value = values[key]
            if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
                raise ValueError("invalid effect lease snapshot blob")
        source_revision = values["source_revision"]
        if source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("invalid effect lease snapshot source revision")
        return cls(
            unit_path=values["unit_path"],
            unit_blob=values["unit_blob"],
            api_version=values["api_version"],
            kind=values["kind"],
            driver=values["driver"],
            source_revision=source_revision,
            cleanup_path=values["cleanup_path"],
            cleanup_blob=values["cleanup_blob"],
        )


@dataclass(frozen=True)
class EffectLease:
    """A non-expiring, CAS-published lease for one desired Unit incarnation."""

    unit_name: str
    uid: str
    token: str
    owner: str
    desired_revision: str
    qualified_name: str | None = None
    snapshot: EffectLeaseSnapshot | None = None

    def __post_init__(self) -> None:
        if self.qualified_name is None:
            object.__setattr__(self, "qualified_name", self.unit_name)

    def document(self) -> JsonObject:
        return {
            "schema": 1,
            "kind": "UnitEffectLease",
            "unitName": self.unit_name,
            "uid": self.uid,
            "token": self.token,
            "owner": self.owner,
            "desiredRevision": self.desired_revision,
            "qualifiedName": self.qualified_name or self.unit_name,
            **({"snapshot": self.snapshot.document()} if self.snapshot is not None else {}),
        }

    @classmethod
    def from_document(cls, document: object, expected_qualified_name: str) -> EffectLease:
        if not isinstance(document, dict) or set(document) not in (
            {
                "schema",
                "kind",
                "unitName",
                "uid",
                "token",
                "owner",
                "desiredRevision",
                "qualifiedName",
            },
            {
                "schema",
                "kind",
                "unitName",
                "uid",
                "token",
                "owner",
                "desiredRevision",
                "qualifiedName",
                "snapshot",
            },
        ):
            raise ValueError("invalid effect lease envelope")
        uid = document.get("uid")
        token = document.get("token")
        owner = document.get("owner")
        desired_revision = document.get("desiredRevision")
        qualified_name = document.get("qualifiedName")
        if (
            type(document.get("schema")) is not int
            or document.get("schema") != 1
            or document.get("kind") != "UnitEffectLease"
            or document.get("unitName") != PurePosixPath(expected_qualified_name).parts[-1]
            or not isinstance(uid, str)
            or not isinstance(token, str)
            or not isinstance(owner, str)
            or not isinstance(desired_revision, str)
            or not isinstance(qualified_name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", uid)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", token)
            or not owner
            or not re.fullmatch(r"[0-9a-f]{40}", desired_revision)
            or not re.fullmatch(QUALIFIED_RESOURCE_NAME_PATTERN, qualified_name)
        ):
            raise ValueError("invalid effect lease fence")
        if qualified_name != expected_qualified_name:
            raise ValueError("effect lease qualifiedName does not match its storage identity")
        ResourceMetadata(name=PurePosixPath(expected_qualified_name).parts[-1], uid=uid).validate_desired()
        snapshot = EffectLeaseSnapshot.from_document(document["snapshot"]) if "snapshot" in document else None
        return cls(
            unit_name=PurePosixPath(expected_qualified_name).parts[-1],
            uid=uid,
            token=token,
            owner=owner,
            desired_revision=desired_revision,
            qualified_name=qualified_name,
            snapshot=snapshot,
        )


@dataclass(frozen=True)
class EffectLeaseAcquisition:
    lease: EffectLease
    revision: str


def _effect_lease_store_root(
    desired_ref: str,
    desired_revision: str,
    desired_root: Path,
    lease_ref: str | None,
    output: Path,
) -> tuple[Path, str | None]:
    """Materialize the lease store and return its root and current revision."""

    if lease_ref is None or lease_ref == desired_ref:
        return desired_root, desired_revision
    lease_revision = fetch_ref(lease_ref)
    if lease_revision is None:
        output.mkdir(parents=True, exist_ok=True)
    else:
        materialize_revision(lease_revision, output)
    marker = output / ".gitopsctr/effect-leases/.store"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("branch\n")
    return output, lease_revision


def _effect_lease_publish_ref(desired_ref: str, lease_ref: str | None) -> str:
    return desired_ref if lease_ref is None else lease_ref


def renew_effect_lease(
    desired_ref: str,
    acquisition: EffectLeaseAcquisition,
    *,
    lease_ref: str | None = None,
) -> EffectLeaseAcquisition:
    """Renew one lease against the latest head while fencing the same Unit snapshot."""

    for attempt in range(5):
        current_revision = fetch_ref(desired_ref)
        if current_revision is None:
            raise EffectLeaseUnavailable(
                f"desired ref disappeared while renewing the effect lease for {acquisition.lease.unit_name!r}"
            )
        with tempfile.TemporaryDirectory() as temporary_directory:
            current = Path(temporary_directory) / "desired"
            materialize_revision(current_revision, current)
            lease_root, lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                Path(temporary_directory) / "leases",
            )
            qualified_name = acquisition.lease.qualified_name or acquisition.lease.unit_name
            existing = load_desired_effect_leases(lease_root).get(qualified_name)
            if (
                existing is None
                or existing.token != acquisition.lease.token
                or existing.uid != acquisition.lease.uid
                or acquisition.lease.snapshot is None
                or existing.snapshot != acquisition.lease.snapshot
                or effect_lease_snapshot(current, qualified_name, acquisition.lease.uid) != acquisition.lease.snapshot
            ):
                raise EffectLeaseUnavailable(
                    f"effect lease for {acquisition.lease.unit_name!r} no longer fences the same Unit snapshot"
                )
            renewed = replace(
                existing,
                desired_revision=current_revision,
            )
            write_effect_lease(lease_root, renewed)
            try:
                published_revision = publish_tree(
                    _effect_lease_publish_ref(desired_ref, lease_ref),
                    lease_root,
                    lease_revision,
                    f"Renew effect lease for {renewed.unit_name} ({renewed.token})",
                    expected_publication_head=lease_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
                continue
            return EffectLeaseAcquisition(
                lease=renewed,
                revision=published_revision if lease_ref is None else current_revision,
            )
    raise EffectLeaseUnavailable(f"could not renew the effect lease for {acquisition.lease.unit_name!r}; retry")


class EffectLeaseHeartbeat:
    """Renew a desired-state lease while a driver effect is in progress."""

    def __init__(
        self,
        desired_ref: str,
        acquisition: EffectLeaseAcquisition,
        interval_seconds: float,
        lease_ref: str | None = None,
    ):
        self._desired_ref = desired_ref
        self._lease_ref = lease_ref
        self._latest = acquisition
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="gitopsctr-effect-lease", daemon=True)

    def start(self) -> EffectLeaseHeartbeat:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                with self._lock:
                    if self._lease_ref is None:
                        self._latest = renew_effect_lease(self._desired_ref, self._latest)
                    else:
                        self._latest = renew_effect_lease(
                            self._desired_ref,
                            self._latest,
                            lease_ref=self._lease_ref,
                        )
            except Exception as exc:
                self._error = exc
                self._stop.set()
                return

    def stop(self) -> EffectLeaseAcquisition:
        self._stop.set()
        self._thread.join()
        if self._error is not None:
            if isinstance(self._error, EffectLeaseUnavailable):
                raise self._error
            raise EffectLeaseUnavailable(f"effect lease heartbeat failed: {self._error}") from self._error
        with self._lock:
            return self._latest


def start_effect_lease_heartbeat(
    desired_ref: str,
    acquisition: EffectLeaseAcquisition,
    *,
    interval_seconds: float | None = None,
    lease_ref: str | None = None,
) -> EffectLeaseHeartbeat:
    interval = interval_seconds if interval_seconds is not None else EFFECT_LEASE_HEARTBEAT_INTERVAL_SECONDS
    return EffectLeaseHeartbeat(desired_ref, acquisition, max(0.01, interval), lease_ref).start()


@dataclass(frozen=True)
class TeardownEvidence:
    """Observed-state proof that one UID-fenced teardown completed."""

    unit_name: str
    qualified_name: str
    uid: str
    deletion_generation: int
    desired_revision: str
    effect_lease_ref: str | None = None
    details: JsonObject = field(default_factory=dict)

    def document(self) -> JsonObject:
        return {
            "schema": 1,
            "kind": "UnitTeardownEvidence",
            "unitName": self.unit_name,
            "qualifiedName": self.qualified_name,
            "uid": self.uid,
            "deletionGeneration": self.deletion_generation,
            "desiredRevision": self.desired_revision,
            "effectLeaseRef": self.effect_lease_ref,
            "details": self.details,
        }

    @classmethod
    def from_document(cls, document: object, expected_qualified_name: str) -> TeardownEvidence:
        required_fields = {
            "schema",
            "kind",
            "unitName",
            "qualifiedName",
            "uid",
            "deletionGeneration",
            "desiredRevision",
            "effectLeaseRef",
        }
        if not isinstance(document, dict) or set(document) not in (
            required_fields,
            required_fields | {"details"},
        ):
            raise ValueError("invalid teardown evidence envelope")
        raw_uid = document.get("uid")
        raw_generation = document.get("deletionGeneration")
        raw_revision = document.get("desiredRevision")
        raw_effect_lease_ref = document.get("effectLeaseRef")
        raw_details = document.get("details", {})
        if (
            type(document.get("schema")) is not int
            or document.get("schema") != 1
            or document.get("kind") != "UnitTeardownEvidence"
            or document.get("unitName") != PurePosixPath(expected_qualified_name).name
            or document.get("qualifiedName") != expected_qualified_name
            or not isinstance(raw_uid, str)
            or not isinstance(raw_generation, int)
            or isinstance(raw_generation, bool)
            or raw_generation < 1
            or not isinstance(raw_revision, str)
            or (raw_effect_lease_ref is not None and not isinstance(raw_effect_lease_ref, str))
            or (isinstance(raw_effect_lease_ref, str) and not raw_effect_lease_ref)
            or not isinstance(raw_details, dict)
        ):
            raise ValueError("invalid teardown evidence envelope")
        ResourceMetadata(name=PurePosixPath(expected_qualified_name).name, uid=raw_uid).validate_desired()
        if not re.fullmatch(r"[0-9a-f]{40}", raw_revision):
            raise ValueError("invalid teardown evidence desired revision")
        try:
            details = cast(JsonObject, require_json_value(raw_details))
        except ValueError as exc:
            raise ValueError("invalid teardown evidence details") from exc
        return cls(
            unit_name=PurePosixPath(expected_qualified_name).name,
            qualified_name=expected_qualified_name,
            uid=raw_uid,
            deletion_generation=raw_generation,
            desired_revision=raw_revision,
            effect_lease_ref=cast(str | None, raw_effect_lease_ref),
            details=details,
        )


def desired_effect_lease_paths(root: Path) -> tuple[Path, ...]:
    directory = root / DESIRED_EFFECT_LEASES_PATH
    if not directory.is_dir():
        return ()
    paths = sorted(
        path for path in directory.rglob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}
    )
    names: dict[str, Path] = {}
    for path in paths:
        qualified_name = path.relative_to(directory).with_suffix("").as_posix()
        if qualified_name in names:
            raise OperationError(f"multiple effect lease formats exist for {qualified_name!r}")
        names[qualified_name] = path
    return tuple(paths)


def resource_incarnation_path(root: Path, tombstone: ResourceIncarnationTombstone) -> Path:
    qualified_name = tombstone.qualified_name or tombstone.name
    return (
        root
        / DESIRED_RESOURCE_INCARNATIONS_PATH
        / PurePosixPath(tombstone.api_version)
        / tombstone.kind
        / PurePosixPath(qualified_name)
        / f"{tombstone.uid}.json"
    )


def load_resource_incarnation_evidence(root: Path) -> tuple[ResourceIncarnationTombstone, ...]:
    """Load every finalized UID evidence record, including older incarnations."""

    directory = root / DESIRED_RESOURCE_INCARNATIONS_PATH
    evidence: list[ResourceIncarnationTombstone] = []
    if not directory.is_dir():
        return ()
    for path in sorted(directory.rglob("*.json")):
        try:
            tombstone = ResourceIncarnationTombstone.from_document(load_json(path))
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid resource incarnation tombstone at {path.relative_to(root)}") from exc
        if path != resource_incarnation_path(root, tombstone):
            raise OperationError(f"invalid resource incarnation tombstone path for {tombstone.name!r}")
        evidence.append(tombstone)
    return tuple(evidence)


def write_resource_incarnation_tombstone(root: Path, tombstone: ResourceIncarnationTombstone) -> Path:
    path = resource_incarnation_path(root, tombstone)
    return write_document(path, tombstone.document(), format=DocumentFormat.JSON)


def copy_resource_incarnation_tombstones(current: Path, candidate: Path) -> None:
    for tombstone in load_resource_incarnation_evidence(current):
        source = resource_incarnation_path(current, tombstone)
        target = resource_incarnation_path(candidate, tombstone)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def finalized_incarnation_evidence(
    root: Path,
    api_version: str,
    kind: str,
    name: str,
    uid: str,
    deletion_generation: int | None = None,
) -> ResourceIncarnationTombstone | None:
    for tombstone in load_resource_incarnation_evidence(root):
        if (
            tombstone.api_version == api_version
            and tombstone.kind == kind
            and (tombstone.name == name or tombstone.qualified_name == name)
            and tombstone.uid == uid
            and (deletion_generation is None or tombstone.deletion_generation == deletion_generation)
        ):
            return tombstone
    return None


def load_desired_effect_leases(root: Path) -> dict[str, EffectLease]:
    leases: dict[str, EffectLease] = {}
    for path in desired_effect_lease_paths(root):
        name = path.relative_to(root / DESIRED_EFFECT_LEASES_PATH).with_suffix("").as_posix()
        try:
            lease = EffectLease.from_document(load_json(path), name)
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid effect lease for {name!r}") from exc
        leases[name] = lease
    return leases


def effect_lease_snapshot(root: Path, unit_name: str, uid: str) -> EffectLeaseSnapshot:
    candidate_unit_path = unit_document_path(root, unit_name)
    unit_path = candidate_unit_path if candidate_unit_path.is_file() else None
    api_version = kind = driver = source_revision = unit_blob = None
    if unit_path is not None:
        unit = load_desired_unit(unit_path, unit_name)
        unit.metadata.validate_desired()
        if unit.metadata.uid != uid:
            raise OperationError(f"leased desired Unit {unit_name!r} has a different UID")
        source = getattr(unit.spec, "source", None)
        unit_blob = file_blob(unit_path)
        api_version = unit.gvk.api_version
        kind = unit.gvk.kind
        driver = unit.driver_name
        source_revision = source.revision if isinstance(source, DesiredSource) else None

    cleanup_paths = document_candidates(root / DESIRED_CLEANUP_UNITS_PATH, unit_name)
    if len(cleanup_paths) > 1:
        raise OperationError(f"multiple cleanup formats exist for leased unit {unit_name!r}")
    return EffectLeaseSnapshot(
        unit_path=unit_path.relative_to(root).as_posix() if unit_path is not None else None,
        unit_blob=unit_blob,
        api_version=api_version,
        kind=kind,
        driver=driver,
        source_revision=source_revision,
        cleanup_path=cleanup_paths[0].relative_to(root).as_posix() if cleanup_paths else None,
        cleanup_blob=file_blob(cleanup_paths[0]) if cleanup_paths else None,
    )


def write_effect_lease(root: Path, lease: EffectLease) -> Path:
    qualified_name = lease.qualified_name or lease.unit_name
    if lease.snapshot is None:
        lease = replace(lease, snapshot=effect_lease_snapshot(root, qualified_name, lease.uid))
    relative = PurePosixPath(qualified_name)
    directory = root / DESIRED_EFFECT_LEASES_PATH / Path(*relative.parts[:-1])
    for path in document_candidates(directory, relative.parts[-1]):
        path.unlink()
    return write_document(directory / f"{relative.parts[-1]}.json", lease.document(), format=DocumentFormat.JSON)


def remove_effect_lease(root: Path, unit_name: str) -> None:
    relative = PurePosixPath(unit_name)
    directory = root / DESIRED_EFFECT_LEASES_PATH / Path(*relative.parts[:-1])
    for path in document_candidates(directory, relative.parts[-1]):
        path.unlink()


def effect_lease_owner() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    host = os.uname().nodename
    return f"{run_id or 'local'}-{attempt}-{host}-{os.getpid()}"


def effect_lease_token() -> str:
    return f"lease-{hashlib.sha256(os.urandom(32)).hexdigest()}"


def effect_lease_active(_lease: EffectLease) -> bool:
    """All persisted leases are active until token-fenced release or recovery."""

    return True


def acquire_effect_lease(
    desired_ref: str,
    desired_revision: str,
    unit_name: str,
    uid: str,
    *,
    precondition: Callable[[Path], None] | None = None,
    resume_existing: bool = False,
    lease_ref: str | None = None,
) -> EffectLeaseAcquisition:
    expected_snapshot: EffectLeaseSnapshot | None = None
    for attempt in range(5):
        current_revision = fetch_ref(desired_ref)
        if current_revision is None:
            raise EffectLeaseUnavailable(f"desired ref disappeared before acquiring the effect lease for {unit_name!r}")
        with tempfile.TemporaryDirectory() as temporary_directory:
            current = Path(temporary_directory) / "desired"
            materialize_revision(current_revision, current)
            lease_root, lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                Path(temporary_directory) / "leases",
            )
            if expected_snapshot is None:
                if current_revision == desired_revision:
                    expected_snapshot = effect_lease_snapshot(current, unit_name, uid)
                else:
                    with tempfile.TemporaryDirectory() as initial_directory:
                        initial = Path(initial_directory) / "initial"
                        materialize_revision(desired_revision, initial)
                        expected_snapshot = effect_lease_snapshot(initial, unit_name, uid)
            if effect_lease_snapshot(current, unit_name, uid) != expected_snapshot:
                raise EffectLeaseUnavailable(
                    f"desired Unit {unit_name!r} changed before acquiring its effect lease; retry"
                )
            leases = load_desired_effect_leases(lease_root)
            current_resources = load_desired_resource_graph(current)
            current_unit = next(
                (
                    resource
                    for key, resource in current_resources.items()
                    if isinstance(resource, UnitResource) and key[2] == unit_name
                ),
                None,
            )
            qualified_name = (
                qualified_unit_name(current_resources, current_unit) if current_unit is not None else unit_name
            )
            existing = leases.get(unit_name)
            if existing is not None:
                if resume_existing:
                    if existing.uid != uid:
                        raise EffectLeaseUnavailable(
                            f"effect lease for {unit_name!r} is fenced to a different Unit UID"
                        )
                    if existing.qualified_name != qualified_name:
                        raise EffectLeaseUnavailable(
                            f"effect lease for {unit_name!r} is fenced to a different qualified Unit address"
                        )
                    if existing.snapshot is None or effect_lease_snapshot(current, unit_name, uid) != existing.snapshot:
                        raise EffectLeaseUnavailable(
                            f"effect lease for {unit_name!r} no longer fences the same Unit snapshot"
                        )
                    if precondition is not None:
                        precondition(current)
                    return EffectLeaseAcquisition(lease=existing, revision=current_revision)
                raise EffectLeaseUnavailable(
                    f"effect lease for {unit_name!r} is held by {existing.owner}; explicit UID/token recovery is required"
                )
            snapshot = effect_lease_snapshot(current, unit_name, uid)
            lease = EffectLease(
                unit_name=current_unit.name if current_unit is not None else PurePosixPath(unit_name).parts[-1],
                uid=uid,
                token=effect_lease_token(),
                owner=effect_lease_owner(),
                desired_revision=current_revision,
                qualified_name=qualified_name,
                snapshot=snapshot,
            )
            if precondition is not None:
                precondition(current)
            write_effect_lease(lease_root, lease)
            try:
                published_revision = publish_tree(
                    _effect_lease_publish_ref(desired_ref, lease_ref),
                    lease_root,
                    lease_revision,
                    f"Acquire effect lease for {unit_name} ({lease.token})",
                    expected_publication_head=lease_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
                continue
            return EffectLeaseAcquisition(
                lease=lease,
                revision=published_revision if lease_ref is None else current_revision,
            )
    raise EffectLeaseUnavailable(f"could not acquire the effect lease for {unit_name!r}; retry")


def release_effect_lease(
    desired_ref: str,
    unit_name: str,
    token: str,
    uid: str | None = None,
    *,
    verify_snapshot: bool = True,
    lease_ref: str | None = None,
) -> str | None:
    for attempt in range(5):
        current_revision = fetch_ref(desired_ref)
        if current_revision is None:
            return None
        with tempfile.TemporaryDirectory() as temporary_directory:
            current = Path(temporary_directory) / "desired"
            materialize_revision(current_revision, current)
            lease_root, lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                Path(temporary_directory) / "leases",
            )
            leases = load_desired_effect_leases(lease_root)
            existing = leases.get(unit_name)
            if existing is None:
                return current_revision
            if existing.token != token or (uid is not None and existing.uid != uid):
                raise OperationError(f"effect lease for {unit_name!r} is held by another runner")
            if verify_snapshot and (
                existing.snapshot is None
                or effect_lease_snapshot(current, unit_name, existing.uid) != existing.snapshot
            ):
                raise EffectLeaseUnavailable(f"effect lease for {unit_name!r} no longer fences the same Unit snapshot")
            remove_effect_lease(lease_root, unit_name)
            try:
                return publish_tree(
                    _effect_lease_publish_ref(desired_ref, lease_ref),
                    lease_root,
                    lease_revision,
                    f"Release effect lease for {unit_name}",
                    expected_publication_head=lease_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not release the effect lease for {unit_name!r}; explicit recovery remains available")


def release_pre_effect_lease(
    desired_ref: str,
    acquisition: EffectLeaseAcquisition,
    *,
    lease_ref: str | None = None,
) -> None:
    """Release a lease before driver invocation, when no external effect can be uncertain."""

    release_effect_lease(
        desired_ref,
        acquisition.lease.qualified_name or acquisition.lease.unit_name,
        acquisition.lease.token,
        acquisition.lease.uid,
        verify_snapshot=False,
        lease_ref=lease_ref,
    )


def recover_effect_lease(
    desired_ref: str,
    unit_name: str,
    uid: str,
    token: str,
    *,
    lease_ref: str | None = None,
) -> str | None:
    """Explicitly clear an abandoned lease after the external effect is verified stopped."""

    for attempt in range(5):
        current_revision = fetch_ref(desired_ref)
        if current_revision is None:
            return None
        with tempfile.TemporaryDirectory() as temporary_directory:
            current = Path(temporary_directory) / "desired"
            materialize_revision(current_revision, current)
            lease_root, lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                Path(temporary_directory) / "leases",
            )
            existing = load_desired_effect_leases(lease_root).get(unit_name)
            if existing is None:
                return current_revision
            if existing.uid != uid or existing.token != token:
                raise EffectLeaseUnavailable(
                    f"effect lease recovery fence did not match the current {unit_name!r} lease"
                )
            remove_effect_lease(lease_root, unit_name)
            try:
                return publish_tree(
                    _effect_lease_publish_ref(desired_ref, lease_ref),
                    lease_root,
                    lease_revision,
                    f"Recover abandoned effect lease for {unit_name}",
                    expected_publication_head=lease_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not recover the effect lease for {unit_name!r}; retry")


def rebase_effect_completion(
    desired_ref: str,
    acquisition: EffectLeaseAcquisition,
    unit_name: str,
    uid: str,
    current_root: Path,
    *,
    lease_ref: str | None = None,
) -> EffectLeaseAcquisition:
    """Refresh local desired state after an effect while preserving its Unit fence."""

    if acquisition.lease.snapshot is None:
        raise EffectLeaseUnavailable(
            f"effect lease for {unit_name!r} lacks an immutable completion snapshot; retry or recover explicitly"
        )
    for attempt in range(5):
        latest_revision = fetch_ref(desired_ref)
        if latest_revision is None:
            raise EffectLeaseUnavailable(f"desired ref disappeared before completing {unit_name!r}")
        with tempfile.TemporaryDirectory() as temporary_directory:
            latest = Path(temporary_directory) / "desired"
            materialize_revision(latest_revision, latest)
            lease_root, lease_revision = _effect_lease_store_root(
                desired_ref,
                latest_revision,
                latest,
                lease_ref,
                Path(temporary_directory) / "leases",
            )
            existing = load_desired_effect_leases(lease_root).get(unit_name)
            if (
                existing is None
                or existing.token != acquisition.lease.token
                or existing.uid != uid
                or existing.snapshot != acquisition.lease.snapshot
                or effect_lease_snapshot(latest, unit_name, uid) != acquisition.lease.snapshot
            ):
                raise EffectLeaseUnavailable(
                    f"desired Unit {unit_name!r} changed during effect completion; result was not published"
                )
            rebased = replace(existing, desired_revision=latest_revision)
            if lease_ref is not None and lease_ref != desired_ref and existing.desired_revision != latest_revision:
                write_effect_lease(lease_root, rebased)
                try:
                    publish_tree(
                        lease_ref,
                        lease_root,
                        lease_revision,
                        f"Rebase effect lease for {unit_name} ({rebased.token})",
                        expected_publication_head=lease_revision,
                    )
                except subprocess.CalledProcessError as exc:
                    if attempt == 4 or not retryable_push_failure(exc):
                        raise
                    continue
            shutil.rmtree(current_root)
            shutil.copytree(latest, current_root)
            return EffectLeaseAcquisition(lease=rebased, revision=latest_revision)
    raise EffectLeaseUnavailable(f"could not rebase the effect lease for {unit_name!r}; retry")


def validate_effect_lease_head(
    desired_ref: str,
    unit_name: str,
    uid: str,
    token: str,
    snapshot: EffectLeaseSnapshot | None,
    *,
    lease_ref: str | None = None,
) -> str:
    """Validate one Unit fence at the latest desired head without fencing unrelated Units."""

    if snapshot is None:
        raise EffectLeaseUnavailable(f"effect lease for {unit_name!r} lacks an immutable snapshot")
    latest_revision = fetch_ref(desired_ref)
    if latest_revision is None:
        raise EffectLeaseUnavailable(f"desired ref disappeared before publishing {unit_name!r}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        latest = Path(temporary_directory) / "desired"
        materialize_revision(latest_revision, latest)
        lease_root, _lease_revision = _effect_lease_store_root(
            desired_ref,
            latest_revision,
            latest,
            lease_ref,
            Path(temporary_directory) / "leases",
        )
        existing = load_desired_effect_leases(lease_root).get(unit_name)
        if (
            existing is None
            or existing.token != token
            or existing.uid != uid
            or existing.snapshot != snapshot
            or effect_lease_snapshot(latest, unit_name, uid) != snapshot
        ):
            raise EffectLeaseUnavailable(f"desired Unit {unit_name!r} changed before completion publication")
    return latest_revision


def validate_effect_lease_head_for_store(
    desired_ref: str,
    unit_name: str,
    uid: str,
    token: str,
    snapshot: EffectLeaseSnapshot | None,
    lease_ref: str | None,
) -> str:
    if lease_ref is None:
        return validate_effect_lease_head(desired_ref, unit_name, uid, token, snapshot)
    return validate_effect_lease_head(
        desired_ref,
        unit_name,
        uid,
        token,
        snapshot,
        lease_ref=lease_ref,
    )


def teardown_evidence_filename(uid: str, deletion_generation: int) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", uid):
        raise OperationError("teardown evidence identity is not safe for a filename")
    if deletion_generation < 1:
        raise OperationError("teardown evidence generation must be positive")
    return f"{uid}.{deletion_generation}.json"


def load_teardown_evidence(
    root: Path,
    unit_name: str,
    uid: str | None = None,
    deletion_generation: int | None = None,
) -> TeardownEvidence | None:
    directory = root / OBSERVED_TEARDOWN_EVIDENCE_PATH / PurePosixPath(unit_name)
    if not directory.is_dir():
        return None
    candidates = sorted(
        path for path in directory.iterdir() if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}
    )
    selected: list[Path] = []
    for path in candidates:
        try:
            evidence = TeardownEvidence.from_document(load_json(path), unit_name)
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid teardown evidence for {unit_name!r}") from exc
        if path.name != teardown_evidence_filename(evidence.uid, evidence.deletion_generation):
            raise OperationError(f"teardown evidence filename does not match its fence for {unit_name!r}")
        if uid is not None and deletion_generation is not None:
            if evidence.uid == uid and evidence.deletion_generation == deletion_generation:
                selected.append(path)
        else:
            selected.append(path)
    if len(selected) > 1:
        raise OperationError(f"multiple teardown evidence fences exist for {unit_name!r}")
    if not selected:
        return None
    try:
        return TeardownEvidence.from_document(load_json(selected[0]), unit_name)
    except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
        raise OperationError(f"invalid teardown evidence for {unit_name!r}") from exc


def publish_teardown_observation_cas(
    observed_ref: str,
    unit_name: str,
    uid: str,
    deletion_generation: int,
    desired_revision: str,
    *,
    desired_ref: str | None = None,
    lease_ref: str | None = None,
    lease_token: str | None = None,
    lease_snapshot: EffectLeaseSnapshot | None = None,
    details: Mapping[str, object] | None = None,
) -> str:
    for attempt in range(5):
        if attempt:
            log_status("RETRY", f"teardown observation publish attempt {attempt + 1}/5")
        with tempfile.TemporaryDirectory() as temporary_directory:
            observed = Path(temporary_directory) / "observed"
            observed_revision = observed_tree(observed_ref, observed)
            if desired_ref is not None and lease_token is not None:
                desired_revision = validate_effect_lease_head_for_store(
                    desired_ref,
                    unit_name,
                    uid,
                    lease_token,
                    lease_snapshot,
                    lease_ref=lease_ref,
                )
            existing = load_teardown_evidence(
                observed,
                unit_name,
                uid,
                deletion_generation,
            )
            if existing is not None:
                # The evidence is the durable record of the store used by the
                # teardown.  A crash may be followed by a configuration change;
                # resuming against that new store would leave the old lease
                # stranded or release a different incarnation's lease.
                lease_ref = existing.effect_lease_ref
            try:
                evidence_details = cast(
                    JsonObject,
                    require_json_value(
                        dict(existing.details if details is None and existing is not None else details or {})
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise OperationError("teardown returned non-JSON evidence details") from exc
            evidence = TeardownEvidence(
                unit_name=PurePosixPath(unit_name).name,
                qualified_name=unit_name,
                uid=uid,
                deletion_generation=deletion_generation,
                desired_revision=desired_revision,
                effect_lease_ref=lease_ref,
                details=evidence_details,
            )
            receipt_paths = document_candidates(
                observed / "units" / PurePosixPath(unit_name).parent,
                PurePosixPath(unit_name).name,
            )
            artifact_path = observed / "artifacts" / PurePosixPath(unit_name)
            had_active_observation = bool(receipt_paths) or artifact_path.exists()
            for receipt_path in receipt_paths:
                receipt_path.unlink()
            if artifact_path.is_dir() and not artifact_path.is_symlink():
                shutil.rmtree(artifact_path)
            elif artifact_path.is_symlink():
                artifact_path.unlink()
            evidence_path = (
                observed
                / OBSERVED_TEARDOWN_EVIDENCE_PATH
                / PurePosixPath(unit_name)
                / teardown_evidence_filename(uid, deletion_generation)
            )
            if desired_ref is not None and lease_token is not None:
                latest_revision = validate_effect_lease_head_for_store(
                    desired_ref,
                    unit_name,
                    uid,
                    lease_token,
                    lease_snapshot,
                    lease_ref=lease_ref,
                )
                if latest_revision != desired_revision:
                    desired_revision = latest_revision
                    evidence = TeardownEvidence(
                        unit_name=PurePosixPath(unit_name).name,
                        qualified_name=unit_name,
                        uid=uid,
                        deletion_generation=deletion_generation,
                        desired_revision=desired_revision,
                        effect_lease_ref=lease_ref,
                        details=evidence_details,
                    )
            elif desired_ref is not None:
                # A separate effect lease is optional, but the desired
                # revision fence is not.  Teardown evidence must describe the
                # exact desired snapshot whose UID/content fence was checked
                # before the external effect ran.
                assert_desired_ref_fence(desired_ref, desired_revision, unit_name, uid)
            write_document(evidence_path, evidence.document(), format=DocumentFormat.JSON)
            if existing is not None and not had_active_observation and observed_revision is not None:
                return observed_revision
            try:
                return publish_tree(
                    observed_ref,
                    observed,
                    observed_revision,
                    f"Record teardown of {unit_name} generation {deletion_generation}",
                    expected_publication_head=observed_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not update {observed_ref} after concurrent updates")


def desired_uid_provenance(
    unit: UnitResource[Any],
    source: DesiredSource | None,
    source_revision: str | None,
    finalized_uids: Sequence[str] = (),
) -> str:
    return json.dumps(
        {
            "apiVersion": unit.gvk.api_version,
            "kind": unit.gvk.kind,
            "name": unit.name,
            "source": source.to_dict() if source is not None else None,
            "sourceRevision": source_revision,
            "finalizedUids": sorted(set(finalized_uids)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def root_metadata_for_resource(
    unit: UnitResource[Any],
    source: DesiredSource | None = None,
    source_revision: str | None = None,
    finalized_uids: Sequence[str] = (),
) -> ResourceMetadata:
    retained_source = source if source is not None else getattr(unit.spec, "source", None)
    if retained_source is not None and not isinstance(retained_source, DesiredSource):
        retained_source = None
    return ResourceMetadata.root_from_provenance(
        unit.name,
        desired_uid_provenance(
            unit=unit,
            source=retained_source,
            source_revision=source_revision,
            finalized_uids=finalized_uids,
        ),
    )


def opaque_document_payload(path: Path) -> object:
    try:
        return load_json(path)
    except Exception:
        try:
            return path.read_text()
        except (OSError, UnicodeError) as exc:
            return {"readError": str(exc)}


def raw_document_source(payload: object) -> DesiredSource | None:
    if not isinstance(payload, dict):
        return None
    specification = payload.get("spec", payload)
    source = specification.get("source") if isinstance(specification, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        return None
    revision = source.get("revision")
    if not isinstance(revision, str) or re.fullmatch(EXACT_REVISION_PATTERN, revision) is None:
        return None
    inputs = source.get("inputs")
    return DesiredSource(
        path=source["path"],
        revision=revision,
        driverVersion=source.get("driverVersion") if isinstance(source.get("driverVersion"), int) else None,
        inputHash=source.get("inputHash") if isinstance(source.get("inputHash"), str) else None,
        inputs=inputs if isinstance(inputs, list) and all(isinstance(value, str) for value in inputs) else None,
    )


def opaque_resource_gvk(payload: object) -> tuple[str, str]:
    if isinstance(payload, dict):
        api_version = payload.get("apiVersion")
        kind = payload.get("kind")
        if isinstance(api_version, str) and "/" in api_version and isinstance(kind, str) and kind:
            return api_version, kind
    return CORE_API_VERSION, "OpaqueUnit"


def opaque_cleanup_content_digest(opaque: OpaqueCleanupRoot) -> str:
    metadata = replace(opaque.metadata, deletion=None).document(profile="desired")
    document = {
        "schema": 1,
        "kind": "OpaqueCleanupRoot",
        "metadata": metadata,
        "payload": opaque.payload,
    }
    return f"sha256:{hashlib.sha256(canonical_json(document)).hexdigest()}"


def mark_opaque_cleanup_for_deletion(
    opaque: OpaqueCleanupRoot,
    *,
    generation: int | None = None,
) -> OpaqueCleanupRoot:
    current = opaque.metadata.deletion
    if current is not None:
        if current.resourceDigest != opaque_cleanup_content_digest(opaque):
            raise OperationError(f"opaque cleanup root {opaque.metadata.name!r} changed after deletion started")
        return opaque
    deletion = DeletionMetadata(
        generation=generation or 1,
        resourceDigest=opaque_cleanup_content_digest(opaque),
    )
    return replace(opaque, metadata=replace(opaque.metadata, deletion=deletion))


def opaque_cleanup_metadata(name: str, payload: object, source_revision: str | None) -> ResourceMetadata:
    has_metadata = isinstance(payload, dict) and "metadata" in payload
    metadata_document = payload.get("metadata") if isinstance(payload, dict) else None
    if has_metadata and not isinstance(metadata_document, dict):
        raise OperationError(f"opaque cleanup metadata for {name!r} must be a mapping")
    if isinstance(metadata_document, dict):
        if metadata_document.get("name") != name:
            raise OperationError(f"opaque cleanup metadata for {name!r} has a mismatched name")
        if set(metadata_document) != {"name"}:
            try:
                metadata = ResourceMetadata.from_dict(metadata_document)
                metadata.validate_desired()
            except (KeyError, TypeError, ValueError) as exc:
                raise OperationError(f"opaque cleanup metadata for {name!r} is invalid") from exc
            if metadata.ownerReferences:
                raise OperationError(f"desired unit {name!r} collides with a UID-owned resource")
            if metadata.uid is None:
                raise OperationError(f"opaque cleanup metadata for {name!r} has no canonical UID")
            return metadata
    provenance = json.dumps(
        {"name": name, "sourceRevision": source_revision, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ResourceMetadata.root_from_provenance(name, provenance)


def load_desired_cleanup_roots(root: Path) -> dict[str, OpaqueCleanupRoot]:
    roots: dict[str, OpaqueCleanupRoot] = {}
    for path in desired_cleanup_root_paths(root):
        name = path.stem
        try:
            document = load_json(path)
        except DocumentFormatError as exc:
            raise OperationError(f"invalid opaque cleanup envelope for {name!r}") from exc
        if (
            type(document.get("schema")) is not int
            or document.get("schema") != 1
            or document.get("kind") != "OpaqueCleanupRoot"
            or "payload" not in document
        ):
            raise OperationError(f"invalid opaque cleanup envelope for {name!r}")
        payload = document["payload"]
        metadata_document = document.get("metadata")
        if not isinstance(metadata_document, dict):
            raise OperationError(f"invalid opaque cleanup envelope for {name!r}")
        try:
            metadata = ResourceMetadata.from_dict(metadata_document)
            metadata.validate_desired()
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid opaque cleanup metadata for {name!r}") from exc
        if metadata.name != name:
            raise OperationError(f"opaque cleanup metadata for {name!r} has a mismatched name")
        if not metadata.is_root:
            raise OperationError(f"opaque cleanup metadata for {name!r} must be a root")
        roots[name] = OpaqueCleanupRoot(
            path=path,
            payload=payload,
            metadata=metadata,
            source=raw_document_source(payload),
        )
    return roots


def write_opaque_cleanup_root(root: Path, name: str, opaque: OpaqueCleanupRoot) -> Path:
    suffix = opaque.path.suffix if opaque.path.suffix in {".json", ".yaml", ".yml"} else ".json"
    directory = root / DESIRED_CLEANUP_UNITS_PATH
    for existing in document_candidates(directory, name):
        existing.unlink()
    path = directory / f"{name}{suffix}"
    write_document(
        path,
        {
            "schema": 1,
            "kind": "OpaqueCleanupRoot",
            "metadata": opaque.metadata.document(profile="desired"),
            "payload": opaque.payload,
        },
        format=DocumentFormat.YAML if suffix in {".yaml", ".yml"} else DocumentFormat.JSON,
    )
    return path


def root_metadata_for_uid(name: str, uid: str, owner: DesiredOwnerReference | None = None) -> ResourceMetadata:
    """Build canonical recovery metadata without accepting authority from opaque payload bytes."""

    metadata = ResourceMetadata(
        name=name,
        uid=uid,
        ownerReferences=[owner] if owner is not None else None,
    )
    metadata.validate_desired()
    return metadata


def parse_opaque_recovery_unit(
    opaque: OpaqueCleanupRoot,
    unit_name: str,
    uid: str,
) -> UnitResource[Any]:
    """Parse only the persisted opaque payload, applying an external UID fence."""

    if opaque.metadata.uid != uid:
        raise OperationError(f"opaque cleanup UID fence for {unit_name!r} does not match --uid")
    if not isinstance(opaque.payload, dict):
        raise OperationError(f"opaque cleanup payload for {unit_name!r} is not a parseable Unit document")
    try:
        parsed = parse_desired_unit_document(cast(dict[str, Any], opaque.payload), unit_name)
    except (DocumentFormatError, DriverError, KeyError, TypeError, ValueError, OperationError) as exc:
        raise OperationError(f"opaque cleanup payload for {unit_name!r} is not parseable: {exc}") from exc
    if parsed.metadata.uid != uid:
        raise OperationError(f"opaque cleanup payload for {unit_name!r} has a conflicting identity")
    if resource_owner_reference(parsed) is not None:
        raise OperationError(f"opaque cleanup payload for {unit_name!r} has an unvalidated owner identity")
    return parsed


def command_recover_opaque_unit(args: argparse.Namespace) -> bool:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    if not isinstance(args.uid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.uid):
        raise OperationError("recover-opaque-unit requires a valid --uid")
    if not isinstance(args.source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", args.source_revision):
        raise OperationError("recover-opaque-unit requires an authoritative full --source-revision commit")
    if not commit_is_available(args.source_revision):
        raise OperationError(f"authoritative source revision is unavailable: {args.source_revision}")

    desired_ref, observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        args.desired_ref,
        None,
    )
    lease_ref = effect_lease_ref(args.environment, desired_ref)
    with unit_effect_lock(args.environment, args.unit):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source_root = temporary / "source"
            current = temporary / "current"
            candidate = temporary / "candidate"
            materialize_revision(args.source_revision, source_root)
            current_revision = observed_tree(desired_ref, current)
            if current_revision is None:
                raise OperationError(f"desired ref {desired_ref!r} has no state to recover")
            lease_root = current
            if lease_ref is not None and lease_ref != desired_ref:
                lease_root, _lease_revision = _effect_lease_store_root(
                    desired_ref,
                    current_revision,
                    current,
                    lease_ref,
                    temporary / "leases",
                )

            opaque = load_desired_cleanup_roots(current).get(args.unit)
            if opaque is None:
                raise OperationError(f"no opaque cleanup root exists for {args.unit!r}")
            if opaque.metadata.uid != args.uid:
                raise OperationError(f"stale opaque cleanup UID fence for {args.unit!r}")
            opaque_deletion = opaque.metadata.deletion
            if opaque_deletion is not None and opaque_deletion.resourceDigest != opaque_cleanup_content_digest(opaque):
                raise OperationError(f"opaque cleanup root {args.unit!r} changed after deletion started")
            incarnations = load_resource_incarnation_evidence(current)
            if any(tombstone.name == args.unit and tombstone.uid == args.uid for tombstone in incarnations):
                raise OperationError(f"opaque cleanup {args.unit!r} has already been finalized")
            if any(
                effect_lease_active(lease)
                for lease in load_desired_effect_leases(lease_root).values()
                if (lease.qualified_name or lease.unit_name) == args.unit
            ):
                raise OperationError(f"active effect lease blocks opaque recovery for {args.unit!r}")
            if document_candidates(current / "units", args.unit):
                raise OperationError(f"canonical desired Unit {args.unit!r} already exists")

            parsed = parse_opaque_recovery_unit(opaque, args.unit, args.uid)
            specifications = load_environment_specifications(source_root, args.environment)
            source_specification = specifications.get(args.unit)
            source_present = source_specification is not None
            if source_present:
                if parsed.gvk != source_specification.gvk or parsed.driver_name != source_specification.driver_name:
                    transition = True
                else:
                    transition = False
                payload_source = getattr(parsed.spec, "source", None)
                authored_source = getattr(source_specification.spec, "source", None)
                if not transition and (
                    (payload_source is None) != (authored_source is None)
                    or (
                        payload_source is not None
                        and authored_source is not None
                        and payload_source.path != authored_source.path
                    )
                ):
                    raise OperationError(f"opaque cleanup payload for {args.unit!r} conflicts with source identity")
                if not transition:
                    payload_revision = payload_source.revision if isinstance(payload_source, DesiredSource) else None
                    if payload_revision != args.source_revision:
                        raise OperationError(
                            f"authoritative source for {args.unit!r} changed after opaque cleanup was retained"
                        )
            else:
                transition = False

            restored = parsed.with_metadata(root_metadata_for_uid(args.unit, args.uid))
            if opaque_deletion is not None or not source_present or transition:
                restored = cast(
                    UnitResource[Any],
                    mark_resource_for_deletion(
                        restored,
                        generation=opaque_deletion.generation if opaque_deletion is not None else None,
                    ),
                )
            require_unit(restored, args.unit)
            validate_unit_materialization(current, args.unit, parsed)

            shutil.copytree(current, candidate)
            for path in document_candidates(candidate / DESIRED_CLEANUP_UNITS_PATH, args.unit):
                path.unlink()
            for path in document_candidates(
                candidate / "units" / PurePosixPath(args.unit).parent,
                PurePosixPath(args.unit).name,
            ):
                path.unlink()
            write_desired_candidate_unit(
                unit_document_path(candidate, args.unit),
                restored,
                source_root,
            )
            copy_unit_materialization(current, candidate, args.unit, restored)

            transition_blocks = load_desired_transition_blocks(candidate)
            transition_blocks.pop(args.unit, None)
            write_desired_transition_blocks(candidate, transition_blocks)
            load_desired_resource_graph(candidate)
            if args.dry:
                log_status("DRY", f"{style_unit(args.unit)}: opaque cleanup recovery would be published")
                return False
            candidate_id = candidate_identifier(
                "deletion-progression",
                args.environment,
                candidate,
                desired_ref,
                current_revision,
                {"unit": args.unit, "uid": args.uid, "operation": "recover-opaque-unit"},
            )
            candidate_ref = resolve_candidate_ref(
                REPOSITORY_ROOT,
                args.environment,
                "deletion-progression",
                candidate_id,
                args.candidate_ref,
            )
            if candidate_ref_conflicts(candidate_ref, desired_ref, observed_ref):
                raise OperationError("opaque recovery candidate ref conflicts with deployment state")
            revision, outcome = publish_desired_change(
                args.environment,
                candidate,
                desired_ref,
                current_revision,
                candidate_ref,
                f"Recover opaque cleanup for {args.unit}",
                f"Recover opaque cleanup for {args.unit}",
                f"Restore the UID-fenced opaque cleanup root for `{args.unit}`.",
                False,
                current,
                request_change=False,
                conflicting_refs=(observed_ref,),
            )
            if outcome is not None:
                log_status(
                    "REVIEW",
                    f"{style_branch(candidate_ref)} submitted at {describe_revision(revision)}; "
                    f"{style_branch(desired_ref)} remains at {describe_revision(current_revision)}",
                )
            else:
                log_status("UPDATE", f"{style_branch(desired_ref)} advanced to {describe_revision(revision)}")
            print(revision)
            write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)
            return True


def command_resolve_opaque_unit(args: argparse.Namespace) -> bool:
    """Resolve an unparseable cleanup root after external cleanup is confirmed."""

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    if not isinstance(args.uid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.uid):
        raise OperationError("resolve-opaque-unit requires a valid --uid")
    if not isinstance(args.deletion_generation, int) or args.deletion_generation < 1:
        raise OperationError("resolve-opaque-unit requires --deletion-generation >= 1")
    if not args.confirm_external_cleanup:
        raise OperationError("resolve-opaque-unit requires --confirm-external-cleanup")
    if not isinstance(args.reason, str) or not args.reason.strip() or len(args.reason) > 500:
        raise OperationError("resolve-opaque-unit requires a bounded non-empty --reason")

    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, None)
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        current = temporary / "current"
        candidate = temporary / "candidate"
        current_revision = observed_tree(desired_ref, current)
        if current_revision is None:
            raise OperationError(f"desired ref {desired_ref!r} has no state")
        lease_ref = effect_lease_ref(args.environment, desired_ref)
        lease_root = current
        if lease_ref is not None and lease_ref != desired_ref:
            lease_root, _lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                temporary / "leases",
            )

        opaque = load_desired_cleanup_roots(current).get(args.unit)
        if opaque is None:
            raise OperationError(f"no opaque cleanup root exists for {args.unit!r}")
        if opaque.metadata.uid != args.uid:
            raise OperationError(f"stale opaque cleanup UID fence for {args.unit!r}")
        deletion = opaque.metadata.deletion
        if deletion is None:
            raise OperationError(f"opaque cleanup root for {args.unit!r} is not marked for deletion")
        if deletion.generation != args.deletion_generation:
            raise OperationError(f"stale opaque cleanup deletion generation fence for {args.unit!r}")
        if deletion.resourceDigest != opaque_cleanup_content_digest(opaque):
            raise OperationError(f"opaque cleanup root {args.unit!r} changed after deletion started")
        if document_candidates(current / "units", args.unit):
            raise OperationError(f"opaque cleanup root for {args.unit!r} conflicts with a desired Unit")
        lease = load_desired_effect_leases(lease_root).get(args.unit)
        if lease is not None and effect_lease_active(lease):
            raise OperationError(f"active effect lease blocks opaque resolution for {args.unit!r}")
        if isinstance(opaque.payload, dict):
            try:
                parse_desired_unit_document(cast(dict[str, Any], opaque.payload), args.unit)
            except (DocumentFormatError, DriverError, KeyError, TypeError, ValueError, OperationError):
                pass
            else:
                raise OperationError(f"opaque cleanup root for {args.unit!r} is parseable; use recover-opaque-unit")

        shutil.copytree(current, candidate)
        for path in document_candidates(candidate / DESIRED_CLEANUP_UNITS_PATH, args.unit):
            path.unlink()
        api_version, kind = opaque_resource_gvk(opaque.payload)
        write_resource_incarnation_tombstone(
            candidate,
            ResourceIncarnationTombstone(
                api_version=api_version,
                kind=kind,
                name=args.unit,
                uid=args.uid,
                deletion_generation=deletion.generation,
            ),
        )
        blocks = load_desired_transition_blocks(candidate)
        blocks.pop(args.unit, None)
        write_desired_transition_blocks(candidate, blocks)
        load_desired_resource_graph(candidate)
        if args.dry:
            log_status("DRY", f"{style_unit(args.unit)}: opaque cleanup resolution would be published")
            return False

        candidate_id = candidate_identifier(
            "resolve-opaque-unit",
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            {"unit": args.unit, "uid": args.uid, "reason": args.reason},
        )
        candidate_ref = resolve_candidate_ref(
            REPOSITORY_ROOT,
            args.environment,
            "resolve-opaque-unit",
            candidate_id,
            args.candidate_ref,
        )
        if candidate_ref_conflicts(candidate_ref, desired_ref, observed_ref):
            raise OperationError("opaque resolution candidate ref conflicts with deployment state")
        revision, outcome = publish_desired_change(
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            candidate_ref,
            f"Resolve opaque cleanup for {args.unit}: {args.reason.strip()}",
            f"Resolve opaque cleanup for {args.unit}",
            (
                f"Record operator-confirmed external cleanup for opaque Unit `{args.unit}` "
                f"(UID `{args.uid}`). Reason: {args.reason.strip()}"
            ),
            False,
            current,
            request_change=False,
            finalized_resources=frozenset(
                {
                    ResourceFinalizationFence(
                        api_version,
                        kind,
                        args.unit,
                        args.uid,
                        deletion.generation,
                    )
                }
            ),
            conflicting_refs=(observed_ref,),
        )
        if outcome is not None:
            log_status(
                "REVIEW",
                f"{style_branch(candidate_ref)} submitted at {describe_revision(revision)}; "
                f"{style_branch(desired_ref)} remains at {describe_revision(current_revision)}",
            )
        else:
            log_status("UPDATE", f"{style_branch(desired_ref)} advanced to {describe_revision(revision)}")
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)
        return True


def desired_metadata_for_candidate(
    authored: UnitResource[Any],
    previous: UnitResource[Any] | None,
    source: DesiredSource | None = None,
    source_revision: str | None = None,
    finalized_uids: Sequence[str] = (),
    partition: str | None = None,
) -> ResourceMetadata:
    """Select a durable desired identity without reusing a colliding incarnation."""

    if previous is None:
        return root_metadata_for_resource(
            authored,
            source=source,
            source_revision=source_revision,
            finalized_uids=finalized_uids,
        ).with_partition(partition)
    previous.metadata.validate_desired()
    if resource_owner_reference(previous) is not None:
        raise OperationError(
            f"desired unit {authored.name!r} collides with a UID-fenced owned resource; refusing source adoption"
        )
    if resource_deletion(previous) is not None:
        raise OperationError(f"desired unit {authored.name!r} is deleting and cannot be applied")
    if partition is not None and previous.metadata.partition not in {None, partition}:
        raise OperationError(f"desired unit {authored.name!r} belongs to partition {previous.metadata.partition!r}")
    if previous.gvk != authored.gvk or previous.driver_name != authored.driver_name:
        raise OperationError(f"desired unit {authored.name!r} changes GVK/driver; delete the previous resource first")
    return previous.metadata.with_partition(partition, preserve_existing=partition is None)


def _current_desired_unit_paths(current_desired: Path) -> dict[str, Path]:
    units = current_desired / "units"
    paths: dict[str, Path] = {}
    relative_stems = (
        sorted(
            {
                path.relative_to(units).with_suffix("").as_posix()
                for path in units.rglob("*")
                if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}
            }
        )
        if units.is_dir()
        else []
    )
    for qualified_name in relative_stems:
        relative = PurePosixPath(qualified_name)
        directory = units.joinpath(*relative.parts[:-1])
        stem = relative.parts[-1]
        candidates = document_candidates(directory, stem)
        if len(candidates) > 1:
            raise OperationError(f"multiple document formats exist for Unit {qualified_name}")
        if candidates:
            paths[qualified_name] = candidates[0]
    return paths


def build_desired_candidate(
    environment_name: str,
    source_root: Path,
    source_revision: str | None,
    current_desired: Path,
    observed: Path,
    observed_revision: str | None,
    candidate: Path,
    promotion: PromotionContext | None = None,
    dry: bool = False,
    verbose: bool = True,
    source_revision_policy: SourceRevisionPolicy | None = None,
    source_revision_operation: Literal["apply", "plan"] = "apply",
    preserve_stack_owned_metadata: bool = False,
    partition: str | None = None,
    source_context_root: Path | None = None,
    stack_template_document_digests: Mapping[str, str] | None = None,
    projection_context: JsonObject | None = None,
    projection_stack_names: frozenset[str] | None = None,
) -> BuildDesiredResult:
    authenticated_workload_revisions = _hydrate_required_stack_workload_pins(
        environment_name,
        current_desired,
    )
    if verbose:
        log_heading(f"Resolve desired state for {style_environment(environment_name)}")
        log_status("SOURCE", f"candidate revision {describe_revision(source_revision)}")
        log_status("DESIRED", "no current state" if not any(current_desired.iterdir()) else "loaded")
        log_status(
            "OBSERVED",
            f"revision {describe_revision(observed_revision)}" if observed_revision else "no observations yet",
        )
    specifications = load_environment_specifications(source_root, environment_name)
    candidate_units = candidate / "units"
    candidate_units.mkdir(parents=True, exist_ok=True)
    (candidate / "stack-templates").mkdir(parents=True, exist_ok=True)
    (candidate / "stacks").mkdir(parents=True, exist_ok=True)
    copy_resource_incarnation_tombstones(current_desired, candidate)
    copy_projection_context(current_desired, candidate)
    project = load_project_config(source_root)
    source_has_stack_graph = bool(
        _document_paths(source_root.joinpath(*project.stack_templates_path.parts))
        or _document_paths(project_environment_root(source_root, environment_name) / "stacks")
    )
    if (
        projection_context is None
        and not _current_desired_stack_paths(current_desired, "Stack")
        and source_has_stack_graph
    ):
        # A new explicitly applied Stack graph has no prior binding to load;
        # create its operation context before projection.  File-less
        # progression with existing Stacks must take the fail-closed path in
        # ``project_stack_resources`` instead of manufacturing a new context.
        projection_context = capture_projection_context(source_root, environment_name)
    if projection_context is not None:
        write_projection_context(candidate, projection_context)
    stack_projection = project_stack_resources(
        source_root,
        environment_name,
        source_revision,
        candidate,
        source_root,
        current_desired,
        promotion,
        partition,
        source_context_root,
        stack_template_document_digests,
        projection_context,
        projection_stack_names,
        authenticated_workload_revisions,
    )
    imported_artifact_fingerprints: dict[str, dict[str, str]] = {}
    imported_artifact_evidence: dict[str, dict[str, ResolvedArtifactImport]] = {}
    for unit_name, generated_unit in stack_projection.generated_units.items():
        if unit_name in specifications:
            raise OperationError(f"generated Stack Unit {unit_name!r} collides with a source Unit")
        specifications[unit_name] = generated_unit
    stack_promotions: dict[str, PromotionContext | None] = {}
    stack_environment_documents: dict[str, Mapping[str, Any]] = {}
    promotion_context_cache: dict[str, PromotionContext | None] = {}
    if promotion is None:
        for owner in stack_projection.owners.values():
            if owner.name in stack_promotions:
                continue
            # The projection engine has already selected the binding that is
            # authoritative for this operation.  Read the resulting Stack,
            # rather than the old desired Stack, so an explicitly reapplied
            # root resolves from its new operation context while a carried
            # fan-out root keeps its retained context.
            stack_path = _current_desired_stack_paths(candidate, "Stack").get(owner.name)
            if stack_path is None:
                stack_path = _current_desired_stack_paths(current_desired, "Stack").get(owner.name)
            if stack_path is None:
                if projection_context is not None:
                    stack_environment_documents[owner.name] = normalize_environment_document(
                        cast(dict[str, Any], projection_context["environmentDocument"]),
                        environment_name,
                    )
                else:
                    stack_environment_documents[owner.name] = load_environment(source_root, environment_name)
                continue
            stack_resource = RESOURCE_CATALOG.parse_stack(
                RESOURCE_CATALOG.load_document(stack_path), profile="desired", expected_name=owner.name
            )
            if not isinstance(stack_resource.spec, DesiredStackSpec):
                stack_environment_documents[owner.name] = load_environment(source_root, environment_name)
                continue
            context_digest = stack_resource.spec.structuralProjection.identity.projectionContextDigest
            context = load_projection_context(candidate, context_digest, environment_name)
            stack_environment_documents[owner.name] = normalize_environment_document(
                cast(dict[str, Any], context["environmentDocument"]),
                environment_name,
            )
            if context_digest not in promotion_context_cache:
                promotion_context_cache[context_digest] = load_promotion_context(
                    candidate,
                    candidate.parent,
                    context_digest,
                )
            stack_promotions[owner.name] = promotion_context_cache[context_digest]
    else:
        operation_environment = load_environment(source_root, environment_name)
        for owner in stack_projection.owners.values():
            stack_environment_documents[owner.name] = operation_environment
    operation_environment = load_environment(source_root, environment_name)
    if source_revision_policy is None:
        source_revision_policy = (
            load_project_config(source_root).source_revision_policy
            if any((source_root / name).is_file() for name in PROJECT_CONFIG_NAMES)
            else SourceRevisionPolicy()
        )
    if promotion is not None:
        write_preferred_document(candidate / "promotion.json", promotion.document(), source_root)
    else:
        promotion_paths = document_candidates(current_desired, "promotion")
        if len(promotion_paths) > 1:
            raise OperationError("multiple promotion document formats exist")
        if promotion_paths:
            promotion_document = load_json(promotion_paths[0])
            validate_document(
                CORE_CONTRACTS["promotion"],
                normalize_promotion_document(promotion_document),
                "promotion document",
            )
            target = candidate / promotion_paths[0].name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(promotion_paths[0], target)

    prepared: dict[str, tuple[UnitResource[Any], ResolvedUnitSourceResult]] = {}
    retained_transitions: dict[str, UnitResource[Any]] = {}
    opaque_transitions = load_desired_cleanup_roots(current_desired)
    opaque_transitions = {
        name: (
            mark_opaque_cleanup_for_deletion(opaque)
            if partition is not None and opaque.metadata.partition == partition and name not in specifications
            else opaque
        )
        for name, opaque in opaque_transitions.items()
    }
    blocked_transitions = load_desired_transition_blocks(current_desired)
    blocked: dict[str, str] = {}
    cleanup_inputs: dict[str, DesiredCleanupInput] = {}
    refreshes: dict[str, str] = {}
    for unit_name, specification in specifications.items():
        if unit_name in opaque_transitions:
            blocked_transitions.setdefault(unit_name, "opaque cleanup root retained pending explicit adoption")
            if verbose:
                log_status("WAIT", f"{style_unit(unit_name)}: {blocked_transitions[unit_name]}")
            continue
        previous_unit = unit_document_path(current_desired, unit_name)
        previous = None
        if previous_unit.is_file():
            try:
                previous = load_desired_unit(previous_unit, previous_unit.stem)
            except Exception:
                opaque_payload = opaque_document_payload(previous_unit)
                opaque_transitions[unit_name] = OpaqueCleanupRoot(
                    path=previous_unit,
                    payload=opaque_payload,
                    metadata=opaque_cleanup_metadata(unit_name, opaque_payload, source_revision),
                    source=raw_document_source(opaque_payload),
                )
                blocked_transitions[unit_name] = "previous desired unit is unavailable; opaque cleanup root retained"
                if verbose:
                    log_status(
                        "RETAIN",
                        f"{style_unit(unit_name)}: unavailable previous unit; retain opaque cleanup root",
                    )
                continue
        if previous is not None:
            previous.metadata.validate_desired()
            owner = resource_owner_reference(previous)
            if owner is not None:
                if previous.gvk != specification.gvk or previous.driver_name != specification.driver_name:
                    raise OperationError(f"desired unit {unit_name!r} collides with a UID-owned resource")
        if previous is not None and (
            previous.gvk != specification.gvk or previous.driver_name != specification.driver_name
        ):
            retained = previous
            retained_transitions[unit_name] = retained
            blocked_transitions[unit_name] = "desired resource identity changed; previous cleanup root retained"
            if verbose:
                log_status(
                    "RETAIN",
                    f"{style_unit(unit_name)}: GVK/driver changed; retain previous desired cleanup root",
                )
            continue
        selected_source_context = stack_projection.source_contexts.get(unit_name)
        unit_source_root = selected_source_context.root if selected_source_context is not None else source_root
        unit_source_revision = (
            selected_source_context.revision if selected_source_context is not None else source_revision
        )
        authored_source = getattr(specification.spec, "source", None)
        if (
            selected_source_context is None
            and isinstance(authored_source, AuthoredSource)
            and authored_source.revision is not None
        ):
            raise OperationError(f"Unit {unit_name!r}: source.revision is supported only in a StackTemplate projection")
        source_resolution = (
            resolved_unit_source(
                specification,
                unit_source_root,
                unit_source_revision,
                current_desired,
                source_revision_policy,
                source_revision_operation,
                preserve_prior_revision=False,
            )
            if unit_name in stack_projection.owners
            else resolved_unit_source(
                specification,
                unit_source_root,
                unit_source_revision,
                current_desired,
                source_revision_policy,
                source_revision_operation,
            )
        )
        prepared[unit_name] = (specification, source_resolution)
        if source_resolution.refresh_reason is not None:
            refreshes[unit_name] = source_resolution.refresh_reason
            if verbose:
                log_status("REFRESH", f"{style_unit(unit_name)}: {source_resolution.refresh_reason}")
            continue
        if not previous_unit.is_file():
            resolution_message = "new unit; use candidate revision"
        elif source_resolution.disposition is SourceResolutionDisposition.INPUTS_CHANGED:
            resolution_message = "inputs changed; use candidate revision"
        elif source_resolution.source is None:
            resolution_message = "source-less unit"
        else:
            resolution_message = f"inputs unchanged; retain {describe_revision(source_resolution.source.revision)}"
        if verbose:
            log_status("CHECK", f"{style_unit(unit_name)}: {resolution_message}")

    unresolved = set(prepared)
    unavailable: dict[str, str] = {}
    while unresolved:
        progressed = False
        for unit_name in sorted(unresolved):
            authored, source_resolution = prepared[unit_name]
            resolved_source = source_resolution.source
            unit_artifact_imports = stack_projection.artifact_imports.get(unit_name, ())
            unit_owner = stack_projection.owners.get(unit_name)
            target_stack_uid = unit_owner.uid if unit_owner is not None else None
            unit_environment_document = (
                stack_environment_documents.get(unit_owner.name, operation_environment)
                if unit_owner is not None
                else operation_environment
            )
            try:
                unit_promotion = promotion
                if unit_owner is not None and promotion is None:
                    unit_promotion = stack_promotions.get(unit_owner.name)
                resolution = authored.driver.resolve_unit(
                    authored.spec,
                    UnitResolutionContext(
                        source=resolved_source,
                        resolve_template=lambda value, pointer, target_unit=unit_name, target_gvk=authored.gvk, artifact_imports=unit_artifact_imports, target_stack_uid=target_stack_uid, unit_promotion=unit_promotion, unit_environment_document=unit_environment_document: (
                            resolve_template(
                                value,
                                candidate,
                                observed,
                                observed_revision,
                                promotion=unit_promotion,
                                target_unit=target_unit,
                                target_gvk=target_gvk,
                                pointer=pointer,
                                dry=dry,
                                environment_document=unit_environment_document,
                                artifact_imports=artifact_imports,
                                target_stack_uid=target_stack_uid,
                            )
                        ),
                    ),
                )
            except ReferenceUnavailable as exc:
                unavailable[unit_name] = str(exc)
                continue
            resolved = authored.with_spec(resolution.unit)
            resolved = materialize_resolved_unit(
                environment_name,
                resolved,
                unit_name,
                unit_source_root,
                unit_source_revision,
                current_desired,
                candidate,
            )
            previous_unit = unit_document_path(current_desired, unit_name)
            previous = load_desired_unit(previous_unit, previous_unit.stem) if previous_unit.is_file() else None
            finalized_uids = (
                tuple(
                    sorted(
                        tombstone.uid
                        for tombstone in load_resource_incarnation_evidence(current_desired)
                        if tombstone.api_version == resolved.gvk.api_version
                        and tombstone.kind == resolved.gvk.kind
                        and tombstone.qualified_name == unit_name
                    )
                )
                if previous is None
                else ()
            )
            owner = stack_projection.owners.get(unit_name)
            if owner is not None and preserve_stack_owned_metadata and previous is not None:
                previous_owner = resource_owner_reference(previous)
                if previous_owner is not None and previous_owner.kind == "Stack" and previous_owner.name == owner.name:
                    resolved = resolved.with_metadata(previous.metadata)
                else:
                    resolved = resolved.with_metadata(_stack_owned_metadata(authored.name, owner))
            else:
                resolved = resolved.with_metadata(
                    _stack_owned_metadata(authored.name, owner)
                    if owner is not None
                    else desired_metadata_for_candidate(
                        authored,
                        previous,
                        resolved_source,
                        source_revision,
                        finalized_uids=finalized_uids,
                        partition=partition,
                    )
                )
            candidate_unit = write_desired_candidate_unit(
                unit_document_path(candidate, unit_name, source_root), resolved, source_root
            )
            previous_inputs = getattr(previous.spec, "resolvedInputs", None) if previous is not None else None
            previous_receipts = previous_inputs.receipts if previous_inputs is not None else None
            previous_artifacts = previous_inputs.artifacts if previous_inputs is not None else None
            previous_promotions = previous_inputs.promotions if previous_inputs is not None else None
            fingerprints = resolution.resolved_inputs
            promotions = fingerprints.promotions if fingerprints is not None else None
            receipts = fingerprints.receipts if fingerprints is not None else None
            artifacts = fingerprints.artifacts if fingerprints is not None else None
            imported_artifacts = fingerprints.importedArtifacts if fingerprints is not None else None
            if imported_artifacts:
                owner = stack_projection.owners.get(unit_name)
                if owner is not None:
                    imported_artifact_fingerprints.setdefault(owner.name, {}).update(imported_artifacts)
            if fingerprints is not None and fingerprints.importedArtifactEvidence:
                owner = stack_projection.owners.get(unit_name)
                if owner is not None:
                    imported_artifact_evidence.setdefault(owner.name, {}).update(
                        {
                            key: ResolvedArtifactImport.from_dict(cast(dict[str, Any], evidence))
                            for key, evidence in fingerprints.importedArtifactEvidence.items()
                        }
                    )
            if promotions:
                promotion_resolution = (
                    "new promotion changes resolved inputs"
                    if previous_promotions != promotions
                    else "promotion already matches resolved inputs"
                )
                if verbose:
                    log_status("PROMOTE", f"{style_unit(unit_name)}: {promotion_resolution}")
            if receipts or artifacts:
                observation_resolution = (
                    "new observation changes resolved inputs"
                    if previous_receipts != receipts or previous_artifacts != artifacts
                    else "observations already match resolved inputs"
                )
                if verbose:
                    log_status("OBSERVE", f"{style_unit(unit_name)}: {observation_resolution}")
            changed = not previous_unit.is_file() or previous_unit.read_bytes() != candidate_unit.read_bytes()
            if verbose:
                log_status(
                    "UPDATE" if changed else "KEEP",
                    f"{style_unit(unit_name)}: {'desired state changed' if changed else 'already resolved'}",
                )
            unresolved.remove(unit_name)
            unavailable.pop(unit_name, None)
            blocked_transitions.pop(unit_name, None)
            progressed = True
        if not progressed:
            break

    for unit_name in sorted(unresolved):
        previous = unit_document_path(current_desired, unit_name)
        previous_driver = persisted_unit_driver_name(previous) if previous.is_file() else None
        authored, source_resolution = prepared[unit_name]
        next_driver = authored.driver_name
        if carry_forward_refreshed_unit(
            source_root, current_desired, candidate, candidate_units, authored, source_resolution
        ):
            resolution = "preserve last resolved inputs"
            if verbose:
                log_status("CARRY", f"{style_unit(unit_name)}: {unavailable[unit_name]}; {resolution}")
        elif previous_driver == next_driver:
            previous_resource = load_desired_unit(previous, previous.stem)
            previous_owner = resource_owner_reference(previous_resource)
            stack_owner = stack_projection.owners.get(unit_name)
            retained_metadata = (
                previous_resource.metadata
                if stack_owner is not None
                and previous_owner is not None
                and previous_owner.kind == "Stack"
                and previous_owner.name == stack_owner.name
                else desired_metadata_for_candidate(
                    authored,
                    previous_resource,
                    source_resolution.source,
                    source_revision,
                    partition=partition,
                )
            )
            retained = previous_resource.with_metadata(retained_metadata)
            write_desired_candidate_unit(unit_document_path(candidate, unit_name, source_root), retained, source_root)
            copy_unit_materialization(current_desired, candidate, unit_name, previous_resource)
            resolution = "retain previous desired state"
        elif previous_driver is not None:
            resolution = f"omit previous {previous_driver} desired state while transitioning to {next_driver}"
        else:
            resolution = "omit until its inputs are available"
        if verbose:
            log_status("WAIT", f"{style_unit(unit_name)}: {unavailable[unit_name]}; {resolution}")
        blocked_transitions[unit_name] = unavailable[unit_name]
        blocked[unit_name] = unavailable[unit_name]

    for stack_name, _fingerprints in imported_artifact_fingerprints.items():
        stack_path = next(
            iter(document_candidates(candidate / "stacks", stack_name)),
            None,
        )
        if stack_path is None:
            continue
        stack_resource = RESOURCE_CATALOG.parse_stack(
            RESOURCE_CATALOG.load_document(stack_path), profile="desired", expected_name=stack_name
        )
        if not isinstance(stack_resource.spec, DesiredStackSpec):
            continue
        stack_resource = replace(
            stack_resource,
            spec=replace(
                stack_resource.spec,
                resolvedArtifactImports=imported_artifact_evidence.get(stack_name),
            ),
        )
        _write_desired_stack_resource(stack_path, stack_resource, source_root)

    atomically_retained_units = _bind_active_stack_projections(
        candidate,
        current_desired,
        blocked_transitions,
        source_root,
    )

    for unit_name, retained in retained_transitions.items():
        previous_path = unit_document_path(current_desired, unit_name)
        write_desired_candidate_unit(unit_document_path(candidate, unit_name, source_root), retained, source_root)
        if getattr(retained.spec, "materialization", None) is not None:
            copy_unit_materialization(current_desired, candidate, unit_name, retained)
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=retained,
            source=getattr(retained.spec, "source", None),
        )
        retained = cast(UnitResource[Any], mark_resource_for_deletion(retained))
        write_desired_candidate_unit(unit_document_path(candidate, unit_name, source_root), retained, source_root)
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=retained,
            source=getattr(retained.spec, "source", None),
        )
    for unit_name, previous_path in _current_desired_unit_paths(current_desired).items():
        if unit_name in specifications:
            continue
        if unit_name in atomically_retained_units:
            continue
        try:
            previous = load_desired_unit(previous_path, previous_path.stem)
        except Exception:
            opaque_payload = opaque_document_payload(previous_path)
            opaque_transitions[unit_name] = OpaqueCleanupRoot(
                path=previous_path,
                payload=opaque_payload,
                metadata=opaque_cleanup_metadata(unit_name, opaque_payload, source_revision),
                source=raw_document_source(opaque_payload),
            )
            blocked_transitions[unit_name] = "opaque desired root retained"
            continue
        previous_owner = resource_owner_reference(previous)
        if previous_owner is not None and previous_owner.kind == "Stack":
            if previous_owner.name not in stack_projection.applied_stacks:
                # The apply contract does not select this Stack. Leave its
                # root and complete owned closure byte-for-byte for the
                # candidate copy step below.
                continue
        retained = previous
        write_desired_candidate_unit(unit_document_path(candidate, unit_name, source_root), retained, source_root)
        if getattr(retained.spec, "materialization", None) is not None:
            copy_unit_materialization(current_desired, candidate, unit_name, previous)
        owner = resource_owner_reference(retained)
        removed_from_applied_stack = (
            owner is not None and owner.kind == "Stack" and owner.name in stack_projection.applied_stacks
        )
        omitted_partition_root = partition is not None and owner is None and retained.metadata.partition == partition
        if removed_from_applied_stack or omitted_partition_root:
            retained = cast(UnitResource[Any], mark_resource_for_deletion(retained))
            write_desired_candidate_unit(unit_document_path(candidate, unit_name, source_root), retained, source_root)
            cleanup_inputs[unit_name] = DesiredCleanupInput(
                unit_name=unit_name,
                desired=retained,
                source=getattr(retained.spec, "source", None),
            )
    # Publish transition blocks before validating the graph. A Stack with an
    # unavailable downstream input may intentionally omit that generated Unit
    # until the upstream artifact becomes observed.
    write_desired_transition_blocks(candidate, blocked_transitions)

    resources = load_desired_resource_graph(candidate, validate=False)
    deleting = [resource for resource in resources.values() if resource_deletion(resource) is not None]
    for parent in deleting:
        for child in _owned_resource_closure(resources, parent):
            if child.name == parent.name or resource_deletion(child) is not None:
                continue
            marked_child = mark_resource_for_deletion(child)
            _write_desired_resource(_desired_resource_path(candidate, child), marked_child)
    for unit_name, opaque in opaque_transitions.items():
        write_opaque_cleanup_root(candidate, unit_name, opaque)
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=None,
            source=opaque.source,
            raw_document=opaque.payload if isinstance(opaque.payload, dict) else None,
        )
        blocked[unit_name] = blocked_transitions[unit_name]
    write_desired_transition_blocks(candidate, blocked_transitions)
    return BuildDesiredResult(
        blocked=blocked,
        cleanup_inputs=cleanup_inputs,
        blocked_transitions=blocked_transitions,
        refreshes=refreshes,
        reprojected_stacks=stack_projection.applied_stacks,
    )


def retryable_push_failure(exc: subprocess.CalledProcessError) -> bool:
    detail = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
    return any(marker in detail for marker in ("non-fast-forward", "fetch first", "stale info", "failed to push"))


def require_revision(value: Any, description: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise OperationError(f"{description} must be a full Git commit")
    return value


def _promotion_context_from_document(
    document: dict[str, Any],
    temporary: Path,
    context_digest: str | None = None,
) -> PromotionContext:
    document = normalize_promotion_document(document)
    validate_document(CORE_CONTRACTS["promotion"], document, "promotion.json")
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {
        "environment",
        "desiredRef",
        "desiredRevision",
        "observedRef",
        "observedRevision",
    }:
        raise OperationError("promotion.json has an invalid source")
    source_environment = source.get("environment")
    if not isinstance(source_environment, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source_environment):
        raise OperationError("promotion.json has an invalid source environment")
    desired_ref = source.get("desiredRef")
    observed_ref = source.get("observedRef")
    if not all(isinstance(ref, str) and ref for ref in (desired_ref, observed_ref)):
        raise OperationError("promotion.json has invalid source refs")
    desired_revision = require_revision(source.get("desiredRevision"), "promotion source desiredRevision")
    observed_value = source.get("observedRevision")
    observed_revision = (
        None if observed_value is None else require_revision(observed_value, "promotion source observedRevision")
    )
    specification_revision = require_revision(document.get("specificationRevision"), "promotion specificationRevision")
    if resolve_ref(desired_ref, desired_revision) != desired_revision:
        raise OperationError("promotion source desired revision changed unexpectedly")
    if observed_revision is not None and resolve_ref(observed_ref, observed_revision) != observed_revision:
        raise OperationError("promotion source observed revision changed unexpectedly")
    materialization_key = (
        context_digest.removeprefix("sha256:")
        if context_digest is not None
        else hashlib.sha256(canonical_json(document)).hexdigest()
    )
    desired_root = temporary / f"promotion-{materialization_key}" / "source"
    materialize_revision(desired_revision, desired_root)
    observed_root = None
    if observed_revision is not None:
        observed_root = desired_root.parent / "observed"
        materialize_revision(observed_revision, observed_root)
    return PromotionContext(
        source_environment=source_environment,
        desired_ref=desired_ref,
        desired_revision=desired_revision,
        observed_ref=observed_ref,
        observed_revision=observed_revision,
        specification_revision=specification_revision,
        desired_root=desired_root,
        observed_root=observed_root,
    )


def load_promotion_context(
    current_desired: Path,
    temporary: Path,
    context_digest: str | None = None,
) -> PromotionContext | None:
    if context_digest is not None:
        context = load_projection_context(current_desired, context_digest)
        promotion_document = context.get("promotionDocument")
        if promotion_document is None:
            return None
        if not isinstance(promotion_document, dict):
            raise OperationError("projection context has an invalid promotionDocument")
        return _promotion_context_from_document(promotion_document, temporary, context_digest)
    paths = document_candidates(current_desired, "promotion")
    if not paths:
        return None
    if len(paths) > 1:
        raise OperationError("multiple promotion document formats exist")
    return _promotion_context_from_document(load_json(paths[0]), temporary)


def historical_receipt_matches(desired: Path, observed: Path, unit_name: str) -> bool:
    unit_path = unit_document_path(desired, unit_name)
    receipt_path = receipt_document_path(observed, unit_name)
    if not unit_path.is_file():
        return False
    unit = load_desired_unit(unit_path, unit_name)
    try:
        validate_unit_materialization(desired, unit_name, unit)
        if not unit_requires_reconciliation(unit):
            return True
    except (DriverError, OperationError):
        return False
    if not receipt_path.is_file():
        return False
    try:
        receipt = load_receipt(receipt_path, unit_name)
    except OperationError:
        return False
    driver = unit.driver_name
    desired_evidence = receipt.spec.desired
    if (
        receipt.name != PurePosixPath(unit_name).name
        or receipt.driver_name != driver
        or not re.fullmatch(r"[0-9a-f]{40}", str(desired_evidence.revision or ""))
        or desired_evidence.unitBlob != file_blob(unit_path)
    ):
        return False
    try:
        validate_receipt_artifacts(observed, unit, receipt)
        semantic_reconciliation_result(driver, receipt.status.result, receipt.status.artifacts)
    except DriverError:
        return False
    return True


def require_clean_source(desired: Path, observed: Path, minimum_evidence: str = "reconciled") -> None:
    unit_names = sorted(_current_desired_unit_paths(desired))
    if not unit_names:
        raise OperationError("promotion source desired state has no units")
    unresolved = [
        unit_name
        for unit_name in unit_names
        if unit_contains_reference(load_desired_unit(unit_document_path(desired, unit_name), unit_name))
    ]
    if unresolved:
        raise OperationError(f"promotion source has unresolved desired units: {', '.join(unresolved)}")
    statuses = reconciliation_statuses(unit_names, desired, observed)
    accepted = {"CLEAN", "MATERIALIZED"} if minimum_evidence == "materialized" else {"CLEAN"}
    unclean = [f"{unit_name} ({status.lower()})" for unit_name, status, _ in statuses if status not in accepted]
    if unclean:
        raise OperationError(f"promotion source is not fully reconciled: {', '.join(unclean)}")


def desired_specification_revision(
    desired_revision: str,
    desired: Path,
    temporary: Path,
) -> str:
    promotion = load_promotion_context(desired, temporary)
    if promotion is not None:
        return promotion.specification_revision
    subjects = git("log", "--format=%s", desired_revision).stdout.splitlines()
    for subject in subjects:
        match = re.fullmatch(r"Desired [a-z0-9][a-z0-9-]* state from ([0-9a-f]{40})", subject)
        if match is not None:
            return match.group(1)
    raise OperationError(
        f"desired revision {describe_revision(desired_revision)} does not record its specification revision"
    )


def find_clean_observed_snapshot(
    observed_ref: str,
    desired: Path,
    unit_names: list[str],
    temporary: Path,
) -> str | None:
    receipt_units = [
        unit_name
        for unit_name in unit_names
        if unit_requires_reconciliation(load_desired_unit(unit_document_path(desired, unit_name), unit_name))
    ]
    if not receipt_units:
        return None
    observed_head = fetch_ref(observed_ref)
    if observed_head is None:
        raise OperationError(f"{observed_ref} has no observation history")
    revisions = git("rev-list", observed_head).stdout.splitlines()
    for index, revision in enumerate(revisions):
        observed = temporary / f"observed-{index}"
        materialize_revision(revision, observed)
        if all(historical_receipt_matches(desired, observed, unit_name) for unit_name in receipt_units):
            return revision
    raise OperationError("rollback target was never fully clean in one observed-state snapshot")


def validate_effect_leases_preserved(
    target_ref: str,
    target_revision: str | None,
    candidate: Path,
    current_root: Path | None = None,
    allow_removed_units: frozenset[str] = frozenset(),
    lease_ref: str | None = None,
) -> None:
    """Prevent a desired-ref mutation from dropping an in-flight effect fence."""

    if target_revision is None:
        return
    temporary_directory = None
    lease_temporary_directory = None
    if current_root is None:
        temporary_directory = tempfile.TemporaryDirectory()
        current = Path(temporary_directory.name) / "current"
        materialize_revision(target_revision, current)
    else:
        current = current_root
    try:
        lease_root = current
        if lease_ref is not None and lease_ref != target_ref:
            lease_temporary_directory = tempfile.TemporaryDirectory()
            lease_root = Path(lease_temporary_directory.name) / "leases"
            lease_revision = fetch_ref(lease_ref)
            if lease_revision is None:
                return
            materialize_revision(lease_revision, lease_root)
        active = {
            name: lease for name, lease in load_desired_effect_leases(lease_root).items() if effect_lease_active(lease)
        }
        if not active:
            return
        candidate_leases = (
            {} if lease_ref is not None and lease_ref != target_ref else load_desired_effect_leases(candidate)
        )
        empty_snapshot = EffectLeaseSnapshot(
            unit_path=None,
            unit_blob=None,
            api_version=None,
            kind=None,
            driver=None,
            source_revision=None,
            cleanup_path=None,
            cleanup_blob=None,
        )
        for name, lease in sorted(active.items()):
            candidate_lease = candidate_leases.get(
                name, lease if lease_ref is not None and lease_ref != target_ref else None
            )
            if lease.snapshot is None:
                raise EffectLeaseUnavailable(
                    f"active effect lease for {name!r} lacks an immutable snapshot; explicit recovery is required"
                )
            current_snapshot = effect_lease_snapshot(current, name, lease.uid)
            if current_snapshot != lease.snapshot:
                raise EffectLeaseUnavailable(
                    f"leased desired state for {name!r} changed before publication; explicit recovery is required"
                )
            candidate_snapshot = effect_lease_snapshot(candidate, name, lease.uid)
            if name in allow_removed_units and candidate_lease is None and candidate_snapshot == empty_snapshot:
                continue
            if candidate_lease != lease:
                raise EffectLeaseUnavailable(
                    f"desired-state mutation would drop or alter active effect lease for {name!r}"
                )
            if candidate_snapshot == lease.snapshot:
                continue
            if name in allow_removed_units and candidate_snapshot == empty_snapshot:
                continue
            raise EffectLeaseUnavailable(
                f"desired-state mutation changed the immutable leased resource or cleanup input for {name!r}"
            )
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
        if lease_temporary_directory is not None:
            lease_temporary_directory.cleanup()


def copy_active_effect_leases(current: Path, candidate: Path) -> None:
    """Carry active effect fences into a promotion candidate built from source state."""

    for lease in load_desired_effect_leases(current).values():
        if effect_lease_active(lease):
            write_effect_lease(candidate, lease)


def publish_change_candidate(
    candidate: Path,
    candidate_ref: str,
    target_ref: str,
    target_revision: str | None,
    commit_message: str,
    title: str,
    body: str,
    current_root: Path | None = None,
    allow_removed_units: frozenset[str] = frozenset(),
    request_change: bool = True,
    lease_ref: str | None = None,
    source_pins: Mapping[str, str] | None = None,
    conflicting_refs: Sequence[str] = (),
) -> tuple[str, ChangeRequestResult | ManualChangeRequest | None]:
    canonical_candidate_ref = canonical_publication_ref(candidate_ref)
    canonical_target_ref = canonical_publication_ref(target_ref)
    complete_conflicting_refs = (*conflicting_refs, lease_ref) if lease_ref is not None else conflicting_refs
    canonical_conflicting_refs = {canonical_publication_ref(ref) for ref in complete_conflicting_refs}
    candidate_ref = canonical_candidate_ref
    target_ref = canonical_target_ref
    if canonical_candidate_ref == canonical_target_ref:
        raise OperationError("change candidate ref conflicts with target desired state")
    if canonical_candidate_ref in canonical_conflicting_refs:
        raise OperationError("change candidate ref conflicts with deployment state")
    load_desired_resource_graph(candidate)
    validate_effect_leases_preserved(
        target_ref,
        target_revision,
        candidate,
        current_root,
        allow_removed_units,
        lease_ref=lease_ref,
    )
    existing_candidate = fetch_ref(candidate_ref)
    if existing_candidate is not None:
        with tempfile.TemporaryDirectory() as existing_directory:
            existing_root = Path(existing_directory) / "candidate"
            materialize_revision(existing_candidate, existing_root)
            parent_result = git("rev-parse", f"{existing_candidate}^", check=False)
            message_result = git("show", "-s", "--format=%B", existing_candidate, check=False)
            if (
                parent_result.returncode != 0
                or message_result.returncode != 0
                or directory_files(existing_root) != directory_files(candidate)
                or parent_result.stdout.strip() != target_revision
                or message_result.stdout.rstrip("\n") != commit_message.rstrip("\n")
            ):
                raise OperationError(f"change candidate ref is occupied by a different proposal: {candidate_ref}")
        candidate_revision = existing_candidate
        log_status("KEEP", f"reuse existing candidate {style_branch(candidate_ref)}")
    else:
        if source_pins:
            candidate_revision = publish_tree(
                candidate_ref,
                candidate,
                target_revision,
                commit_message,
                source_pins,
                expected_publication_head=existing_candidate,
            )
        else:
            candidate_revision = publish_tree(
                candidate_ref,
                candidate,
                target_revision,
                commit_message,
                expected_publication_head=existing_candidate,
            )
    verify_gated_candidate(candidate_revision, target_revision)
    if existing_candidate is not None and source_pins:
        verify_owners = getattr(state_store(), "verify_publication_owners", None)
        if callable(verify_owners) and not verify_owners(candidate_ref, candidate_revision, source_pins):
            raise OperationError("existing candidate is missing its publication-owner refs")
    if not request_change:
        outcome = ManualChangeRequest(
            reason="change-request creation is delegated to the calling CI workflow",
            head=candidate_ref,
            base=target_ref,
            title=title,
            body=body,
            remote_url=None,
        )
        log_status("REVIEW", f"external change request required for {style_branch(candidate_ref)}")
        return candidate_revision, outcome
    outcome = ensure_change_request(
        ChangeRequestSpec(
            head=candidate_ref,
            base=target_ref,
            title=title,
            body=body,
        ),
        cwd=REPOSITORY_ROOT,
    )
    if isinstance(outcome, ChangeRequestResult):
        log_status("REVIEW", f"{outcome.status} pull request {outcome.url}")
    else:
        log_status("REVIEW", f"manual pull request required: {outcome.reason}")
        for line in outcome.instructions().splitlines():
            log_status("MANUAL", line)
    return candidate_revision, outcome


def write_change_outputs(
    revision: str,
    target_ref: str,
    candidate_ref: str = "",
    outcome: ChangeRequestResult | ManualChangeRequest | None = None,
) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        if isinstance(outcome, ChangeRequestResult):
            change_status = outcome.status
            change_url = outcome.url
        elif isinstance(outcome, ManualChangeRequest):
            change_status = "manual"
            change_url = ""
        else:
            change_status = "published"
            change_url = ""
        with Path(output).open("a") as stream:
            stream.write(f"change_revision={revision}\n")
            stream.write(f"target_ref={target_ref}\n")
            stream.write(f"candidate_ref={candidate_ref}\n")
            stream.write(f"change_status={change_status}\n")
            stream.write(f"change_url={change_url}\n")


def _initialize_gated_desired_ref(
    source_root: Path,
    environment: str,
    desired_ref: str,
    current: Path,
) -> str:
    """Create the inert parent required by a first pull-request-gated publication."""

    baseline = current.parent / f"{current.name}-baseline"
    baseline_environment = {"name": environment, "state": "unpromoted"}
    if resource_documents_enabled(source_root):
        write_document(
            baseline / f"environment{load_project_config(source_root).write_format.suffix}",
            serialize_environment_document(baseline_environment),
            format=load_project_config(source_root).write_format,
        )
    else:
        write_json(baseline / "environment.json", baseline_environment)
    revision = publish_tree(
        desired_ref,
        baseline,
        None,
        f"Initialize desired {environment} state",
        expected_publication_head=None,
    )
    shutil.copytree(baseline, current, dirs_exist_ok=True)
    log_status("INIT", f"created inert {style_branch(desired_ref)} at {describe_revision(revision)}")
    return revision


def command_promote(args: argparse.Namespace) -> None:
    dry = bool(getattr(args, "dry", False))
    specification_revision = git("rev-parse", f"{args.specification_revision or 'HEAD'}^{{commit}}").stdout.strip()
    _validate_apply_input_selection(
        args.files,
        specification_revision,
        operation="promotion",
        revision_option="--specification-revision",
    )
    log_heading(f"Promote {style_environment(args.from_environment)} to {style_environment(args.to_environment)}")
    log_status("SPEC", f"reviewed source {describe_revision(specification_revision)}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        source_root = temporary / "source"
        materialize_revision(specification_revision, source_root)
        explicit_documents = _load_apply_documents(
            args.files,
            source_revision=specification_revision,
            source_root=source_root,
            operation="promotion",
            revision_option="--specification-revision",
        )
        if not explicit_documents and args.partition is None:
            raise OperationError(
                "promotion produced zero documents; specify --partition for authoritative empty membership"
            )
        allowed_sources = allowed_promotion_sources(source_root, args.to_environment)
        if args.from_environment not in allowed_sources:
            raise OperationError(
                f"{args.from_environment} is not an allowed promotion source for {args.to_environment}"
            )

        source_desired_ref, source_observed_ref = deployment_refs(source_root, args.from_environment)
        source_lease_ref = effect_lease_ref(args.from_environment, source_desired_ref, source_root)
        source_desired_revision = resolve_ref(source_desired_ref, args.source_desired_revision)
        source_observed_revision = fetch_ref(source_observed_ref)
        source_desired = temporary / "source-desired"
        source_observed = temporary / "source-observed"
        materialize_revision(source_desired_revision, source_desired)
        if source_observed_revision is None:
            source_observed.mkdir(parents=True)
        else:
            materialize_revision(source_observed_revision, source_observed)
        evidence = minimum_promotion_evidence(source_root, args.from_environment)
        require_clean_source(source_desired, source_observed, evidence)
        evidence_label = "reconciled" if evidence == "reconciled" else "promotion-complete"
        log_status(
            "SOURCE",
            f"{style_branch(source_desired_ref)} {describe_revision(source_desired_revision)} is {evidence_label} at "
            f"{describe_revision(source_observed_revision)}",
        )

        target_desired_ref, target_observed_ref = deployment_refs(source_root, args.to_environment)
        current_target = temporary / "target-current"
        target_observed = temporary / "target-observed"
        candidate = temporary / "candidate"
        target_revision = observed_tree(target_desired_ref, current_target)
        target_observed_revision = observed_tree(target_observed_ref, target_observed)
        gate = change_gate(source_root, args.to_environment)
        promotion = PromotionContext(
            source_environment=args.from_environment,
            desired_ref=source_desired_ref,
            desired_revision=source_desired_revision,
            observed_ref=source_observed_ref,
            observed_revision=source_observed_revision,
            specification_revision=specification_revision,
            desired_root=source_desired,
            observed_root=source_observed,
        )
        explicit_source = temporary / "explicit-target"
        _copy_apply_source_base(source_root, explicit_source, args.to_environment)
        stack_template_document_digests: dict[str, str] = {}
        authored_units, authored_stacks = _write_apply_authored_documents(
            explicit_source,
            args.to_environment,
            explicit_documents,
            stack_template_document_digests,
        )
        projection_context = (
            capture_projection_context(explicit_source, args.to_environment, promotion)
            if any(item.document.get("kind") in {"Stack", "StackTemplate"} for item in explicit_documents)
            else None
        )
        if any(_document_is_canonical_desired(item.document) for item in explicit_documents):
            raise OperationError("promote accepts authored target input only")
        build_desired_candidate(
            args.to_environment,
            explicit_source,
            specification_revision,
            current_target,
            target_observed,
            target_observed_revision,
            candidate,
            promotion=promotion,
            dry=dry,
            partition=args.partition,
            source_context_root=source_root,
            stack_template_document_digests=stack_template_document_digests,
            projection_context=projection_context,
        )
        applied = set(_explicit_applied_root_identities(explicit_documents, [*authored_units, *authored_stacks]))
        _copy_unrelated_desired_resources(current_target, candidate, frozenset(applied), args.partition)
        _prune_omitted_partition_resources(current_target, candidate, frozenset(applied), args.partition)
        _reject_applied_stacks_against_deleting_templates(candidate, frozenset(applied))
        candidate_resources = load_desired_resource_graph(candidate)
        if target_revision is None and not candidate_resources:
            log_status("KEEP", f"{style_branch(target_desired_ref)} remains empty")
            return
        if target_revision is None and gate == "pullRequest" and not dry:
            target_revision = _initialize_gated_desired_ref(
                source_root,
                args.to_environment,
                target_desired_ref,
                current_target,
            )
        target_lease_ref = effect_lease_ref(args.to_environment, target_desired_ref, source_root)
        if target_lease_ref == target_desired_ref:
            copy_active_effect_leases(current_target, candidate)
        validate_effect_leases_preserved(
            target_desired_ref,
            target_revision,
            candidate,
            current_target,
            lease_ref=target_lease_ref,
        )

        if dry:
            log_status("DRY", f"{style_branch(target_desired_ref)} would receive the promotion")
            return

        validate_desired_resource_transition(current_target, candidate)

        if target_revision is not None and directory_files(current_target) == directory_files(candidate):
            _ensure_stack_template_source_pins(args.to_environment, candidate)
            _gc_superseded_stack_workload_pins(
                args.to_environment,
                candidate,
                AcceptedDesiredTarget(target_desired_ref, target_revision),
            )
            log_status("KEEP", f"{style_branch(target_desired_ref)} already contains this promotion")
            print(target_revision)
            write_change_outputs(target_revision, target_desired_ref)
            return

        commit_message = f"Promote {args.from_environment} to {args.to_environment} from {source_desired_revision}"
        title = f"Promote {args.from_environment} to {args.to_environment}"
        body = (
            f"Promotes reconciled desired state from `{source_desired_revision}`. "
            f"After merge, reconcile `{args.to_environment}`."
        )
        outcome: ChangeRequestResult | ManualChangeRequest | None = None
        pin_acquisition = _acquire_stack_template_source_pins(args.to_environment, candidate)
        source_pins = dict(_required_stack_template_source_pins(args.to_environment, candidate))
        published = False
        candidate_ref = ""
        try:
            if gate == "pullRequest":
                assert target_revision is not None
                candidate_id = candidate_identifier(
                    "promotion",
                    args.to_environment,
                    candidate,
                    target_desired_ref,
                    target_revision,
                    promotion.document(),
                )
                candidate_ref = resolve_candidate_ref(
                    source_root,
                    args.to_environment,
                    "promotion",
                    candidate_id,
                    args.candidate_ref,
                )
                promotion_conflicting_refs = (
                    source_desired_ref,
                    source_observed_ref,
                    target_desired_ref,
                    target_observed_ref,
                    *(lease_ref for lease_ref in (source_lease_ref, target_lease_ref) if lease_ref is not None),
                )
                if candidate_ref_conflicts(
                    candidate_ref,
                    *promotion_conflicting_refs,
                ):
                    raise OperationError("promotion candidate ref conflicts with deployment state")
                change_revision, outcome = publish_change_candidate(
                    candidate,
                    candidate_ref,
                    target_desired_ref,
                    target_revision,
                    commit_message,
                    title,
                    body,
                    current_target,
                    lease_ref=target_lease_ref,
                    source_pins=source_pins,
                    conflicting_refs=promotion_conflicting_refs,
                )
                published = True
                log_status(
                    "CANDIDATE",
                    f"{style_branch(candidate_ref)} at {describe_revision(change_revision)} targets "
                    f"{style_branch(target_desired_ref)}",
                )
            else:
                if args.candidate_ref:
                    raise OperationError("--candidate-ref requires changeGate pullRequest")
                candidate_ref = ""
                if source_pins:
                    change_revision = publish_tree(
                        target_desired_ref,
                        candidate,
                        target_revision,
                        commit_message,
                        source_pins,
                        expected_publication_head=target_revision,
                    )
                else:
                    change_revision = publish_tree(
                        target_desired_ref,
                        candidate,
                        target_revision,
                        commit_message,
                        expected_publication_head=target_revision,
                    )
                published = True
                log_status(
                    "UPDATE",
                    f"{style_branch(target_desired_ref)} advanced to {describe_revision(change_revision)}",
                )
        except BaseException as publication_error:
            if not published:
                try:
                    publication_ref = candidate_ref if gate == "pullRequest" else target_desired_ref
                    verified = _verify_published_stack_template_change(
                        publication_ref, candidate, target_revision, source_pins
                    )
                except BaseException:
                    log_status("KEEP", "retained StackTemplate source claims after ambiguous publication inspection")
                    raise publication_error from None
                if verified is not None:
                    if gate == "pullRequest":
                        if target_revision is None:
                            raise OperationError("verified pull-request publication has no target revision") from None
                        accepted_target = None
                    else:
                        accepted_target = (
                            AcceptedDesiredTarget(target_desired_ref, target_revision)
                            if target_revision is not None
                            else None
                        )
                    try:
                        _promote_stack_template_source_pins(
                            args.to_environment,
                            candidate,
                            pin_acquisition,
                            accepted_target,
                        )
                    except BaseException:
                        log_status("KEEP", "retained StackTemplate source claims after ambiguous publication")
                else:
                    _release_new_stack_template_source_pins(pin_acquisition)
            raise
        if change_revision is None:
            raise OperationError("publication returned no revision")
        if gate == "pullRequest":
            if target_revision is None:
                raise OperationError("pull-request publication has no target revision")
            accepted_revision = target_revision
        else:
            accepted_revision = change_revision
        _promote_stack_template_source_pins(
            args.to_environment,
            candidate,
            pin_acquisition,
            None if gate == "pullRequest" else AcceptedDesiredTarget(target_desired_ref, accepted_revision),
        )
        print(change_revision)
        write_change_outputs(
            change_revision,
            target_desired_ref,
            candidate_ref,
            outcome,
        )
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a") as stream:
                stream.write(f"candidate_revision={change_revision if candidate_ref else ''}\n")
                stream.write(f"source_desired_revision={source_desired_revision}\n")
        artifact_uris = sorted(
            {
                value
                for path in (source_desired / "units").glob("*")
                if path.suffix in {".json", ".yaml", ".yml"}
                for value in nested_strings(load_json(path))
                if re.search(r"@sha256:[0-9a-f]{64}$", value)
            }
        )
        for uri in artifact_uris:
            log_status("ARTIFACT", uri)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as stream:
                stream.write(
                    f"## Promote {args.from_environment} to {args.to_environment}\n\n"
                    f"- Source desired: `{source_desired_revision}`\n"
                    f"- Source observed: `{source_observed_revision}`\n"
                    f"- Specification: `{specification_revision}`\n"
                    + (
                        f"- Candidate: `{candidate_ref}` (`{change_revision}`)\n"
                        if candidate_ref
                        else f"- Published: `{target_desired_ref}` (`{change_revision}`)\n"
                    )
                )
                if artifact_uris:
                    stream.write("\nArtifacts:\n")
                    for uri in artifact_uris:
                        stream.write(f"- `{uri}`\n")


def publish_desired_change(
    environment: str,
    candidate: Path,
    target_ref: str,
    target_revision: str | None,
    candidate_ref: str,
    commit_message: str,
    title: str,
    body: str,
    dry: bool,
    current_root: Path | None = None,
    allow_removed_units: frozenset[str] = frozenset(),
    request_change: bool = True,
    finalized_resources: frozenset[ResourceFinalizationFence] = frozenset(),
    configuration_root: Path | None = None,
    accepted_continuation: bool = False,
    conflicting_refs: Sequence[str] = (),
) -> tuple[str, ChangeRequestResult | ManualChangeRequest | None]:
    target_ref = canonical_publication_ref(target_ref)
    candidate_ref = canonical_publication_ref(candidate_ref)
    configuration_root = configuration_root or REPOSITORY_ROOT
    lease_ref = effect_lease_ref(environment, target_ref, configuration_root)
    complete_conflicting_refs = (*conflicting_refs, lease_ref) if lease_ref is not None else conflicting_refs
    if candidate_ref == target_ref:
        raise OperationError("change candidate ref conflicts with target desired state")
    if candidate_ref_conflicts(candidate_ref, *complete_conflicting_refs):
        raise OperationError("change candidate ref conflicts with deployment state")
    load_desired_resource_graph(candidate)
    if current_root is not None:
        validate_desired_resource_transition(current_root, candidate, finalized_resources)
    validate_effect_leases_preserved(
        target_ref,
        target_revision,
        candidate,
        current_root,
        allow_removed_units,
        lease_ref=lease_ref,
    )
    gate = change_gate(configuration_root, environment)
    if dry:
        log_status("DRY", f"{style_branch(target_ref)} would receive {title.lower()}")
        return target_revision or "", None
    if gate == "pullRequest" and target_revision is None:
        raise OperationError("pull-request publication requires an initialized desired ref")
    if gate == "pullRequest":
        assert target_revision is not None
    pin_acquisition = _acquire_stack_template_source_pins(environment, candidate)
    source_pins = dict(_required_stack_template_source_pins(environment, candidate))
    published = False
    try:
        if gate == "pullRequest" and not accepted_continuation:
            revision, outcome = publish_change_candidate(
                candidate,
                candidate_ref,
                target_ref,
                target_revision,
                commit_message,
                title,
                body,
                current_root,
                allow_removed_units,
                request_change,
                lease_ref,
                source_pins,
                complete_conflicting_refs,
            )
            published = True
            log_status(
                "CANDIDATE",
                f"{style_branch(candidate_ref)} at {describe_revision(revision)} targets {style_branch(target_ref)}",
            )
            if target_revision is None:
                raise OperationError("pull-request publication has no target revision")
            _promote_stack_template_source_pins(
                environment,
                candidate,
                pin_acquisition,
                None,
            )
            return revision, outcome
        if source_pins:
            revision = publish_tree(
                target_ref,
                candidate,
                target_revision,
                commit_message,
                source_pins,
                expected_publication_head=target_revision,
            )
        else:
            revision = publish_tree(
                target_ref,
                candidate,
                target_revision,
                commit_message,
                expected_publication_head=target_revision,
            )
        published = True
        log_status("UPDATE", f"{style_branch(target_ref)} advanced to {describe_revision(revision)}")
        _promote_stack_template_source_pins(
            environment,
            candidate,
            pin_acquisition,
            AcceptedDesiredTarget(target_ref, revision),
        )
        return revision, None
    except BaseException as publication_error:
        if not published:
            try:
                publication_ref = candidate_ref if gate == "pullRequest" and not accepted_continuation else target_ref
                verified = _verify_published_stack_template_change(
                    publication_ref, candidate, target_revision, source_pins
                )
            except BaseException:
                log_status("KEEP", "retained StackTemplate source claims after ambiguous publication inspection")
                raise publication_error from None
            if verified is not None:
                # The publication is durable even though a later local step
                # failed. Keep ownership, and make the canonical pin durable
                # when possible; never release a claim for this state.
                try:
                    if gate == "pullRequest" and target_revision is None:
                        raise OperationError("verified pull-request publication has no target revision")
                    accepted_target = (
                        None
                        if gate == "pullRequest"
                        else AcceptedDesiredTarget(target_ref, target_revision)
                        if target_revision is not None
                        else None
                    )
                    _promote_stack_template_source_pins(
                        environment,
                        candidate,
                        pin_acquisition,
                        accepted_target,
                    )
                except BaseException:
                    log_status("KEEP", "retained StackTemplate source claims after ambiguous publication")
            else:
                _release_new_stack_template_source_pins(pin_acquisition)
        raise


@dataclass(frozen=True)
class RollbackDesiredInventory:
    """Validated, self-contained Unit inventory from one desired snapshot."""

    units: dict[str, UnitResource[Any]]
    dependencies: dict[str, tuple[str, ...]]


def validate_rollback_desired_inventory(
    desired_revision: str,
    desired: Path,
    description: str,
) -> RollbackDesiredInventory:
    resources = load_desired_resource_graph(desired)
    units: dict[str, UnitResource[Any]] = {}
    for resource in resources.values():
        if not isinstance(resource, UnitResource):
            continue
        if resource_deletion(resource) is not None:
            raise OperationError(f"{description} unit {resource.name!r} is deleting")
        if unit_contains_reference(resource):
            raise OperationError(f"{description} unit {resource.name!r} contains unresolved inputs")
        require_unit(resource, resource.name)
        validate_unit_materialization(desired, resource.name, resource)
        units[resource.name] = resource
    stack_dependencies = stack_dependency_edges(resources)
    dependencies = {
        name: tuple(sorted(desired_observation_reference_units(unit) | set(stack_dependencies.get(name, ()))))
        for name, unit in units.items()
    }
    for name, required in dependencies.items():
        missing = sorted(set(required) - units.keys())
        if missing:
            raise OperationError(
                f"{description} {describe_revision(desired_revision)} unit {name!r} "
                f"depends on missing Unit(s): {', '.join(missing)}"
            )
    return RollbackDesiredInventory(units, dependencies)


def _downstream_desired_unit_closure(
    inventory: RollbackDesiredInventory,
    selected: Sequence[str],
) -> tuple[str, ...]:
    consumers = {name: set() for name in inventory.units}
    for consumer, dependencies in inventory.dependencies.items():
        for producer in dependencies:
            consumers[producer].add(consumer)
    closure: set[str] = set()
    pending = list(selected)
    while pending:
        for consumer in consumers[pending.pop()]:
            if consumer not in closure and consumer not in selected:
                closure.add(consumer)
                pending.append(consumer)
    return tuple(sorted(closure))


def canonicalize_rollback_unit(
    candidate_path: Path,
    current_path: Path,
    qualified_name: str,
    finalized_incarnations: Sequence[ResourceIncarnationTombstone] = (),
) -> None:
    """Keep historical payload while carrying forward the current incarnation identity."""

    historical = load_desired_unit(candidate_path, qualified_name)
    current = load_desired_unit(current_path, qualified_name) if current_path.is_file() else None
    if current is not None:
        metadata = current.metadata
    elif finalized_incarnations:
        historical_source = getattr(historical.spec, "source", None)
        if not isinstance(historical_source, DesiredSource):
            historical_source = None
        metadata = root_metadata_for_resource(
            historical,
            source=historical_source,
            source_revision=historical_source.revision if historical_source is not None else None,
            finalized_uids=tuple(tombstone.uid for tombstone in finalized_incarnations),
        )
        if historical.metadata.is_root:
            metadata = metadata.with_partition(historical.metadata.partition)
    else:
        historical.metadata.validate_desired()
        metadata = historical.metadata
    selected = DocumentFormat.YAML if candidate_path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    write_document(
        candidate_path,
        serialize_unit_document(historical.with_metadata(metadata), profile="desired"),
        format=selected,
    )


def copy_current_blocked_unit(current: Path, candidate: Path, unit_name: str) -> None:
    """Carry a parseable blocked current unit and its materialization into rollback."""

    current_path = unit_document_path(current, unit_name)
    current_unit = load_desired_unit(current_path, unit_name)
    current_unit.metadata.validate_desired()
    for unit_path in document_candidates(
        candidate / "units" / PurePosixPath(unit_name).parent,
        PurePosixPath(unit_name).name,
    ):
        unit_path.unlink()
    selected = DocumentFormat.YAML if current_path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    write_document(
        unit_document_path(candidate, unit_name),
        serialize_unit_document(current_unit, profile="desired"),
        format=selected,
    )
    copy_unit_materialization(current, candidate, unit_name, current_unit)


def merge_current_cleanup_state(
    current: Path,
    candidate: Path,
    *,
    preserve_target_stack_semantics: bool = False,
) -> None:
    """Carry genuine cleanup state through a rollback.

    Full Stack aggregate rollback keeps the target's parseable Stack-owned
    payload and transition blocks authoritative.  Targeted rollback retains
    the historical broader overlay behavior for unrelated cleanup.
    """

    copy_resource_incarnation_tombstones(current, candidate)
    current_blocks = load_desired_transition_blocks(current)
    current_roots = load_desired_cleanup_roots(current)
    cleanup_directory = candidate / DESIRED_CLEANUP_UNITS_PATH
    if cleanup_directory.is_dir():
        for cleanup_path in cleanup_directory.iterdir():
            if cleanup_path.is_file() and cleanup_path.suffix in {".json", ".yaml", ".yml"}:
                cleanup_path.unlink()
    for name, root in current_roots.items():
        for unit_path in document_candidates(candidate / "units", name):
            unit_path.unlink()
        write_opaque_cleanup_root(candidate, name, root)
    resources = load_desired_resource_graph(current, validate=False)
    for key, resource in resources.items():
        if resource_deletion(resource) is None:
            continue
        source_path = _desired_resource_path(current, resource)
        target_path = _desired_resource_path(candidate, resource)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        if isinstance(resource, UnitResource):
            copy_unit_materialization(current, candidate, key[2], resource)
    retained_blocks = dict(current_blocks)
    for name in current_blocks:
        if name in current_roots:
            continue
        current_path = unit_document_path(current, name)
        if current_path.is_file():
            if preserve_target_stack_semantics:
                try:
                    current_unit = load_desired_unit(current_path, name)
                except Exception:
                    current_unit = None
                owner = resource_owner_reference(current_unit) if current_unit is not None else None
                if owner is not None and owner.kind == "Stack":
                    retained_blocks.pop(name, None)
                    continue
            copy_current_blocked_unit(current, candidate, name)
    write_desired_transition_blocks(candidate, retained_blocks)


def active_teardown_dependents(
    root: Path,
    target: UnitResource[Any],
    qualified_name: str,
) -> tuple[str, ...]:
    """Find active owned or observation-dependent descendants of a teardown target."""

    resources = load_desired_resource_graph(root, validate=False)
    explicit_dependencies = stack_dependency_edges(resources, include_missing=True)
    opaque_roots = load_desired_cleanup_roots(root)
    target_keys = tuple(
        key
        for key, resource in resources.items()
        if key[2] == qualified_name and resource.gvk == target.gvk and resource.metadata.uid == target.metadata.uid
    )
    if len(target_keys) != 1:
        raise OperationError(f"desired Unit {target.name!r} has no unique storage identity")
    pending = [target_keys[0]]
    dependents: set[str] = set()
    for opaque_name in opaque_roots:
        if opaque_name != target_keys[0][2]:
            dependents.add(f"{opaque_name} (opaque cleanup root lacks a validated deletion identity)")
    while pending:
        parent_key = pending.pop()
        parent = resources[parent_key]
        parent_identity = (parent.gvk.api_version, parent.gvk.kind, parent.name, parent.metadata.uid or "")
        for child_key, child in resources.items():
            if child_key == parent_key or child_key[2] in dependents:
                continue
            owner = resource_owner_reference(child)
            owner_matches = (
                owner is not None and (owner.apiVersion, owner.kind, owner.name, owner.uid) == parent_identity
            )
            dependency_matches = isinstance(child, UnitResource) and bool(
                (desired_observation_reference_units(child) | set(explicit_dependencies.get(child_key[2], ())))
                & {parent_key[2]}
            )
            if owner_matches or dependency_matches:
                dependents.add(child_key[2])
                pending.append(child_key)
    return tuple(sorted(dependents))


def validate_full_rollback_stack_aggregate(current: Path, target: Path) -> None:
    """Reject rollback targets that would mix historical and current Stack identities."""

    current_resources = load_desired_resource_graph(current)
    target_resources = load_desired_resource_graph(target)
    root_kinds = {"Stack", "StackTemplate"}
    current_roots = {
        key: resource
        for key, resource in current_resources.items()
        if isinstance(resource, StackResource) and resource.gvk.kind in root_kinds
    }
    target_roots = {
        key: resource
        for key, resource in target_resources.items()
        if isinstance(resource, StackResource) and resource.gvk.kind in root_kinds
    }
    current_tombstones = load_resource_incarnation_evidence(current)
    for key, target_root in target_roots.items():
        has_matching_tombstone = any(
            (tombstone.api_version, tombstone.kind, tombstone.name) == key and tombstone.uid == target_root.metadata.uid
            for tombstone in current_tombstones
        )
        if key not in current_roots and has_matching_tombstone:
            raise OperationError(
                f"full rollback would resurrect finalized {target_root.gvk.kind} {target_root.name!r} "
                "without a new aggregate incarnation"
            )
    if set(current_roots) != set(target_roots):
        raise OperationError(
            "full rollback across Stack/StackTemplate aggregate shape is not supported; "
            "the target must contain the same root identities"
        )

    for key, target_root in target_roots.items():
        current_root = current_roots[key]
        if resource_deletion(current_root) is not None:
            raise OperationError(
                f"full rollback is blocked while {target_root.gvk.kind} {target_root.name!r} is deleting"
            )
        if current_root.metadata.uid != target_root.metadata.uid:
            raise OperationError(
                f"full rollback would cross the current {target_root.gvk.kind} {target_root.name!r} incarnation"
            )
        has_matching_tombstone = any(
            (tombstone.api_version, tombstone.kind, tombstone.name) == key and tombstone.uid == target_root.metadata.uid
            for tombstone in current_tombstones
        )
        if has_matching_tombstone:
            raise OperationError(
                f"full rollback would resurrect finalized {target_root.gvk.kind} {target_root.name!r} "
                "without a new aggregate incarnation"
            )

    # A full aggregate rollback may change Unit specifications, but it must
    # not splice historical owned identities into a current Stack projection.
    for stack_key, current_stack in current_roots.items():
        if not isinstance(current_stack, StackResource) or current_stack.gvk.kind != "Stack":
            continue
        target_stack = target_roots[stack_key]
        current_owned = {
            key: resource
            for key, resource in current_resources.items()
            if isinstance(resource, UnitResource)
            and _unit_owned_by_stack(resource, current_stack.name, current_stack.metadata.uid)
        }
        target_owned = {
            key: resource
            for key, resource in target_resources.items()
            if isinstance(resource, UnitResource)
            and _unit_owned_by_stack(resource, target_stack.name, target_stack.metadata.uid)
        }
        if set(current_owned) != set(target_owned):
            raise OperationError(
                f"full rollback of Stack {current_stack.name!r} would change its owned Unit aggregate; "
                "cross-incarnation aggregate rollback is not supported"
            )
        for key in current_owned:
            if current_owned[key].metadata.uid != target_owned[key].metadata.uid:
                raise OperationError(
                    f"full rollback of Stack {current_stack.name!r} would publish a historical owned Unit "
                    f"{current_owned[key].name!r} under the current projection"
                )


def command_rollback(args: argparse.Namespace) -> None:
    reason = args.reason.strip()
    if not reason:
        raise OperationError("--reason must not be blank")
    desired_ref, observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        args.desired_ref,
        args.observed_ref,
    )
    if args.candidate_ref and change_gate(REPOSITORY_ROOT, args.environment) != "pullRequest":
        raise OperationError("--candidate-ref requires changeGate pullRequest")
    log_heading(f"Roll back {style_environment(args.environment)}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        current = temporary / "current"
        target = temporary / "target"
        current_revision = observed_tree(desired_ref, current)
        if current_revision is None:
            raise OperationError(f"{desired_ref} does not exist")
        if args.unit:
            args.unit = list(resolve_unit_selectors(load_desired_resource_graph(current), tuple(args.unit)))
        lease_ref = effect_lease_ref(args.environment, desired_ref)
        lease_root = current
        if lease_ref != desired_ref:
            lease_root, _lease_revision = _effect_lease_store_root(
                desired_ref,
                current_revision,
                current,
                lease_ref,
                temporary / "leases",
            )
        active_leases = [
            lease for lease in load_desired_effect_leases(lease_root).values() if effect_lease_active(lease)
        ]
        if active_leases:
            raise OperationError(
                "rollback is blocked by active desired-state effect lease(s): "
                + ", ".join(f"{lease.qualified_name or lease.unit_name} by {lease.owner}" for lease in active_leases)
            )
        target_revision = resolve_ref(desired_ref, args.to_desired_revision)
        mode = "units" if args.unit else "full"
        if target_revision == current_revision:
            raise OperationError("rollback target is already the current desired revision")
        ancestry = git(
            "merge-base",
            "--is-ancestor",
            target_revision,
            current_revision,
            check=False,
        )
        if ancestry.returncode != 0:
            raise OperationError("rollback target is not ancestral to the current desired head")
        materialize_revision(target_revision, target)

        if mode == "full":
            validate_full_rollback_stack_aggregate(current, target)

        target_inventory = validate_rollback_desired_inventory(target_revision, target, "rollback target")
        current_inventory = (
            validate_rollback_desired_inventory(current_revision, current, "current desired state")
            if mode == "units"
            else None
        )
        target_units = sorted(target_inventory.units)
        requested_units = sorted(set(args.unit or target_units))
        if current_inventory is not None:
            stack_owned = sorted(
                unit_name
                for unit_name in requested_units
                if unit_name in current_inventory.units and _unit_is_stack_owned(current_inventory.units[unit_name])
            )
            if stack_owned:
                raise OperationError(
                    "targeted rollback of Stack-owned Unit(s) is not supported: " + ", ".join(stack_owned)
                )
        unknown = (
            sorted((set(requested_units) - set(current_inventory.units)) | (set(requested_units) - set(target_units)))
            if current_inventory is not None
            else []
        )
        if unknown:
            raise OperationError("unknown rollback unit(s): " + ", ".join(unknown))
        downstream = (
            _downstream_desired_unit_closure(current_inventory, requested_units)
            if current_inventory is not None
            else []
        )
        materialized_units = sorted(set(requested_units) | set(downstream)) if mode == "units" else target_units
        missing_from_target = sorted(set(materialized_units) - set(target_units))
        if missing_from_target:
            raise OperationError("rollback target is missing downstream unit(s): " + ", ".join(missing_from_target))
        if current_inventory is not None:
            for unit_name in materialized_units:
                current_driver = current_inventory.units[unit_name].driver_name
                target_driver = target_inventory.units[unit_name].driver_name
                if current_driver != target_driver:
                    raise OperationError(
                        f"rollback unit {unit_name} changes driver from {current_driver} to {target_driver}"
                    )

        observed_revision = find_clean_observed_snapshot(
            observed_ref,
            target,
            target_units,
            temporary / "observed-history",
        )
        provenance = {
            "environment": args.environment,
            "reason": reason,
            "fromDesiredRevision": current_revision,
            "targetDesiredRevision": target_revision,
            "targetObservedRevision": observed_revision,
            "requestedUnits": "all" if mode == "full" else requested_units,
            "materializedUnits": materialized_units,
        }
        candidate = temporary / "candidate"
        base = target if mode == "full" else current
        shutil.copytree(base, candidate)
        if mode == "units":
            for unit_name in materialized_units:
                historical_path = unit_document_path(target, unit_name)
                historical_unit = load_desired_unit(historical_path, unit_name)
                validate_unit_materialization(target, unit_name, historical_unit)
                candidate_path = unit_document_path(candidate, unit_name)
                if candidate_path.exists():
                    candidate_path.unlink()
                shutil.copy2(
                    historical_path,
                    candidate_path,
                )
                copy_unit_materialization(target, candidate, unit_name, historical_unit)
        merge_current_cleanup_state(
            current,
            candidate,
            preserve_target_stack_semantics=mode == "full",
        )
        current_cleanup_names = set(load_desired_cleanup_roots(current))
        finalized_incarnations = load_resource_incarnation_evidence(candidate)
        for qualified_name, candidate_path in _current_desired_unit_paths(candidate).items():
            candidate_unit = load_desired_unit(candidate_path, qualified_name)
            canonicalize_rollback_unit(
                candidate_path,
                unit_document_path(current, qualified_name),
                qualified_name,
                tuple(
                    tombstone
                    for tombstone in finalized_incarnations
                    if (
                        tombstone.api_version,
                        tombstone.kind,
                        tombstone.name,
                    )
                    == (
                        candidate_unit.gvk.api_version,
                        candidate_unit.gvk.kind,
                        qualified_name,
                    )
                ),
            )
        for unit_name in materialized_units:
            if unit_name in current_cleanup_names:
                continue
            validate_unit_materialization(
                candidate,
                unit_name,
                load_desired_unit(unit_document_path(candidate, unit_name), unit_name),
            )
        requested_label = "all" if mode == "full" else ", ".join(requested_units)
        materialized_label = ", ".join(materialized_units)
        commit_message = (
            f"Rollback {args.environment} to {target_revision}\n\n"
            f"From-Desired-Revision: {current_revision}\n"
            f"Target-Desired-Revision: {target_revision}\n"
            f"Target-Observed-Revision: {observed_revision}\n"
            f"Requested-Units: {requested_label}\n"
            f"Materialized-Units: {materialized_label}\n"
            f"Reason: {reason}"
        )
        candidate_id = candidate_identifier(
            "rollback",
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            provenance,
        )
        candidate_ref = resolve_candidate_ref(
            REPOSITORY_ROOT,
            args.environment,
            "rollback",
            candidate_id,
            args.candidate_ref,
        )
        if candidate_ref_conflicts(candidate_ref, desired_ref, observed_ref):
            raise OperationError("rollback candidate ref conflicts with deployment state")
        title = f"Roll back {args.environment} to {target_revision[:12]}"
        body = (
            f"Forward rollback to desired revision `{target_revision}`.\n\n"
            f"Reason: {reason}\n\n"
            f"Requested units: {requested_label}.\n\n"
            f"Materialized units: {materialized_label}."
        )
        revision, outcome = publish_desired_change(
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            candidate_ref,
            commit_message,
            title,
            body,
            args.dry,
            current,
            conflicting_refs=(observed_ref,),
        )
        if args.dry:
            print(json.dumps(provenance, indent=2, sort_keys=True))
            return
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)


def _release_finalized_unit_lease(
    name: str,
    uid: str,
    deletion_generation: int,
    desired_ref: str,
    current_revision: str,
    desired_root: Path,
) -> bool:
    """Release a separate-store lease after desired finalization succeeded."""

    tombstones = load_resource_incarnation_evidence(desired_root)
    matches = [
        tombstone
        for tombstone in tombstones
        if tombstone.name == name
        and tombstone.uid == uid
        and tombstone.deletion_generation == deletion_generation
        and f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
    ]
    if len(matches) != 1:
        return False
    lease_ref = matches[0].effect_lease_ref
    if lease_ref is None:
        return False
    with tempfile.TemporaryDirectory() as temporary_directory:
        lease_root, _lease_revision = _effect_lease_store_root(
            desired_ref,
            current_revision,
            desired_root,
            lease_ref,
            Path(temporary_directory) / "leases",
        )
        lease = load_desired_effect_leases(lease_root).get(name)
    if lease is None:
        return False
    if lease.uid != uid:
        return False
    release_effect_lease(
        desired_ref,
        name,
        lease.token,
        uid,
        verify_snapshot=False,
        lease_ref=lease_ref,
    )
    return True


def _resource_matches_category(resource: UnitResource[Any] | StackResource, category: str) -> bool:
    return (category == "Unit" and isinstance(resource, UnitResource)) or (
        category in {"Stack", "StackTemplate"} and resource.gvk.kind == category
    )


def _deletion_parent_keys(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    resource: UnitResource[Any] | StackResource,
) -> frozenset[tuple[str, str, str]]:
    """Return controller parents that may become removable with ``resource``."""

    parents: set[tuple[str, str, str]] = set()
    owner = resource_owner_reference(resource)
    if owner is not None:
        key = (owner.apiVersion, owner.kind, owner.name)
        parent = resources.get(key)
        if isinstance(parent, StackResource) and parent.metadata.uid == owner.uid:
            parents.add(key)
    if (
        isinstance(resource, StackResource)
        and resource.gvk.kind == "Stack"
        and isinstance(resource.spec, DesiredStackSpec)
    ):
        key = (CORE_API_VERSION, "StackTemplate", resource.spec.templateRef.name)
        template = resources.get(key)
        if template is not None and template.metadata.uid == resource.spec.templateRef.uid:
            parents.add(key)
    return frozenset(parents)


def _resource_management_partition(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    resource: UnitResource[Any] | StackResource,
) -> str | None:
    """Resolve the partition of the independently managed root for ``resource``."""

    current = resource
    visited: set[tuple[str, str, str]] = set()
    while True:
        key = (current.gvk.api_version, current.gvk.kind, current.name)
        if key in visited:
            raise OperationError(f"desired resource {resource.name!r} has an ownership cycle")
        visited.add(key)
        owner = resource_owner_reference(current)
        if owner is None:
            return current.metadata.partition
        parent = resources.get((owner.apiVersion, owner.kind, owner.name))
        if parent is None or parent.metadata.uid != owner.uid:
            raise OperationError(f"desired resource {resource.name!r} has a missing or stale owner")
        current = parent


def _remove_finalized_resource(
    candidate: Path,
    resource: UnitResource[Any] | StackResource,
    *,
    effect_lease_ref: str | None = None,
) -> ResourceFinalizationFence:
    """Remove one already-safe resource and persist its exact incarnation fence."""

    deletion = resource_deletion(resource)
    if deletion is None or resource.metadata.uid is None:
        raise OperationError(f"desired resource {resource.name!r} is not fenced for deletion")
    candidate_resources = load_desired_resource_graph(candidate, validate=False)
    partition = _resource_management_partition(candidate_resources, resource)
    qualified_name = (
        qualified_unit_name(candidate_resources, resource) if isinstance(resource, UnitResource) else resource.name
    )
    path = _desired_resource_path(candidate, resource)
    for candidate_path in document_candidates(path.parent, resource.name):
        candidate_path.unlink()
    if isinstance(resource, UnitResource):
        materialization = getattr(resource.spec, "materialization", None)
        if materialization is not None:
            materialized_path = candidate / materialization.path
            if materialized_path.is_dir():
                shutil.rmtree(materialized_path)
        remove_effect_lease(candidate, qualified_name)
    write_resource_incarnation_tombstone(
        candidate,
        ResourceIncarnationTombstone(
            api_version=resource.gvk.api_version,
            kind=resource.gvk.kind,
            name=resource.name,
            uid=resource.metadata.uid,
            deletion_generation=deletion.generation,
            qualified_name=qualified_name,
            partition=partition,
            effect_lease_ref=effect_lease_ref if isinstance(resource, UnitResource) else None,
        ),
    )
    return ResourceFinalizationFence(
        resource.gvk.api_version,
        resource.gvk.kind,
        qualified_name,
        resource.metadata.uid,
        deletion.generation,
    )


def _progress_deletion(args: argparse.Namespace) -> bool:
    """Progress one accepted deleting resource through teardown and cleanup.

    This is deliberately controller-only.  Deletion intent is published by
    ``delete``/``apply``; this primitive is the only path that may execute a
    Unit teardown or remove a retained desired resource.
    """
    category = {
        "unit": "Unit",
        "stack": "Stack",
        "stacktemplate": "StackTemplate",
    }[args.kind.lower()]
    name_pattern = QUALIFIED_RESOURCE_NAME_PATTERN if category == "Unit" else r"[a-z0-9][a-z0-9-]*"
    if not re.fullmatch(name_pattern, args.name):
        raise OperationError(f"invalid resource name: {args.name!r}")
    if not isinstance(args.uid, str) or not args.uid:
        raise OperationError("deletion progression requires --uid")
    if not isinstance(args.deletion_generation, int) or args.deletion_generation < 1:
        raise OperationError("deletion progression requires --deletion-generation >= 1")
    configuration_root = getattr(args, "configuration_root", None) or REPOSITORY_ROOT
    desired_ref, observed_ref = deployment_refs(
        configuration_root,
        args.environment,
        args.desired_ref,
        args.observed_ref,
    )
    # A candidate is not an accepted desired snapshot.  The normal controller
    # path always resolves the environment's live desired ref here; accepting
    # an arbitrary override would allow a reviewed-but-unmerged deletion to
    # trigger an external teardown.
    # Accepted-ref authority comes from the live controller configuration,
    # never from configuration bytes inside the selected desired ref. The
    # latter may be an unmerged candidate and is intentionally untrusted for
    # deciding whether cleanup effects are authorized.
    live_desired_ref, _live_observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, None, None)
    if desired_ref != live_desired_ref:
        raise OperationError("deletion progression requires the live desired ref; unaccepted candidates are inert")
    lease_ref = (
        args.lease_ref
        if hasattr(args, "lease_ref")
        else effect_lease_ref(args.environment, desired_ref, configuration_root)
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        current = temporary / "current"
        current_revision = observed_tree(desired_ref, current)
        if current_revision is None:
            return False
        resources = load_desired_resource_graph(current, validate=False)
        matches = [
            resource
            for key, resource in resources.items()
            if (key[2] == args.name if category == "Unit" else resource.name == args.name)
            and _resource_matches_category(resource, category)
        ]
        if len(matches) != 1:
            tombstone = finalized_incarnation_evidence(
                current, CORE_API_VERSION, category, args.name, args.uid, args.deletion_generation
            )
            if category in {"Stack", "StackTemplate"} and (tombstone is None):
                raise OperationError(
                    f"missing desired {category} {args.name!r} without its matching incarnation tombstone; "
                    "the desired graph is corrupt"
                )
            if category == "StackTemplate" and not args.dry:
                return _release_finalized_stack_template_pins(
                    args.environment,
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    current,
                    AcceptedDesiredTarget(desired_ref, current_revision),
                )
            if category == "Stack" and not args.dry:
                return _release_finalized_stack_workload_pins(
                    args.environment,
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    current,
                    AcceptedDesiredTarget(desired_ref, current_revision),
                )
            if category == "Unit" and not args.dry:
                # Unit tombstones use the concrete driver kind (for example
                # ``Terraform``), not the abstract resource category.
                return _release_finalized_unit_lease(
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    desired_ref,
                    current_revision,
                    current,
                )
            return False
        if category == "StackTemplate" and matches[0].metadata.uid != args.uid:
            tombstone = finalized_incarnation_evidence(
                current, CORE_API_VERSION, category, args.name, args.uid, args.deletion_generation
            )
            if tombstone is not None:
                if not args.dry:
                    return _release_finalized_stack_template_pins(
                        args.environment,
                        args.name,
                        args.uid,
                        args.deletion_generation,
                        current,
                        AcceptedDesiredTarget(desired_ref, current_revision),
                    )
                return False
        if category == "Stack" and matches[0].metadata.uid != args.uid:
            tombstone = finalized_incarnation_evidence(
                current, CORE_API_VERSION, category, args.name, args.uid, args.deletion_generation
            )
            if tombstone is not None and not args.dry:
                return _release_finalized_stack_workload_pins(
                    args.environment,
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    current,
                    AcceptedDesiredTarget(desired_ref, current_revision),
                )
        if category == "Unit" and matches[0].metadata.uid != args.uid and not args.dry:
            return _release_finalized_unit_lease(
                args.name,
                args.uid,
                args.deletion_generation,
                desired_ref,
                current_revision,
                current,
            )
        resource = matches[0]
        deletion = resource_deletion(resource)
        if deletion is None:
            return False
        if resource.metadata.uid != args.uid:
            raise OperationError(f"stale {category} UID fence for {args.name!r}")
        if deletion.generation != args.deletion_generation:
            raise OperationError(f"stale {category} deletion generation fence for {args.name!r}")
        if resource_content_digest(resource) != deletion.resourceDigest:
            raise OperationError(f"{category} {args.name!r} changed after deletion started")

        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack":
            if not isinstance(resource.spec, DesiredStackSpec):
                raise OperationError(f"Stack {resource.name!r} has an invalid desired specification")
            tombstones = load_resource_incarnation_evidence(current)
            active_bindings = (
                {
                    (
                        binding.apiVersion,
                        binding.kind,
                        stack_generated_unit_name(resource.name, binding.name),
                    ): binding.uid
                    for binding in resource.spec.activeProjection.units.values()
                }
                if resource.spec.activeProjection is not None
                else {}
            )
            for logical_name, projected in resource.spec.structuralProjection.units.items():
                child_name = stack_generated_unit_name(resource.name, logical_name)
                child_key = (projected.apiVersion, projected.kind, child_name)
                if child_key in resources:
                    continue
                expected_uid = active_bindings.get(child_key)
                if not any(
                    (tombstone.api_version, tombstone.kind, tombstone.qualified_name) == child_key
                    and (expected_uid is None or tombstone.uid == expected_uid)
                    for tombstone in tombstones
                ):
                    raise OperationError(
                        f"Stack {resource.name!r} child {child_name!r} is missing without a matching incarnation "
                        "tombstone; the desired graph is corrupt"
                    )

        children = []
        for child in resources.values():
            owner = resource_owner_reference(child)
            if (
                owner is not None
                and owner.apiVersion == resource.gvk.api_version
                and owner.kind == resource.gvk.kind
                and owner.name == resource.name
                and owner.uid == resource.metadata.uid
            ):
                children.append(child)
        if children:
            log_status(
                "WAIT",
                "owned resources must be finalized first: " + ", ".join(sorted(child.name for child in children)),
            )
            return False
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate":
            from gitopsctr.registry import RESOURCE_REGISTRY

            referencing_stacks: list[str] = []
            for item in resources.values():
                if not isinstance(item, StackResource) or item.gvk.kind != "Stack":
                    continue
                if not isinstance(item.spec, DesiredStackSpec):
                    continue
                if item.spec.templateRef.name != resource.name:
                    continue
                try:
                    RESOURCE_REGISTRY.graph_relationship("stack-selects-stacktemplate").binding.validate(item, resource)
                except Exception as exc:
                    raise OperationError(
                        f"Stack {item.name!r} has a stale StackTemplate identity fence: {exc}"
                    ) from exc
                referencing_stacks.append(item.name)
            if referencing_stacks:
                log_status("WAIT", "Stacks reference this StackTemplate: " + ", ".join(sorted(referencing_stacks)))
                return False

        unit: UnitResource[Any] | None = resource if isinstance(resource, UnitResource) else None
        observed = temporary / "observed"
        lease_acquisition: EffectLeaseAcquisition | None = None
        teardown_details: Mapping[str, object] = {}
        source = getattr(unit.spec, "source", None) if unit is not None else None
        if unit is not None:
            require_unit(unit, PurePosixPath(args.name).name)
            validate_unit_materialization(current, args.name, unit)
            dependents = active_teardown_dependents(current, unit, args.name)
            if dependents:
                log_status("WAIT", "active owned/dependent Units must be finalized first: " + ", ".join(dependents))
                return False
            if args.dry:
                log_status(
                    "DRY",
                    f"{style_unit(unit.name)}: teardown would run at generation {deletion.generation}",
                )
                return False
            observed_tree(observed_ref, observed)
            receipt_path = receipt_document_path(observed, args.name)
            previous_receipt = load_receipt(receipt_path, unit.name) if receipt_path.is_file() else None
            existing_evidence = load_teardown_evidence(observed, args.name, args.uid, deletion.generation)
            if existing_evidence is not None:
                teardown_details = existing_evidence.details
                # Resume with the exact lease store that fenced the original
                # teardown, even if configuration changed after a crash.
                lease_ref = existing_evidence.effect_lease_ref
            elif not isinstance(unit.driver, TeardownCapability):
                log_status(
                    "WAIT",
                    f"{style_unit(unit.name)}: driver {unit.driver_name} does not support teardown",
                )
                return False
            if source is not None and not isinstance(source, DesiredSource):
                raise OperationError(f"retained source identity for {unit.name!r} is invalid")
            source_root = None
            if source is not None and source.revision is not None:
                source_root = temporary / "source"
                _hydrate_stack_workload_pin_for_unit(current, args.environment, unit)
                materialize_revision(source.revision, source_root)

            def assert_no_dependents(desired_root: Path) -> None:
                latest = load_desired_unit(unit_document_path(desired_root, args.name), unit.name)
                if latest.metadata.uid != args.uid:
                    raise EffectLeaseUnavailable(f"desired Unit {unit.name!r} changed before finalization")
                active = active_teardown_dependents(desired_root, latest, args.name)
                if active:
                    raise EffectLeaseUnavailable("active dependents appeared: " + ", ".join(active))

            if lease_ref is not None:
                lease_acquisition = acquire_effect_lease(
                    desired_ref,
                    current_revision,
                    args.name,
                    args.uid,
                    precondition=assert_no_dependents,
                    resume_existing=existing_evidence is not None,
                    lease_ref=lease_ref,
                )
                if lease_acquisition.revision != current_revision:
                    current_revision = lease_acquisition.revision
                    refresh_materialized_root(current_revision, current)
                elif lease_ref == desired_ref:
                    write_effect_lease(current, lease_acquisition.lease)
            if existing_evidence is None:
                assert isinstance(unit.driver, TeardownCapability)
                # Fence the external effect even when leases are disabled.
                # The desired resource digest was checked above and this
                # revision must still be the accepted live head.
                assert_desired_ref_fence(desired_ref, current_revision, args.name, args.uid)
                try:
                    result = unit.driver.teardown(
                        TeardownContext(
                            environment=args.environment,
                            desired_root=current,
                            desired_revision=current_revision,
                            source_root=source_root,
                            source_revision=source.revision if source is not None else None,
                            source_path=source.path if source is not None else None,
                            unit_name=unit.name,
                            unit=unit.spec,
                            resource_uid=args.uid,
                            deletion_generation=deletion.generation,
                            qualified_name=args.name,
                            previous_receipt=previous_receipt,
                            report=Path(args.report).resolve() if args.report else None,
                            execution=DriverExecution.console(),
                        )
                    )
                except TeardownUnsupported as exc:
                    if lease_acquisition is not None:
                        release_effect_lease(
                            desired_ref,
                            args.name,
                            lease_acquisition.lease.token,
                            args.uid,
                            verify_snapshot=False,
                            lease_ref=lease_ref,
                        )
                    log_status("WAIT", f"{style_unit(unit.name)}: {exc}")
                    return False
                except (DriverError, subprocess.CalledProcessError):
                    # A reported driver failure proves this invocation has
                    # stopped. Release its exact token so the idempotent
                    # teardown can be retried automatically.
                    if lease_acquisition is not None:
                        release_effect_lease(
                            desired_ref,
                            args.name,
                            lease_acquisition.lease.token,
                            args.uid,
                            verify_snapshot=False,
                            lease_ref=lease_ref,
                        )
                    raise
                if result is not None and not isinstance(result, TeardownResult):
                    if lease_acquisition is not None:
                        release_effect_lease(
                            desired_ref,
                            args.name,
                            lease_acquisition.lease.token,
                            args.uid,
                            verify_snapshot=False,
                            lease_ref=lease_ref,
                        )
                    raise DriverError("teardown returned an invalid result")
                teardown_details = result.details if result is not None else {}
            publish_teardown_observation_cas(
                observed_ref,
                args.name,
                args.uid,
                deletion.generation,
                current_revision,
                desired_ref=desired_ref,
                lease_ref=lease_ref,
                lease_token=lease_acquisition.lease.token if lease_acquisition is not None else None,
                lease_snapshot=lease_acquisition.lease.snapshot if lease_acquisition is not None else None,
                details=teardown_details,
            )

        candidate = temporary / "candidate"
        shutil.copytree(current, candidate)
        finalized = [resource]
        finalized_fences = [
            _remove_finalized_resource(
                candidate,
                resource,
                effect_lease_ref=lease_ref,
            )
        ]
        pending_parents = set(_deletion_parent_keys(resources, resource))
        cascade_partition = _resource_management_partition(resources, resource)
        while pending_parents:
            candidate_resources = load_desired_resource_graph(candidate)
            parent_key = min(pending_parents)
            pending_parents.remove(parent_key)
            parent = candidate_resources.get(parent_key)
            if parent is None or resource_deletion(parent) is None:
                continue
            if _resource_management_partition(candidate_resources, parent) != cascade_partition:
                continue
            if _resource_deletion_blockers(candidate_resources, parent):
                continue
            pending_parents.update(_deletion_parent_keys(candidate_resources, parent))
            finalized.append(parent)
            finalized_fences.append(
                _remove_finalized_resource(
                    candidate,
                    parent,
                )
            )
        load_desired_resource_graph(candidate)
        candidate_id = candidate_identifier(
            "deletion-progression",
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            {"kind": category, "name": args.name, "uid": args.uid, "deletionGeneration": deletion.generation},
        )
        candidate_ref = resolve_candidate_ref(
            configuration_root, args.environment, "deletion-progression", candidate_id, args.candidate_ref
        )
        revision, outcome = publish_desired_change(
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            candidate_ref,
            f"Finalize deletion of {category} {args.name}",
            f"Finalize deletion of {category} {args.name}",
            f"Remove the finalized resource {category} {args.name}.",
            args.dry,
            current,
            frozenset(
                fence.name
                for item, fence in zip(finalized, finalized_fences, strict=True)
                if isinstance(item, UnitResource)
            ),
            request_change=False,
            finalized_resources=frozenset(finalized_fences),
            configuration_root=configuration_root,
            accepted_continuation=True,
            conflicting_refs=(observed_ref,),
        )
        if not args.dry and outcome is None:
            for finalized_resource in finalized:
                finalized_deletion = resource_deletion(finalized_resource)
                if (
                    isinstance(finalized_resource, StackResource)
                    and finalized_resource.gvk.kind == "StackTemplate"
                    and finalized_resource.metadata.uid is not None
                    and finalized_deletion is not None
                ):
                    _release_finalized_stack_template_pins(
                        args.environment,
                        finalized_resource.name,
                        finalized_resource.metadata.uid,
                        finalized_deletion.generation,
                        candidate,
                        AcceptedDesiredTarget(desired_ref, revision),
                    )
                if (
                    isinstance(finalized_resource, StackResource)
                    and finalized_resource.gvk.kind == "Stack"
                    and finalized_resource.metadata.uid is not None
                    and finalized_deletion is not None
                ):
                    _release_finalized_stack_workload_pins(
                        args.environment,
                        finalized_resource.name,
                        finalized_resource.metadata.uid,
                        finalized_deletion.generation,
                        candidate,
                        AcceptedDesiredTarget(desired_ref, revision),
                    )
        if not args.dry and lease_acquisition is not None and outcome is None:
            release_effect_lease(
                desired_ref,
                resource.name,
                lease_acquisition.lease.token,
                args.uid,
                verify_snapshot=False,
                lease_ref=lease_ref,
            )
        if args.dry:
            return False
        return outcome is None


def command_recover_effect_lease(args: argparse.Namespace) -> None:
    if not re.fullmatch(QUALIFIED_RESOURCE_NAME_PATTERN, args.unit):
        raise OperationError(f"invalid Unit qualified name: {args.unit!r}")
    if not args.confirm_stopped:
        raise OperationError("lease recovery requires --confirm-stopped")
    desired_ref, _observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        args.desired_ref,
        None,
    )
    selector = args.unit
    with tempfile.TemporaryDirectory(prefix="gitopsctr-lease-address-") as temporary_directory:
        desired = Path(temporary_directory) / "desired"
        revision = observed_tree(desired_ref, desired)
        if revision is None:
            raise OperationError(f"desired ref {desired_ref!r} does not exist")
        addresses = qualified_unit_name_map(load_desired_resource_graph(desired))
        concrete = addresses.get(selector)
        if concrete is None:
            matches = {
                tombstone.name
                for tombstone in load_resource_incarnation_evidence(desired)
                if tombstone.qualified_name == selector and tombstone.uid == args.uid
            }
            if len(matches) != 1:
                raise OperationError(f"no exact Unit address {selector!r} exists for UID {args.uid!r}")
            concrete = next(iter(matches))
    args.unit = concrete
    with unit_effect_lock(args.environment, concrete):
        revision = recover_effect_lease(
            desired_ref,
            concrete,
            args.uid,
            args.token,
            lease_ref=effect_lease_ref(args.environment, desired_ref),
        )
    if revision is not None:
        print(revision)


def command_resolve_desired(args: argparse.Namespace) -> None:
    revision = resolve_ref(args.desired_ref, args.desired_revision)
    print(revision)


def _parse_stack_parameters(raw: str) -> JsonObjectValue:
    try:
        value = json.loads(raw)
        validated = require_json_value(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise OperationError(f"--parameters must be a finite JSON object: {exc}") from exc
    if not isinstance(validated, dict):
        raise OperationError("--parameters must contain a JSON object")
    return JsonObjectValue(validated)


def _parameter_values(value: object) -> list[object]:
    if isinstance(value, dict):
        return [value, *[item for child in value.values() for item in _parameter_values(child)]]
    if isinstance(value, list):
        return [item for child in value for item in _parameter_values(child)]
    return [value]


def command_status(args: argparse.Namespace) -> None:
    if args.environment is None:
        if args.unit or args.desired_ref or args.desired_revision or args.observed_ref or args.verbose:
            raise OperationError("status options other than --environment are only available for one environment")
        inspect_resources(
            REPOSITORY_ROOT,
            argparse.Namespace(
                selector="environments",
                name=None,
                environment=None,
                all_environments=False,
                desired_ref=None,
                desired_revision=None,
                observed_ref=None,
                observed_revision=None,
                output="table",
                artifact=None,
                artifacts=False,
            ),
        )
        return
    desired_ref, observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        args.desired_ref,
        args.observed_ref,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        observed = temporary / "observed"
        if args.desired_revision:
            desired_revision = resolve_ref(desired_ref, args.desired_revision)
            materialize_revision(desired_revision, desired)
        else:
            desired_revision = observed_tree(desired_ref, desired)
        observed_revision = observed_tree(observed_ref, observed)
        log_heading(f"Deployment status for {style_environment(args.environment)}")
        log_status(
            "DESIRED",
            f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}"
            if desired_revision
            else f"{style_branch(desired_ref)} does not exist",
        )
        log_status(
            "OBSERVED",
            f"{style_branch(observed_ref)} at {describe_revision(observed_revision)}"
            if observed_revision
            else f"{style_branch(observed_ref)} has no receipts yet",
        )
        specifications = load_environment_specifications(REPOSITORY_ROOT, args.environment)
        statuses = reconciliation_statuses(
            sorted(set(specifications) | set(desired_unit_names(desired))),
            desired,
            observed,
        )
        desired_resources = load_desired_resource_graph(desired)
        display_names = {
            concrete: qualified for qualified, concrete in qualified_unit_name_map(desired_resources).items()
        }
        for concrete in set(specifications) | set(desired_unit_names(desired)):
            display_names.setdefault(concrete, concrete)
        if args.unit is not None:
            selector = args.unit
            concrete = resolve_qualified_unit_values(
                (selector,), {qualified: concrete for concrete, qualified in display_names.items()}
            )[0]
            status_names = {unit_name for unit_name, _status, _reason in statuses}
            if concrete not in status_names:
                available = ", ".join(sorted(display_names.get(name, name) for name in status_names)) or "none"
                raise OperationError(
                    f"unknown unit {selector!r} for environment {args.environment!r}; available units: {available}"
                )
            statuses = [item for item in statuses if item[0] == concrete]
        display_statuses = [(display_names.get(name, name), status, reason) for name, status, reason in statuses]
        log_reconciliation_status(
            args.environment,
            display_statuses,
            desired_revision,
            desired,
            observed,
            args.verbose,
        )


def command_get(args: argparse.Namespace) -> None:
    inspect_resources(REPOSITORY_ROOT, args)


def command_verify(args: argparse.Namespace) -> None:
    desired_ref, _ = deployment_refs(REPOSITORY_ROOT, args.environment)
    log_heading(f"Verify {style_environment(args.environment)}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        desired_revision = resolve_ref(desired_ref)
        materialize_revision(desired_revision, desired)
        log_status("DESIRED", f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}")

        unit_paths = sorted(path for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"})
        available = {path.stem: path for path in unit_paths}
        ensure_desired_units_materialized(desired)
        desired_resources = load_desired_resource_graph(desired)
        addresses = qualified_unit_name_map(desired_resources)
        display_names = {concrete: qualified for qualified, concrete in addresses.items()}
        requested = tuple(args.unit or sorted(addresses))
        selected = sorted(set(resolve_unit_selectors(desired_resources, requested)))
        if not selected:
            raise OperationError(f"{desired_ref} has no materialized units")

        prepared: list[tuple[str, str, UnitResource[Any], DesiredSource | None]] = []
        for unit_name in selected:
            if raw_unit_contains_reference(load_json(available[unit_name])):
                raise OperationError(f"{unit_name} desired state is not fully materialized")
            unit = load_desired_unit(available[unit_name], unit_name)
            driver_name, source = require_unit(unit, unit_name)
            validate_unit_materialization(desired, unit_name, unit)
            if unit_contains_reference(unit):
                raise OperationError(f"{unit_name} desired state is not fully materialized")
            if driver_name not in VERIFICATION_DRIVERS:
                raise OperationError(f"{unit_name} uses {driver_name}, which does not support verification")
            prepared.append((unit_name, driver_name, unit, source))

        drifted: list[str] = []
        for unit_name, driver_name, unit, source in prepared:
            display_name = display_names[unit_name]
            log_status("VERIFY", f"{style_unit(display_name)} ({driver_name})")
            source_root = temporary / "sources" / unit_name if source is not None else None
            if source is not None:
                assert source.revision is not None
                assert source_root is not None
                _hydrate_stack_workload_pin_for_unit(desired, args.environment, unit)
                materialize_revision(source.revision, source_root)
            result = VERIFICATION_DRIVERS[driver_name].verify(
                VerificationContext(
                    environment=args.environment,
                    desired_root=desired,
                    desired_revision=desired_revision,
                    source_root=source_root,
                    source_revision=source.revision if source is not None else None,
                    source_path=source.path if source is not None else None,
                    unit_name=unit.name,
                    unit=unit.spec,
                    qualified_name=next(
                        qualified for qualified, concrete in addresses.items() if concrete == unit_name
                    ),
                    execution=DriverExecution.console(),
                )
            )
            if result.status is VerificationStatus.CLEAN:
                log_status("CLEAN", style_unit(display_name))
            elif result.status is VerificationStatus.DRIFT:
                drifted.append(unit_name)
                log_status("DRIFT", style_unit(display_name))
            else:
                raise DriverError(f"{driver_name} returned an invalid verification status: {result.status!r}")

    if drifted:
        display_drifted = [display_names[name] for name in drifted]
        log_status("RESULT", f"DRIFT: {style_units(display_drifted)}")
        raise OperationError(f"verification detected drift in: {', '.join(display_drifted)}")
    log_status("RESULT", "CLEAN")


def require_unit(unit: UnitResource[Any], unit_name: str) -> tuple[str, DesiredSource | None]:
    if unit.name != PurePosixPath(unit_name).name:
        raise OperationError(f"invalid desired unit: {unit_name}")
    driver = unit.driver_name
    source = getattr(unit.spec, "source", None)
    if source is not None and not isinstance(source, DesiredSource):
        raise OperationError(f"{unit_name} has an invalid source")
    if source is not None:
        safe_source_path(source.path, f"{unit_name} source path")
        if not re.fullmatch(r"[0-9a-f]{40}", str(source.revision or "")):
            raise OperationError(f"{unit_name} has an invalid source revision")
    recorded_version = source.driverVersion if source is not None else None
    if recorded_version is not None and recorded_version != DRIVER_VERSIONS[driver]:
        raise OperationError(
            f"{unit_name} requires {driver} driver version {recorded_version}; "
            f"controller provides {DRIVER_VERSIONS[driver]}"
        )
    return driver, source


def controller_evidence() -> dict[str, str]:
    evidence = {
        "version": version("gitopsctr"),
        "revision": os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD^{commit}").stdout.strip(),
        "observed_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if run_id := os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        evidence["workflow_url"] = f"{server}/{repository}/actions/runs/{run_id}"
    return evidence


def observed_tree(ref: str, output: Path) -> str | None:
    revision = fetch_ref(ref)
    if revision is not None:
        materialize_revision(revision, output)
    else:
        output.mkdir(parents=True, exist_ok=True)
    return revision


@contextmanager
def unit_effect_lock(environment: str, unit_name: str):
    """Serialize reconcile/deletion effects for one environment and Unit."""

    identity = hashlib.sha256(f"{REPOSITORY_ROOT}\0{environment}\0{unit_name}".encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f"gitopsctr-effect-{identity}.lock"
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def assert_desired_ref_fence(
    desired_ref: str,
    expected_revision: str,
    unit_name: str,
    expected_uid: str,
) -> None:
    actual_revision = fetch_ref(desired_ref)
    if actual_revision != expected_revision:
        raise OperationError(
            f"desired Unit {unit_name!r} changed during effect preparation; "
            f"expected revision {expected_revision}, found {actual_revision}"
        )
    if not expected_uid:
        raise OperationError(f"desired Unit {unit_name!r} has no UID effect fence")


def json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise OperationError(f"JSON pointer must start with '/': {pointer!r}")
    value = document
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise OperationError(f"JSON pointer has an invalid escape: {pointer!r}")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(token)] if isinstance(value, list) else value[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OperationError(f"JSON pointer does not resolve: {pointer!r}") from exc
    return value


def _template(value: object, pointer: str = "") -> TemplateValue:
    try:
        return parse_template_value(value, pointer)
    except TemplateError as exc:
        raise OperationError(str(exc)) from exc


def contains_reference(value: object) -> bool:
    return template_contains_reference(_template(value))


def unit_contains_reference(unit: UnitResource[Any]) -> bool:
    return contains_reference(unit.driver.desired_unit_contract.dump(unit.spec))


def raw_unit_contains_reference(document: object) -> bool:
    """Check an untrusted persisted unit before requiring its final desired contract."""

    return operational.raw_unit_contains_reference(document)


def artifact_references(value: Any, pointer: str = "") -> set[ArtifactReferenceTarget]:
    """Collect validated and explicitly typed artifact references.

    Invalid references include their JSON Pointer location so callers can identify
    the offending field in a larger unit document.
    """
    found: set[ArtifactReferenceTarget] = set()
    for expression in template_references(_template(value, pointer)):
        if isinstance(expression, ArtifactReferenceExpression):
            validate_artifact_reference_target(expression.fromArtifact)
            found.add(expression.fromArtifact)
    return found


def nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for candidate in value for item in nested_strings(candidate)]
    if isinstance(value, dict):
        return [item for candidate in value.values() for item in nested_strings(candidate)]
    return []


def publish_observation_cas(
    observed_ref: str,
    unit_name: str,
    receipt: ReceiptResource[Any],
    unit: UnitResource[Any],
    artifact_documents: Mapping[str, JsonObject],
    desired_revision: str,
    *,
    desired_ref: str | None = None,
    lease_ref: str | None = None,
    expected_uid: str | None = None,
    lease_token: str | None = None,
    lease_snapshot: EffectLeaseSnapshot | None = None,
) -> str:
    for attempt in range(5):
        if attempt:
            log_status("RETRY", f"observation publish attempt {attempt + 1}/5")
        with tempfile.TemporaryDirectory() as temporary_directory:
            observed = Path(temporary_directory) / "observed"
            observed_revision = observed_tree(observed_ref, observed)
            if desired_ref is not None and lease_token is not None:
                desired_revision = validate_effect_lease_head_for_store(
                    desired_ref,
                    unit_name,
                    expected_uid or "",
                    lease_token,
                    lease_snapshot,
                    lease_ref=lease_ref,
                )
                receipt = replace(
                    receipt,
                    spec=replace(
                        receipt.spec,
                        desired=replace(receipt.spec.desired, revision=desired_revision),
                    ),
                )
            driver = receipt.driver_name
            if receipt.name != unit.name:
                raise OperationError(f"candidate receipt name is not local Unit name {unit.name!r}")
            validate_artifact_output_identity(driver, unit, artifact_documents, unit_name)
            receipt_path = receipt_document_path(observed, unit_name)
            existing_receipt = load_receipt(receipt_path, unit_name) if receipt_path.is_file() else None
            if existing_receipt is not None:
                if existing_receipt.spec.desired.unitBlob == receipt.spec.desired.unitBlob:
                    validate_receipt_artifacts(observed, unit, existing_receipt)
            descriptors = write_artifact_documents(observed, unit_name, driver, artifact_documents)
            try:
                typed_descriptors = {
                    name: ArtifactDescriptor.from_dict(descriptor) for name, descriptor in descriptors.items()
                }
            except (TypeError, ValueError) as exc:
                raise OperationError(f"candidate receipt has invalid artifact descriptors: {exc}") from exc
            candidate_receipt = ReceiptResource(
                gvk=receipt.gvk,
                metadata=receipt.metadata,
                driver=receipt.driver,
                spec=receipt.spec,
                status=replace(receipt.status, artifacts=typed_descriptors or None),
            )
            if desired_ref is not None and lease_token is not None:
                latest_revision = validate_effect_lease_head_for_store(
                    desired_ref,
                    unit_name,
                    expected_uid or "",
                    lease_token,
                    lease_snapshot,
                    lease_ref=lease_ref,
                )
                if latest_revision != desired_revision:
                    desired_revision = latest_revision
                    candidate_receipt = replace(
                        candidate_receipt,
                        spec=replace(
                            candidate_receipt.spec,
                            desired=replace(candidate_receipt.spec.desired, revision=desired_revision),
                        ),
                    )
            validate_receipt_document(
                RESOURCE_CATALOG.serialize_receipt(candidate_receipt),
                f"candidate receipt for {unit_name}",
            )
            if (
                existing_receipt is not None
                and existing_receipt.spec.desired.unitBlob == candidate_receipt.spec.desired.unitBlob
            ):
                if observed_revision is None:
                    raise OperationError(f"{observed_ref} receipt has no revision")
                if existing_receipt.driver_name != driver:
                    raise OperationError(f"duplicate {unit_name} receipt changed its reconciliation driver")
                existing_result = semantic_reconciliation_result(
                    driver, existing_receipt.status.result, existing_receipt.status.artifacts
                )
                candidate_result = semantic_reconciliation_result(
                    driver, candidate_receipt.status.result, candidate_receipt.status.artifacts
                )
                if existing_result != candidate_result:
                    raise OperationError(
                        f"duplicate {unit_name} receipt for the same desired unit has a different semantic result"
                    )
                return observed_revision
            if desired_ref is not None and lease_token is not None:
                latest_revision = validate_effect_lease_head_for_store(
                    desired_ref,
                    unit_name,
                    expected_uid or "",
                    lease_token,
                    lease_snapshot,
                    lease_ref=lease_ref,
                )
                if latest_revision != desired_revision:
                    desired_revision = latest_revision
                    candidate_receipt = replace(
                        candidate_receipt,
                        spec=replace(
                            candidate_receipt.spec,
                            desired=replace(candidate_receipt.spec.desired, revision=desired_revision),
                        ),
                    )
                    validate_receipt_document(
                        RESOURCE_CATALOG.serialize_receipt(candidate_receipt),
                        f"candidate receipt for {unit_name}",
                    )
            elif desired_ref is not None:
                assert_desired_ref_fence(desired_ref, desired_revision, unit_name, expected_uid or "")
            write_preferred_document(receipt_path, candidate_receipt, REPOSITORY_ROOT)
            try:
                return publish_tree(
                    observed_ref,
                    observed,
                    observed_revision,
                    f"Observe {unit_name} at {desired_revision}",
                    expected_publication_head=observed_revision,
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not update {observed_ref} after concurrent updates")


def write_reconcile_outputs(changed: bool) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"reconciled={'true' if changed else 'false'}\n")


def reconciliation_artifact_effects(
    observed: Path,
    unit: UnitResource[Any],
    previous_receipt: ReceiptResource[Any] | None,
    artifacts: Mapping[str, JsonObject],
) -> list[tuple[str, str]]:
    """Describe artifact changes using the typed artifact documents, not incidental serialization."""

    previous_names = set(previous_receipt.status.artifacts or {}) if previous_receipt is not None else set()
    effects: list[tuple[str, str]] = []
    for name in sorted(artifacts):
        if name not in previous_names or previous_receipt is None:
            effects.append(("ADDED", f"Artifact {style_unit(name)}"))
            continue
        previous_document, _digest = load_artifact_document(
            observed,
            unit,
            previous_receipt,
            name,
            require_current_producer=False,
        )
        artifact_api = require_artifact_api(unit.driver.artifact_outputs[name])
        current_resource = parse_artifact_document(
            artifact_api,
            artifacts[name],
            f"{unit.driver_name} artifact {name}",
        )
        current_document = artifact_api.dump(current_resource)
        status = "UNCHANGED" if previous_document == current_document else "UPDATED"
        effects.append((status, f"Artifact {style_unit(name)}"))
    return effects


def _materialize_durable_projection_source(
    current_desired: Path,
    environment_name: str,
    destination: Path,
    context_digest: str,
) -> None:
    """Create the minimal authored view needed to re-project persisted Stacks.

    This source tree contains only durable Stack inputs and Project/
    Environment policy from a retained reviewed source revision. Repository-
    backed Unit sources are read from each template's retained immutable
    sourceContext by ``project_stack_resources``; the live worktree is never
    consulted.
    """

    destination.mkdir(parents=True, exist_ok=True)
    resources = load_desired_resource_graph(current_desired, validate=False)
    context = load_projection_context(current_desired, context_digest, environment_name)
    project_file = _safe_context_basename(context["projectFile"], "projectFile")
    environment_file = _safe_context_basename(context["environmentFile"], "environmentFile")
    try:
        project_bytes = base64.b64decode(cast(str, context["projectBytes"]), validate=True)
        environment_bytes = base64.b64decode(cast(str, context["environmentBytes"]), validate=True)
    except (ValueError, TypeError) as exc:
        raise OperationError("durable projection context contains invalid document bytes") from exc
    (destination / project_file).write_bytes(project_bytes)
    project = load_project_config(destination)

    target_environment = project_environment_root(destination, environment_name)
    target_environment.mkdir(parents=True, exist_ok=True)
    (target_environment / environment_file).write_bytes(environment_bytes)

    template_directory = destination.joinpath(*project.stack_templates_path.parts)
    stack_directory = target_environment / "stacks"
    template_directory.mkdir(parents=True, exist_ok=True)
    stack_directory.mkdir(parents=True, exist_ok=True)
    for resource in resources.values():
        if not isinstance(resource, StackResource) or resource_deletion(resource) is not None:
            continue
        if resource.gvk.kind == "StackTemplate":
            if not isinstance(resource.spec, DesiredStackTemplateSpec):
                raise OperationError(f"desired StackTemplate {resource.name!r} has an invalid specification")
            authored = StackResource(
                resource.gvk,
                ResourceMetadata(name=resource.name),
                StackTemplateInlineSpec(
                    parameters=list(resource.spec.parameters),
                    unitTemplates=dict(resource.spec.unitTemplates),
                ),
            )
            write_document(
                template_directory / f"{resource.name}.yaml",
                RESOURCE_CATALOG.serialize_stack_resource(authored, profile="authored"),
                format=DocumentFormat.YAML,
            )
        elif resource.gvk.kind == "Stack":
            if not isinstance(resource.spec, DesiredStackSpec):
                raise OperationError(f"desired Stack {resource.name!r} has an invalid specification")
            authored = StackResource(
                resource.gvk,
                ResourceMetadata(name=resource.name),
                StackSpec(
                    template=resource.spec.templateRef.name,
                    parameters=resource.spec.parameters,
                    units=resource.spec.units,
                    artifactImports=resource.spec.artifactImports,
                ),
            )
            write_document(
                stack_directory / f"{resource.name}.yaml",
                RESOURCE_CATALOG.serialize_stack_resource(authored, profile="authored"),
                format=DocumentFormat.YAML,
            )


@dataclass(frozen=True)
class DurablePublicationPolicy:
    """Publication controls that must be identical for one durable snapshot."""

    change_gate: str
    candidate_ref_template: str
    effect_lease_ref: str | None


def _durable_publication_policy(
    environment_name: str,
    desired_ref: str,
    context_root: Path,
) -> DurablePublicationPolicy:
    return DurablePublicationPolicy(
        change_gate=change_gate(context_root, environment_name),
        candidate_ref_template=candidate_ref_template(context_root, environment_name),
        effect_lease_ref=effect_lease_ref(environment_name, desired_ref, context_root),
    )


def _validate_durable_publication_policies(
    environment_name: str,
    desired_ref: str,
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    context_sources: Mapping[str, Path],
) -> tuple[str, Path]:
    """Return the common publication context, or fail before publication."""

    context_digests: set[str] = set()
    for resource in resources.values():
        if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
            continue
        if not isinstance(resource.spec, DesiredStackSpec):
            raise OperationError(f"desired Stack {resource.name!r} has no desired projection")
        context_digests.add(resource.spec.structuralProjection.identity.projectionContextDigest)
        active = resource.spec.activeProjection
        if active is not None and active.units:
            # Active Units can retain effect leases even while the structural
            # projection is blocked, so their old context is part of the
            # publication policy fence as well.
            context_digests.add(active.projectionContextDigest)
            context_digests.update(binding.projectionContextDigest for binding in active.units.values())
    if not context_digests:
        raise OperationError("durable Stack projection has no bound publication context")

    policies: list[tuple[str, DurablePublicationPolicy]] = []
    for digest in sorted(context_digests):
        context_root = context_sources.get(digest)
        if context_root is None:
            raise OperationError(f"durable Stack projection context {digest!r} was not materialized")
        policies.append((digest, _durable_publication_policy(environment_name, desired_ref, context_root)))
    common_digest, common_policy = policies[0]
    for digest, policy in policies[1:]:
        if policy != common_policy:
            raise OperationError(
                "durable Stack projection contexts have incompatible publication policies: "
                f"{common_digest} has changeGate={common_policy.change_gate!r}, "
                f"candidateRefTemplate={common_policy.candidate_ref_template!r}, "
                f"effectLeaseRef={common_policy.effect_lease_ref!r}; "
                f"{digest} has changeGate={policy.change_gate!r}, "
                f"candidateRefTemplate={policy.candidate_ref_template!r}, "
                f"effectLeaseRef={policy.effect_lease_ref!r}"
            )
    return common_digest, context_sources[common_digest]


def progress_durable_stack_projection(
    environment_name: str,
    desired_ref: str,
    observed_ref: str,
) -> str | None:
    """Re-project persisted Stack inputs after new observation evidence.

    The desired ref is read and published with compare-and-swap semantics on
    every attempt.  A concurrent desired update simply causes the complete
    projection to be rebuilt from the newer snapshot.
    """

    for attempt in range(5):
        current_revision = fetch_ref(desired_ref)
        if current_revision is None:
            return None
        if attempt:
            log_status("RETRY", f"durable Stack projection publish attempt {attempt + 1}/5")
        try:
            with tempfile.TemporaryDirectory(prefix="gitopsctr-durable-projection-") as directory:
                temporary = Path(directory)
                source_root = temporary / "source"
                current = temporary / "current"
                observed = temporary / "observed"
                materialize_revision(current_revision, current)
                if not (
                    _current_desired_stack_paths(current, "Stack")
                    or _current_desired_stack_paths(current, "StackTemplate")
                ):
                    return current_revision
                current_resources = load_desired_resource_graph(current, validate=False)
                # A fresh runner may have the desired snapshot but not the
                # source objects referenced by structural or stale-active
                # Stack Units. Hydrate those exact per-Stack pins before any
                # projection availability/materialization work.
                _hydrate_required_stack_workload_pins(environment_name, current)
                stack_contexts: dict[str, str] = {}
                for resource in current_resources.values():
                    if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
                        continue
                    if not isinstance(resource.spec, DesiredStackSpec):
                        raise OperationError(f"desired Stack {resource.name!r} has no desired projection")
                    digest = resource.spec.structuralProjection.identity.projectionContextDigest
                    load_projection_context(current, digest, environment_name)
                    stack_contexts[resource.name] = digest
                if not stack_contexts:
                    return current_revision
                observed_revision = observed_tree(observed_ref, observed)
                context_groups: dict[str, frozenset[str]] = {}
                context_sources: dict[str, Path] = {}
                baseline = current
                for context_digest in sorted(set(stack_contexts.values())):
                    context_groups[context_digest] = frozenset(
                        name for name, digest in stack_contexts.items() if digest == context_digest
                    )
                    context_source = temporary / f"source-{context_digest.removeprefix('sha256:')}"
                    context_sources[context_digest] = context_source
                    _materialize_durable_projection_source(
                        current,
                        environment_name,
                        context_source,
                        context_digest,
                    )
                    if not source_root.exists() or not any(source_root.iterdir()):
                        source_root = context_source
                    next_candidate = temporary / f"candidate-{context_digest.removeprefix('sha256:')}"
                    candidate_units = next_candidate / "units"
                    candidate_units.mkdir(parents=True, exist_ok=True)
                    # Seed persisted Units so receipt/artifact lookups can
                    # authenticate external producers while this group is
                    # resolved.  The resulting tree is complete before it
                    # becomes the next group's immutable baseline.
                    for unit_name, baseline_path in _current_desired_unit_paths(baseline).items():
                        target_path = unit_document_path(next_candidate, unit_name, context_source)
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(baseline_path, target_path)
                        baseline_unit = load_desired_unit(baseline_path, baseline_path.stem)
                        if getattr(baseline_unit.spec, "materialization", None) is not None:
                            copy_unit_materialization(baseline, next_candidate, unit_name, baseline_unit)
                    build_desired_candidate(
                        environment_name,
                        context_source,
                        None,
                        baseline,
                        observed,
                        observed_revision,
                        next_candidate,
                        verbose=False,
                        projection_stack_names=context_groups[context_digest],
                    )
                    # Projection only rewrites this context group. Carry the
                    # rest of the complete baseline, including owned
                    # materializations, contexts, tombstones, and retained
                    # transition/active state, before advancing.
                    _copy_unrelated_desired_resources(baseline, next_candidate, frozenset(), None)
                    for baseline_path in document_candidates(baseline, "promotion"):
                        target_path = next_candidate / baseline_path.name
                        shutil.copy2(baseline_path, target_path)
                    load_desired_resource_graph(next_candidate)
                    validate_desired_resource_transition(baseline, next_candidate)
                    baseline = next_candidate

                candidate = baseline
                final_resources = load_desired_resource_graph(candidate)
                for resource in final_resources.values():
                    if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
                        continue
                    if not isinstance(resource.spec, DesiredStackSpec):
                        continue
                    active = resource.spec.activeProjection
                    digests = {resource.spec.structuralProjection.identity.projectionContextDigest}
                    if active is not None and active.units:
                        digests.add(active.projectionContextDigest)
                        digests.update(binding.projectionContextDigest for binding in active.units.values())
                    for context_digest in sorted(digests):
                        if context_digest in context_sources:
                            continue
                        context_source = temporary / f"source-{context_digest.removeprefix('sha256:')}"
                        _materialize_durable_projection_source(
                            current,
                            environment_name,
                            context_source,
                            context_digest,
                        )
                        context_sources[context_digest] = context_source
                _common_digest, source_root = _validate_durable_publication_policies(
                    environment_name,
                    desired_ref,
                    final_resources,
                    context_sources,
                )
                for current_path in document_candidates(current, "promotion"):
                    target_path = candidate / current_path.name
                    shutil.copy2(current_path, target_path)
                if directory_files(current) == directory_files(candidate):
                    _ensure_stack_template_source_pins(environment_name, current)
                    _gc_superseded_stack_workload_pins(
                        environment_name,
                        current,
                        AcceptedDesiredTarget(desired_ref, current_revision),
                    )
                    return current_revision
                candidate_id = candidate_identifier(
                    "apply",
                    environment_name,
                    candidate,
                    desired_ref,
                    current_revision,
                    {"durableProjection": True, "observedRevision": observed_revision},
                )
                candidate_ref = resolve_candidate_ref(
                    source_root,
                    environment_name,
                    "apply",
                    candidate_id,
                )
                revision, outcome = publish_desired_change(
                    environment_name,
                    candidate,
                    desired_ref,
                    current_revision,
                    candidate_ref,
                    f"Advance durable Stack projections in {environment_name}",
                    f"Advance durable Stack projections in {environment_name}",
                    "Re-project persisted StackTemplate and Stack inputs after new observation evidence.",
                    False,
                    current,
                    request_change=False,
                    configuration_root=source_root,
                    conflicting_refs=(observed_ref,),
                )
                if outcome is not None:
                    log_status(
                        "CANDIDATE",
                        f"{style_branch(candidate_ref)} at {describe_revision(revision)} targets "
                        f"{style_branch(desired_ref)}",
                    )
                    return current_revision
                return revision
        except subprocess.CalledProcessError as exc:
            if attempt == 4 or not retryable_push_failure(exc):
                raise
    raise OperationError(f"could not advance durable Stack projection on {desired_ref} after concurrent updates")


def _stack_effect_context_digest(stack: StackResource, unit_name: str | None = None) -> str:
    if not isinstance(stack.spec, DesiredStackSpec):
        raise OperationError(f"Stack {stack.name!r} has an invalid desired specification")
    structural = stack.spec.structuralProjection
    active = stack.spec.activeProjection
    if active is not None and unit_name is not None:
        binding = next((item for item in active.units.values() if item.name == unit_name), None)
        if binding is not None:
            return binding.projectionContextDigest
    if active is not None and active.sourceProjectionDigest != structural.identity.projectionDigest:
        return active.projectionContextDigest
    return structural.identity.projectionContextDigest


def _command_reconcile(args: argparse.Namespace) -> bool:
    if not re.fullmatch(QUALIFIED_RESOURCE_NAME_PATTERN, args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    verbose = getattr(args, "verbose", False)
    display_unit = getattr(args, "qualified_unit", args.unit)

    def detail(status: str, message: str) -> None:
        if verbose:
            log_status(status, message)

    def write_output(changed: bool) -> None:
        if not getattr(args, "_defer_reconcile_output", False):
            write_reconcile_outputs(changed)

    explicit_configuration = args.desired_ref is not None and args.observed_ref is not None
    if not explicit_configuration:
        # Default ref discovery is intentionally live; callers that provide
        # both refs are handled from the desired Stack context below.
        load_environment(REPOSITORY_ROOT, args.environment)
    if verbose:
        log_heading(f"Reconcile {style_unit(display_unit)}")
        log_status("START", f"environment {style_environment(args.environment)}")
        log_status("MODE", "plan" if args.plan else "apply")
    else:
        log_heading(f"{style_unit(display_unit)} · {style_environment(args.environment)}")
    report = Path(args.report).resolve() if args.report else None
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        observed = temporary / "observed"
        if explicit_configuration:
            desired_ref, observed_ref = canonical_deployment_refs(
                args.environment,
                cast(str, args.desired_ref),
                cast(str, args.observed_ref),
            )
        else:
            desired_ref, observed_ref = deployment_refs(
                REPOSITORY_ROOT,
                args.environment,
                args.desired_ref,
                args.observed_ref,
            )
        lease_ref: str | None = None
        detail("REFS", f"desired {style_branch(desired_ref)}; observed {style_branch(observed_ref)}")
        observed_revision = observed_tree(observed_ref, observed)
        desired_revision = resolve_ref(desired_ref, args.desired_revision)
        materialize_revision(desired_revision, desired)
        configuration_root = REPOSITORY_ROOT
        if explicit_configuration:
            resources = load_desired_resource_graph(desired, validate=False)
            selected_unit = next(
                (
                    resource
                    for key, resource in resources.items()
                    if isinstance(resource, UnitResource) and key[2] == args.unit
                ),
                None,
            )
            selected_owner = (
                resource_owner_reference(selected_unit) if isinstance(selected_unit, UnitResource) else None
            )
            if selected_owner is not None and selected_owner.kind == "Stack":
                selected_stack = resources.get((CORE_API_VERSION, "Stack", selected_owner.name))
                if isinstance(selected_stack, StackResource) and isinstance(selected_stack.spec, DesiredStackSpec):
                    configuration_root = temporary / "stack-context"
                    _materialize_durable_projection_source(
                        desired,
                        args.environment,
                        configuration_root,
                        _stack_effect_context_digest(selected_stack, PurePosixPath(args.unit).name),
                    )
        if configuration_root != REPOSITORY_ROOT:
            load_environment(configuration_root, args.environment)
        lease_ref = effect_lease_ref(args.environment, desired_ref, configuration_root)
        detail("DESIRED", f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}")
        detail(
            "OBSERVED",
            f"{style_branch(observed_ref)} at {describe_revision(observed_revision)}"
            if observed_revision
            else f"{style_branch(observed_ref)} has no receipts yet",
        )
        deletion_path = unit_document_path(desired, args.unit)
        if deletion_path.is_file():
            deleting_unit = load_desired_unit(deletion_path, args.unit)
            if resource_deletion(deleting_unit) is not None:
                deletion = resource_deletion(deleting_unit)
                assert deletion is not None
                progressed = _progress_deletion(
                    argparse.Namespace(
                        kind="Unit",
                        name=args.unit,
                        environment=args.environment,
                        desired_ref=desired_ref,
                        observed_ref=observed_ref,
                        candidate_ref=None,
                        uid=deleting_unit.metadata.uid,
                        deletion_generation=deletion.generation,
                        report=report,
                        dry=args.plan,
                        configuration_root=configuration_root,
                        lease_ref=lease_ref,
                    )
                )
                if progressed:
                    log_status("APPLY", f"{style_unit(display_unit)} deletion progressed")
                else:
                    log_status("WAIT", deletion_reason(deleting_unit))
                log_status("DONE", f"{style_unit(display_unit)}: {'progressed' if progressed else 'no changes'}")
                write_output(progressed)
                return progressed
        # A desired cleanup commit can succeed while the final lease-release
        # publication is interrupted.  Tombstone-backed retries are safe and
        # must not require the removed Unit document or public UID arguments.
        finalized = [
            tombstone
            for tombstone in load_resource_incarnation_evidence(desired)
            if tombstone.qualified_name == args.unit
            and f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
        ]
        if not args.plan:
            for tombstone in finalized:
                progressed = _retry_finalized_cleanup(
                    tombstone,
                    environment=args.environment,
                    desired_ref=desired_ref,
                    current_revision=desired_revision,
                    desired_root=desired,
                )
                if progressed:
                    log_status("APPLY", f"{style_unit(display_unit)} deletion cleanup progressed")
                    write_output(True)
                    return True
        if transition_reason := load_desired_transition_blocks(desired).get(args.unit):
            log_status("WAIT", transition_reason)
            log_status("DONE", f"{style_unit(display_unit)}: no changes")
            write_output(False)
            return False
        unit_path = unit_document_path(desired, args.unit)
        if not unit_path.is_file():
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(display_unit)}: no changes")
            write_output(False)
            return False
        unit = load_desired_unit(unit_path, args.unit)
        ensure_desired_units_materialized(desired)
        desired_resources = load_desired_resource_graph(desired)
        unit_qualified_name = qualified_unit_name(desired_resources, unit)
        if raw_unit_contains_reference(load_json(unit_path)):
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(display_unit)}: no changes")
            write_output(False)
            return False
        driver_name, source = require_unit(unit, args.unit)
        validate_unit_materialization(desired, args.unit, unit)
        detail("DRIVER", driver_name)
        if source is not None:
            assert source.revision is not None
            detail("SOURCE", f"{describe_revision(source.revision)} ({source.path})")
        else:
            detail("SOURCE", "none (source-less unit)")
        if unit_contains_reference(unit):
            raise OperationError(f"{args.unit} desired state is not fully materialized")
        if not unit_requires_reconciliation(unit):
            log_status("SKIP", "unit is complete after desired-state materialization")
            log_status("DONE", f"{style_unit(display_unit)}: materialized for external delivery")
            write_output(False)
            return False

        unit_blob = file_blob(unit_path)
        receipt_path = receipt_document_path(observed, args.unit)
        previous_receipt = load_receipt(receipt_path, args.unit) if receipt_path.is_file() else None
        receipt_is_current = False
        if receipt_path.is_file():
            assert previous_receipt is not None
            receipt = previous_receipt
            skip_clean_unit = not args.plan or bool(UNIT_DRIVERS[driver_name].artifact_outputs)
            receipt_is_current = receipt.spec.desired.unitBlob == unit_blob
            if receipt_is_current:
                validate_receipt_artifacts(observed, unit, receipt)
            if not getattr(args, "reapply", False) and skip_clean_unit and receipt_is_current:
                detail("KEEP", "observation already matches desired state")
                if verbose:
                    log_status("DONE", f"{style_unit(display_unit)}: clean")
                else:
                    log_reconcile_outcome(
                        "UP TO DATE",
                        "Observation matches desired state",
                        "No reconciliation needed",
                        [],
                    )
                    log_status(
                        "DESIRED",
                        f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}",
                    )
                    log_status(
                        "OBSERVED",
                        f"{style_branch(observed_ref)} at {describe_revision(observed_revision)}",
                    )
                    log_status("DRIVER", driver_name)
                    if source is not None:
                        log_status("SOURCE", f"{describe_revision(source.revision)} ({source.path})")
                    else:
                        log_status("SOURCE", "none (source-less unit)")
                write_output(False)
                return False

        source_root = temporary / "source" if source is not None else None
        if not args.plan:
            assert unit.metadata.uid is not None
            assert_desired_ref_fence(desired_ref, desired_revision, args.unit, unit.metadata.uid)
        if source is not None:
            assert source.revision is not None
            assert source_root is not None
            _hydrate_stack_workload_pin_for_unit(desired, args.environment, unit)
            materialize_revision(source.revision, source_root)
        plugin: ReconciliationCapability | None = None
        if not args.plan:
            try:
                plugin = RECONCILIATION_DRIVERS[driver_name]
            except KeyError as exc:
                raise OperationError(f"{args.unit} uses {driver_name}, which does not support reconciliation") from exc

        lease_acquisition: EffectLeaseAcquisition | None = None
        if not args.plan and lease_ref is not None:
            assert unit.metadata.uid is not None
            try:
                lease_acquisition = acquire_effect_lease(
                    desired_ref,
                    desired_revision,
                    args.unit,
                    unit.metadata.uid,
                    lease_ref=lease_ref,
                )
            except EffectLeaseUnavailable as exc:
                log_status("WAIT", f"{style_unit(display_unit)}: {exc}")
                log_status("DONE", f"{style_unit(display_unit)}: no changes")
                write_output(False)
                return False
            try:
                if lease_ref == desired_ref and lease_acquisition.revision == desired_revision:
                    write_effect_lease(desired, lease_acquisition.lease)
                elif lease_ref == desired_ref:
                    refresh_materialized_root(lease_acquisition.revision, desired)
                desired_revision = lease_acquisition.revision
                assert_desired_ref_fence(desired_ref, desired_revision, args.unit, unit.metadata.uid)
            except BaseException:
                try:
                    release_pre_effect_lease(desired_ref, lease_acquisition, lease_ref=lease_ref)
                except Exception as release_exc:
                    log_status(
                        "WAIT",
                        f"{style_unit(display_unit)}: pre-effect lease release failed; explicit recovery remains: "
                        f"{release_exc}",
                    )
                raise

        def log_compact_failure() -> None:
            if verbose:
                return
            log_status("PLAN" if args.plan else "APPLY", "FAILED")
            log_status(
                "UNCHANGED",
                f"Observation {style_branch(observed_ref)} at {describe_revision(observed_revision)}",
            )
            log_status(
                "UNCHANGED",
                f"Desired state {style_branch(desired_ref)} at {describe_revision(desired_revision)}",
            )
            if not args.plan:
                log_status("WARN", "Driver effects may have occurred; no observation was published")

        if verbose:
            log_status("RUN", f"execute {driver_name} {'planning' if args.plan else 'reconciliation'}")
        else:
            if args.plan:
                reason = (
                    "Plan requested for the current desired state"
                    if receipt_is_current
                    else ("No observation exists" if previous_receipt is None else "Desired inputs changed")
                )
                log_status("CHANGED", reason)
                log_status("ACTION", f"Run {driver_name} planning")
            else:
                reason = (
                    "Reapply requested"
                    if getattr(args, "reapply", False)
                    else "No observation exists"
                    if previous_receipt is None
                    else "Desired inputs changed since the last observation"
                )
                log_status("CHANGED", reason)
                log_status("ACTION", f"Run {driver_name} reconciliation")
        driver_started = False
        try:
            execution: dict[str, Any] = {
                "environment": args.environment,
                "desired_root": desired,
                "desired_revision": desired_revision,
                "source_root": source_root,
                "source_revision": source.revision if source is not None else None,
                "source_path": source.path if source is not None else None,
                "unit_name": unit.name,
                "unit": unit.spec,
                "qualified_name": unit_qualified_name,
                "report": report,
                "execution": DriverExecution.console(),
            }
            if args.plan:
                try:
                    planner = PLANNING_DRIVERS[driver_name]
                except KeyError as exc:
                    raise OperationError(f"{args.unit} uses {driver_name}, which does not support planning") from exc
                planned = planner.plan(PlanningContext(**execution))
                if planned is not None:
                    raise DriverError(f"{driver_name} planning returned a value; planning evidence belongs in reports")
                if verbose:
                    log_status("PLAN", f"{driver_name} planning succeeded")
                    log_status("DONE", f"{style_unit(display_unit)}: no remote changes")
                else:
                    log_status("PLAN", "SUCCEEDED")
                    log_status("EFFECTS", "None; planning does not change remote state")
                write_output(False)
                return False
            assert plugin is not None
            driver_started = True
            output = plugin.reconcile(
                ReconciliationContext(
                    **execution,
                    previous_receipt=previous_receipt,
                )
            )
        except BaseException:
            if lease_acquisition is not None and not driver_started:
                try:
                    release_pre_effect_lease(desired_ref, lease_acquisition, lease_ref=lease_ref)
                except Exception as release_exc:
                    log_status(
                        "WAIT",
                        f"{style_unit(display_unit)}: pre-effect lease release failed; explicit recovery remains: "
                        f"{release_exc}",
                    )
            log_compact_failure()
            raise
        if lease_acquisition is not None:
            assert lease_acquisition is not None
            assert unit.metadata.uid is not None
            try:
                lease_acquisition = rebase_effect_completion(
                    desired_ref,
                    lease_acquisition,
                    args.unit,
                    unit.metadata.uid,
                    desired,
                    lease_ref=lease_ref,
                )
            except EffectLeaseUnavailable as exc:
                log_status("WAIT", f"{style_unit(display_unit)}: {exc}; reconciliation result was not published")
                log_status("DONE", f"{style_unit(display_unit)}: no changes")
                write_output(False)
                return False
            desired_revision = lease_acquisition.revision
        if not isinstance(output, ReconciliationOutput):
            log_compact_failure()
            raise DriverError(f"{driver_name} reconciliation did not return ReconciliationOutput")
        receipt = ReceiptResource(
            gvk=unit.gvk,
            metadata=unit.metadata,
            driver=unit.driver,
            spec=ReceiptSpec(
                subject=ReceiptSubject(
                    apiVersion=unit.gvk.api_version,
                    kind=unit.gvk.kind,
                    name=unit.name,
                    qualifiedName=unit_qualified_name,
                ),
                desired=ReceiptDesired(revision=desired_revision, unitBlob=unit_blob),
                resolvedInputs=getattr(unit.spec, "resolvedInputs", None),
            ),
            status=ReceiptStatus(
                controller=JsonObjectValue(controller_evidence()),
                result=output.result,
            ),
        )
        try:
            revision = publish_observation_cas(
                observed_ref,
                args.unit,
                receipt,
                unit,
                output.artifacts,
                desired_revision,
                desired_ref=desired_ref,
                lease_ref=lease_ref,
                expected_uid=unit.metadata.uid,
                lease_token=lease_acquisition.lease.token if lease_acquisition is not None else None,
                lease_snapshot=lease_acquisition.lease.snapshot if lease_acquisition is not None else None,
            )
        except BaseException:
            log_compact_failure()
            raise
        try:
            artifact_effects = reconciliation_artifact_effects(
                observed,
                unit,
                previous_receipt,
                output.artifacts,
            )
        except Exception:
            previous_artifacts = set(previous_receipt.status.artifacts or {}) if previous_receipt is not None else set()
            artifact_effects = [
                ("UPDATED" if name in previous_artifacts else "ADDED", f"Artifact {style_unit(name)}")
                for name in sorted(output.artifacts)
            ]
        detail(
            "OBSERVE",
            f"receipt published to {style_branch(observed_ref)} at {describe_revision(revision)}",
        )
        if lease_acquisition is not None:
            try:
                release_effect_lease(
                    desired_ref,
                    args.unit,
                    lease_acquisition.lease.token,
                    unit.metadata.uid,
                    lease_ref=lease_ref,
                )
            except OperationError as exc:
                log_status(
                    "WAIT",
                    f"{style_unit(display_unit)}: effect lease release deferred; explicit recovery remains available: {exc}",
                )
                write_output(True)
                if verbose:
                    log_status("DONE", f"{style_unit(display_unit)}: reconciled successfully; lease release deferred")
                else:
                    log_status("APPLY", "SUCCEEDED; lease release deferred")
                    observation_status = "UPDATED" if revision != observed_revision else "UNCHANGED"
                    log_status(
                        observation_status,
                        f"Observation {style_branch(observed_ref)} "
                        f"{describe_revision(observed_revision)} → {describe_revision(revision)}",
                    )
                    for effect_status, message in artifact_effects:
                        log_status(effect_status, message)
                return True
        progressed_desired_revision = progress_durable_stack_projection(
            args.environment,
            desired_ref,
            observed_ref,
        )
        if progressed_desired_revision is not None:
            desired_revision = progressed_desired_revision
        write_output(True)
        if verbose:
            log_status("DONE", f"{style_unit(display_unit)}: reconciled successfully")
        else:
            log_status("APPLY", "SUCCEEDED")
            observation_status = "UPDATED" if revision != observed_revision else "UNCHANGED"
            log_status(
                observation_status,
                f"Observation {style_branch(observed_ref)} "
                f"{describe_revision(observed_revision)} → {describe_revision(revision)}",
            )
            for effect_status, message in artifact_effects:
                log_status(effect_status, message)
            log_status(
                "UNCHANGED",
                f"Desired state {style_branch(desired_ref)} at {describe_revision(desired_revision)}",
            )
        return True


def command_reconcile(args: argparse.Namespace) -> bool:
    canonical_desired, canonical_observed = canonical_deployment_ref_overrides(
        args.environment,
        args.desired_ref,
        args.observed_ref,
    )
    if canonical_desired is not None or canonical_observed is not None:
        args.desired_ref = canonical_desired
        args.observed_ref = canonical_observed
    if args.desired_ref is None or args.observed_ref is None:
        # Validate configured refs before acquiring the effect lock. The
        # implementation resolves them again to preserve its live-config
        # behavior and to keep explicit both-ref calls configuration-free.
        deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, args.observed_ref)
    if not getattr(args, "_unit_is_concrete", False):
        desired_ref = args.desired_ref or deployment_refs(REPOSITORY_ROOT, args.environment)[0]
        with tempfile.TemporaryDirectory(prefix="gitopsctr-unit-address-") as temporary_directory:
            desired_revision = resolve_ref(desired_ref, args.desired_revision)
            desired = Path(temporary_directory) / "desired"
            materialize_revision(desired_revision, desired)
            selector = args.unit
            resources = load_desired_resource_graph(desired)
            tombstones = load_resource_incarnation_evidence(desired)
            addresses = qualified_unit_name_map(resources)
            if not addresses and not tombstones and "/" not in selector:
                args.unit = selector
            else:
                args.unit = resolve_unit_selectors(resources, (selector,), tombstones)[0]
            args.qualified_unit = selector
    with unit_effect_lock(args.environment, getattr(args, "unit", "<invalid>")):
        return _command_reconcile(args)


def require_reconciliation_approval(unit_name: str) -> None:
    approval = style_text("APPROVE", "warning")
    padding = " " * (8 - len("APPROVE") + 1)
    print(
        f"    {approval}{padding}Continue with {style_unit(unit_name)}? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    if answer not in {"y", "yes"}:
        raise OperationError(f"reconciliation of {unit_name} was not approved")


def log_compact_convergence_summary(
    environment: str,
    scope: Sequence[str],
    steps: list[str],
    result: str,
    unselected: list[tuple[str, str, str]] | None = None,
    display_names: Mapping[str, str] | None = None,
) -> None:
    names = display_names or {}
    display_scope = tuple(names.get(name, name) for name in scope)
    display_steps = [names.get(name, name) for name in steps]
    log_heading(f"Convergence result for {style_environment(environment)}")
    if result == "CLEAN":
        driver_summary = f"drivers ran for {style_units(display_steps)}" if display_steps else "no drivers ran"
        log_status("RESULT", f"CLEAN: {len(display_scope)}/{len(display_scope)} units; {driver_summary}")
    else:
        log_status("RESULT", result)
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{style_unit(names.get(unit_name, unit_name))}: {status.lower()}; {reason}")


def command_dependencies(args: argparse.Namespace) -> None:
    source_revision = git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_root = Path(temporary_directory) / "source"
        materialize_revision(source_revision, source_root)
        current_desired = Path(temporary_directory) / "current-desired"
        current_desired.mkdir()
        loaded = load_convergence_specifications(
            source_root,
            args.environment,
            current_desired,
            source_revision,
            Path(temporary_directory) / "stack-projection",
        )
        specifications, stack_dependencies = loaded.units, loaded.dependencies
        addresses = {qualified: concrete for concrete, qualified in loaded.qualified_names.items()}
        requested = resolve_qualified_unit_values(args.unit or (), addresses)
        selection = convergence_scope(specifications, requested, args.depth, stack_dependencies)
        targets, scope = selection.targets, selection.scope
        graph = dependency_graph(specifications, scope, stack_dependencies)
        order = convergence_order(specifications, scope, stack_dependencies)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "environment": args.environment,
                    "sourceRevision": source_revision,
                    "targets": targets,
                    "units": [
                        {"name": unit_name, "dependencies": graph.dependencies[unit_name]} for unit_name in order
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.list:
        print("\n".join(loaded.qualified_names.get(name, name) for name in order))
        return
    for index, target in enumerate(targets):
        if index:
            print()
        print(
            "\n".join(
                graph.render_tree(
                    target,
                    lambda unit_name: style_unit(loaded.qualified_names.get(unit_name, unit_name), sys.stdout),
                )
            )
        )


def _partition_unit_names(desired: Path, partition: str) -> list[str]:
    resources = load_desired_resource_graph(desired)
    selected: set[tuple[str, str, str]] = set()
    for resource in resources.values():
        if resource_owner_reference(resource) is None and resource.metadata.partition == partition:
            closure = _owned_resource_closure(resources, resource)
            selected.update(key for key, item in resources.items() if any(item is child for child in closure))
    return sorted(
        key[2] for key, resource in resources.items() if key in selected and isinstance(resource, UnitResource)
    )


def _desired_convergence_model(
    desired: Path,
) -> tuple[dict[str, UnitResource[Any]], dict[str, tuple[str, ...]]]:
    resources = load_desired_resource_graph(desired)
    units = {key[2]: resource for key, resource in resources.items() if isinstance(resource, UnitResource)}
    return units, stack_dependency_edges(resources)


def _resource_deletion_blockers(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    target: UnitResource[Any] | StackResource,
) -> tuple[UnitResource[Any] | StackResource, ...]:
    """Return resources that must disappear before ``target`` can finalize."""

    target_identity = (
        target.gvk.api_version,
        target.gvk.kind,
        target.name,
        target.metadata.uid or "",
    )
    if target.gvk.kind == "Stack":
        return tuple(
            sorted(
                (
                    child
                    for child in resources.values()
                    if (owner := resource_owner_reference(child)) is not None
                    and (owner.apiVersion, owner.kind, owner.name, owner.uid) == target_identity
                ),
                key=lambda item: (item.gvk.kind, item.name),
            )
        )
    if target.gvk.kind == "StackTemplate":
        return tuple(
            sorted(
                (
                    child
                    for child in resources.values()
                    if isinstance(child, StackResource)
                    and child.gvk.kind == "Stack"
                    and isinstance(child.spec, DesiredStackSpec)
                    and child.spec.templateRef.name == target.name
                    and child.spec.templateRef.uid == target.metadata.uid
                ),
                key=lambda item: (item.gvk.kind, item.name),
            )
        )
    explicit_dependencies = stack_dependency_edges(resources, include_missing=True)
    target_key = next((key for key, resource in resources.items() if resource is target), None)
    if target_key is None:
        raise OperationError(f"desired resource {target.name!r} is absent from its graph")
    blockers: dict[tuple[str, str, str], UnitResource[Any] | StackResource] = {}
    pending = [target_key]
    visited = {target_key}
    while pending:
        parent_key = pending.pop()
        parent = resources[parent_key]
        parent_identity = (
            parent.gvk.api_version,
            parent.gvk.kind,
            parent.name,
            parent.metadata.uid or "",
        )
        for key, child in resources.items():
            if key in visited:
                continue
            owner = resource_owner_reference(child)
            owner_matches = (
                owner is not None
                and (
                    owner.apiVersion,
                    owner.kind,
                    owner.name,
                    owner.uid,
                )
                == parent_identity
            )
            dependency_matches = isinstance(child, UnitResource) and parent_key[2] in (
                set(desired_observation_reference_units(child)) | set(explicit_dependencies.get(key[2], ()))
            )
            template_matches = (
                parent_identity[1] == "StackTemplate"
                and isinstance(child, StackResource)
                and child.gvk.kind == "Stack"
                and isinstance(child.spec, DesiredStackSpec)
                and child.spec.templateRef.name == parent_identity[2]
                and child.spec.templateRef.uid == parent_identity[3]
            )
            if owner_matches or dependency_matches or template_matches:
                visited.add(key)
                blockers[key] = child
                pending.append(key)
    return tuple(sorted(blockers.values(), key=lambda item: (item.gvk.kind, item.name)))


def _deletion_scope_resources(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    *,
    partition: str | None = None,
    selected_units: Sequence[str] | None = None,
) -> frozenset[tuple[str, str, str]]:
    """Select deletion roots plus their ownership/dependency closure."""

    if partition is None and selected_units is None:
        return frozenset(resources)

    selected: set[tuple[str, str, str]] = set()
    if partition is not None:
        roots = [
            resource
            for resource in resources.values()
            if resource_owner_reference(resource) is None and resource.metadata.partition == partition
        ]
    else:
        names = set(selected_units or ())
        roots = [
            resource for key, resource in resources.items() if isinstance(resource, UnitResource) and key[2] in names
        ]
    for root in roots:
        closure = _owned_resource_closure(resources, root)
        selected.update(key for key, resource in resources.items() if any(resource is item for item in closure))
    authorized_partitions = {_resource_management_partition(resources, root) for root in roots}

    # A deleting producer cannot finalize until its dependents have either
    # finalized or stopped being active. Include the reverse dependency
    # closure, then include deleting controller parents that become safe only
    # after the selected child/Stack disappears.
    changed = True
    while changed:
        changed = False
        for key in tuple(selected):
            resource = resources.get(key)
            if resource is None or resource_deletion(resource) is None:
                continue
            blockers = _resource_deletion_blockers(resources, resource)
            for blocker in blockers:
                if resource_deletion(blocker) is None:
                    continue
                if _resource_management_partition(resources, blocker) not in authorized_partitions:
                    # Partition selection is an authority boundary. A deleting
                    # dependent in another partition blocks this root but is
                    # progressed only by its own partition converge.
                    continue
                blocker_key = (blocker.gvk.api_version, blocker.gvk.kind, blocker.name)
                if blocker_key in selected:
                    continue
                selected.update(
                    (item.gvk.api_version, item.gvk.kind, item.name)
                    for item in _owned_resource_closure(resources, blocker)
                )
                changed = True
            for parent_key in _deletion_parent_keys(resources, resource):
                parent = resources.get(parent_key)
                if parent is None or resource_deletion(parent) is None or parent_key in selected:
                    continue
                if _resource_management_partition(resources, parent) not in authorized_partitions:
                    continue
                selected.update(
                    key
                    for key, item in resources.items()
                    if any(item is child for child in _owned_resource_closure(resources, parent))
                )
                changed = True
    return frozenset(selected)


def _deletion_queue(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    scope: frozenset[tuple[str, str, str]],
) -> tuple[UnitResource[Any] | StackResource, ...]:
    """Return eligible deleting resources in dependent/child-first order."""

    candidates = [
        resource for key, resource in resources.items() if key in scope and resource_deletion(resource) is not None
    ]
    eligible: list[UnitResource[Any] | StackResource] = []
    for resource in candidates:
        blockers = _resource_deletion_blockers(resources, resource)
        if not blockers:
            eligible.append(resource)
    return tuple(sorted(eligible, key=lambda item: (item.gvk.kind, item.name)))


def _selected_finalized_cleanup(
    desired: Path,
    *,
    partition: str | None,
    requested_units: Sequence[str],
) -> tuple[ResourceIncarnationTombstone, ...]:
    """Return tombstone-backed cleanup work authorized by the selector."""

    evidence = load_resource_incarnation_evidence(desired)
    cleanup_capable = tuple(
        tombstone
        for tombstone in evidence
        if tombstone.kind in {"Stack", "StackTemplate"}
        or f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
    )
    if partition is not None:
        return tuple(tombstone for tombstone in cleanup_capable if tombstone.partition == partition)
    if not requested_units:
        return cleanup_capable
    names = set(requested_units)
    selected_units = tuple(
        tombstone
        for tombstone in cleanup_capable
        if tombstone.qualified_name in names and f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
    )
    partitions = {tombstone.partition for tombstone in selected_units if tombstone.partition is not None}
    return selected_units + tuple(
        tombstone
        for tombstone in cleanup_capable
        if tombstone.kind == "StackTemplate" and tombstone.partition in partitions
    )


def _retry_finalized_cleanup(
    tombstone: ResourceIncarnationTombstone,
    *,
    environment: str,
    desired_ref: str,
    current_revision: str,
    desired_root: Path,
) -> bool:
    """Retry only post-publication cleanup; never repeat external teardown."""

    live_desired_ref, _live_observed_ref = deployment_refs(REPOSITORY_ROOT, environment, None, None)
    if desired_ref != live_desired_ref:
        raise OperationError("deletion cleanup requires the live desired ref; unaccepted candidates are inert")
    accepted_revision = fetch_ref(desired_ref)
    if accepted_revision != current_revision:
        raise OperationError("deletion cleanup requires the accepted desired head; historical candidates are inert")

    if tombstone.kind == "StackTemplate":
        return _release_finalized_stack_template_pins(
            environment,
            tombstone.name,
            tombstone.uid,
            tombstone.deletion_generation,
            desired_root,
            AcceptedDesiredTarget(desired_ref, current_revision),
        )
    if tombstone.kind == "Stack":
        return _release_finalized_stack_workload_pins(
            environment,
            tombstone.name,
            tombstone.uid,
            tombstone.deletion_generation,
            desired_root,
            AcceptedDesiredTarget(desired_ref, current_revision),
        )
    if f"{tombstone.api_version}/{tombstone.kind}" not in DRIVER_NAMES_BY_GVK:
        return False
    return _release_finalized_unit_lease(
        tombstone.qualified_name or tombstone.name,
        tombstone.uid,
        tombstone.deletion_generation,
        desired_ref,
        current_revision,
        desired_root,
    )


def command_converge(args: argparse.Namespace) -> None:
    """Converge persisted desired Units, optionally reapplying one input snapshot."""

    if args.max_steps is not None and args.max_steps < 1:
        raise OperationError("--max-steps must be a positive integer")
    if args.partition is not None:
        _resource_name(args.partition, "partition name")
    explicit_configuration = args.desired_ref is not None and args.observed_ref is not None
    if explicit_configuration:
        desired_ref, observed_ref = canonical_deployment_refs(
            args.environment,
            cast(str, args.desired_ref),
            cast(str, args.observed_ref),
        )
    else:
        desired_ref, observed_ref = deployment_refs(
            REPOSITORY_ROOT,
            args.environment,
            args.desired_ref,
            args.observed_ref,
        )
    apply_arguments = None
    if args.files:
        apply_arguments = argparse.Namespace(
            environment=args.environment,
            files=list(args.files),
            partition=args.partition,
            source_revision=args.source_revision,
            desired_ref=desired_ref,
            observed_ref=observed_ref,
            candidate_ref=args.candidate_ref,
            dry=False,
            verbose=args.verbose,
        )
    log_heading(f"Converge {style_environment(args.environment)}")
    steps: list[str] = []
    attempted_steps = 0
    previous_ready: set[str] = set()
    max_steps = args.max_steps
    iteration = 0
    selected_partition_once = False
    requested_selectors = tuple(dict.fromkeys(args.unit or ()))
    requested_units: tuple[str, ...] = ()
    seen_requested_units: set[str] = set()
    reconciled = False

    def finish() -> None:
        write_reconcile_outputs(reconciled)

    with tempfile.TemporaryDirectory(prefix="gitopsctr-converge-") as temporary_directory:
        temporary = Path(temporary_directory)
        while True:
            iteration += 1
            if apply_arguments is not None:
                previous_revision = fetch_ref(desired_ref)
                applied_revision = command_apply(apply_arguments)
                target_revision = fetch_ref(desired_ref)
                if applied_revision is not None and target_revision != previous_revision:
                    reconciled = True
                if applied_revision is not None and applied_revision != target_revision:
                    raise OperationError(
                        "apply produced a reviewed candidate; merge it before converging the target desired state"
                    )
            else:
                # A producer may have published evidence independently of this
                # converge invocation.  Re-project durable Stack intent before
                # calculating the next coverage set so newly resolvable consumers
                # enter the same convergence run without authored source input.
                previous_revision = fetch_ref(desired_ref)
                projected_revision = progress_durable_stack_projection(
                    args.environment,
                    desired_ref,
                    observed_ref,
                )
                if projected_revision is not None and projected_revision != previous_revision:
                    reconciled = True
            desired_revision = fetch_ref(desired_ref)
            if desired_revision is None:
                raise OperationError(f"desired ref {desired_ref!r} has no state; apply resources first")
            desired = temporary / f"desired-{iteration}"
            observed = temporary / f"observed-{iteration}"
            materialize_revision(desired_revision, desired)
            observed_tree(observed_ref, observed)
            resources = load_desired_resource_graph(desired)
            tombstones = load_resource_incarnation_evidence(desired)
            display_names = {concrete: qualified for qualified, concrete in qualified_unit_name_map(resources).items()}
            if requested_selectors and not requested_units:
                requested_units = resolve_unit_selectors(resources, requested_selectors, tombstones)
            specifications, stack_dependencies = _desired_convergence_model(desired)
            finalized_cleanup = _selected_finalized_cleanup(
                desired,
                partition=args.partition,
                requested_units=requested_units,
            )
            if args.partition is not None:
                selected_units = _partition_unit_names(desired, args.partition)
            elif requested_units:
                present_requested_units = [name for name in requested_units if name in specifications]
                finalized_unit_names = {
                    tombstone.qualified_name
                    for tombstone in finalized_cleanup
                    if f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
                }
                unseen_requested_units = [
                    name
                    for name in requested_units
                    if name not in specifications
                    and name not in seen_requested_units
                    and name not in finalized_unit_names
                ]
                selected_units = list(requested_units) if unseen_requested_units else present_requested_units
                seen_requested_units.update(present_requested_units)
                seen_requested_units.update(finalized_unit_names & set(requested_units))
            else:
                selected_units = None
            deletion_scope = _deletion_scope_resources(
                resources,
                partition=args.partition,
                selected_units=selected_units if args.partition is None and args.unit is not None else None,
            )
            if args.partition is not None and not selected_units and not deletion_scope and not finalized_cleanup:
                if selected_partition_once:
                    log_compact_convergence_summary(args.environment, (), steps, "CLEAN")
                    finish()
                    return
                raise OperationError(f"partition {args.partition!r} selects no desired resources")
            if args.partition is not None:
                selected_partition_once = True
            if requested_units:
                if (
                    not selected_units
                    and not deletion_scope
                    and not finalized_cleanup
                    and seen_requested_units == set(requested_units)
                ):
                    log_compact_convergence_summary(args.environment, (), steps, "CLEAN")
                    finish()
                    return
            deletion_units = {
                key[2]
                for key, resource in resources.items()
                if key in deletion_scope
                and isinstance(resource, UnitResource)
                and resource_deletion(resource) is not None
            }
            if args.partition is not None and not selected_units:
                scope = tuple(sorted(deletion_units))
                order = ()
            else:
                selection = convergence_scope(
                    specifications, selected_units, additional_dependencies=stack_dependencies
                )
                scope = tuple(sorted(set(selection.scope) | deletion_units))
                order = convergence_order(specifications, scope, stack_dependencies)
            statuses = reconciliation_statuses(scope, desired, observed)
            scoped_names = set(scope)
            statuses = [item for item in statuses if item[0] in scoped_names]
            if args.verbose:
                log_reconciliation_status(args.environment, statuses, desired_revision, desired, observed, True)
            deleting = tuple(
                resource
                for key, resource in resources.items()
                if key in deletion_scope and resource_deletion(resource) is not None
            )
            if max_steps is None:
                max_steps = max(2, 2 * (len(scope) + len(deleting) + len(finalized_cleanup)))
            deletion_queue = _deletion_queue(resources, deletion_scope)
            cleanup_progressed = False
            for tombstone in finalized_cleanup:
                if len(steps) >= max_steps:
                    raise OperationError(f"convergence did not finish within {max_steps} reconciliation steps")
                if _retry_finalized_cleanup(
                    tombstone,
                    environment=args.environment,
                    desired_ref=desired_ref,
                    current_revision=desired_revision,
                    desired_root=desired,
                ):
                    steps.append(f"{tombstone.kind}/{tombstone.name} cleanup")
                    cleanup_progressed = True
                    break
            if cleanup_progressed:
                reconciled = True
                continue
            if not deleting and all(status in {"CLEAN", "MATERIALIZED"} for _, status, _ in statuses):
                log_compact_convergence_summary(args.environment, scope, steps, "CLEAN", display_names=display_names)
                finish()
                return
            ready = [
                name for name in order if dict((name, status) for name, status, _ in statuses).get(name) == "READY"
            ]
            if args.fail_on_repeat and any(name in previous_ready for name in ready):
                repeated = sorted(name for name in ready if name in previous_ready)
                raise OperationError("convergence heuristic detected repeated ready unit(s): " + ", ".join(repeated))
            previous_ready.update(ready)
            if deletion_queue:
                progressed = False
                for deleting_resource in deletion_queue:
                    if attempted_steps >= max_steps:
                        raise OperationError(f"convergence did not finish within {max_steps} reconciliation steps")
                    deletion = resource_deletion(deleting_resource)
                    assert deletion is not None
                    deleting_name = next(key[2] for key, item in resources.items() if item is deleting_resource)
                    deletion_args = argparse.Namespace(
                        kind=deleting_resource.gvk.kind,
                        name=deleting_name,
                        environment=args.environment,
                        desired_ref=desired_ref,
                        observed_ref=observed_ref,
                        candidate_ref=args.candidate_ref,
                        uid=deleting_resource.metadata.uid,
                        deletion_generation=deletion.generation,
                        report=None,
                        dry=False,
                    )
                    if isinstance(deleting_resource, UnitResource):
                        progressed = command_reconcile(
                            argparse.Namespace(
                                unit=deleting_name,
                                environment=args.environment,
                                desired_ref=desired_ref,
                                desired_revision=desired_revision,
                                observed_ref=observed_ref,
                                plan=False,
                                report=None,
                                reapply=False,
                                verbose=args.verbose,
                                qualified_unit=deleting_name,
                                _defer_reconcile_output=True,
                                _unit_is_concrete=True,
                            )
                        )
                    else:
                        if deleting_resource.gvk.kind == "Stack":
                            deletion_context = temporary / (f"deletion-context-{iteration}-{deleting_resource.name}")
                            _materialize_durable_projection_source(
                                desired,
                                args.environment,
                                deletion_context,
                                _stack_effect_context_digest(cast(StackResource, deleting_resource)),
                            )
                            deletion_args.configuration_root = deletion_context
                            deletion_args.lease_ref = effect_lease_ref(
                                args.environment,
                                desired_ref,
                                deletion_context,
                            )
                        progressed = _progress_deletion(deletion_args)
                    attempted_steps += 1
                    if progressed:
                        reconciled = True
                        steps.append(deleting_name)
                        break
                if progressed:
                    # Every deletion publication changes the desired graph or
                    # observed evidence.  Reload it before considering a
                    # newly unblocked parent.
                    continue
            if not ready:
                waiting = [
                    f"{display_names.get(name, name)} ({reason})"
                    for name, status, reason in statuses
                    if status == "WAIT"
                ]
                waiting.extend(
                    f"{resource.gvk.kind}/{resource.name} (deletion blocked)"
                    for resource in deleting
                    if resource not in deletion_queue
                )
                log_compact_convergence_summary(
                    args.environment,
                    scope,
                    steps,
                    "WAIT: " + (", ".join(waiting) or "deletion progression is waiting"),
                    display_names=display_names,
                )
                finish()
                return
            if attempted_steps >= max_steps:
                raise OperationError(f"convergence did not finish within {max_steps} reconciliation steps")
            unit_name = ready[0]
            if not args.yes:
                require_reconciliation_approval(display_names.get(unit_name, unit_name))
            unit_progressed = command_reconcile(
                argparse.Namespace(
                    unit=unit_name,
                    environment=args.environment,
                    desired_ref=desired_ref,
                    desired_revision=desired_revision,
                    observed_ref=observed_ref,
                    plan=False,
                    report=None,
                    reapply=False,
                    verbose=args.verbose,
                    qualified_unit=display_names.get(unit_name, unit_name),
                    _defer_reconcile_output=True,
                    _unit_is_concrete=True,
                )
            )
            attempted_steps += 1
            if not unit_progressed:
                finish()
                log_compact_convergence_summary(
                    args.environment,
                    scope,
                    steps,
                    "WAIT: reconciliation made no progress",
                    display_names=display_names,
                )
                return
            reconciled = True
            steps.append(unit_name)


def _resource_name(value: str, description: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise OperationError(f"invalid {description}: {value!r}")
    return value


def _creation_target(directory: Path, stem: str, *, suffix: str, force: bool) -> Path:
    candidates = document_candidates(directory, stem)
    if len(candidates) > 1:
        raise OperationError(f"multiple document formats exist for {stem}: {', '.join(map(str, candidates))}")
    if candidates:
        if not force:
            raise OperationError(f"resource already exists: {candidates[0]}")
        return candidates[0]
    return directory / f"{stem}{suffix}"


def _print_created(path: Path) -> None:
    try:
        print(path.relative_to(REPOSITORY_ROOT))
    except ValueError:
        print(path)


def _document_format_for_path(path: Path) -> DocumentFormat:
    return DocumentFormat.JSON if path.suffix.lower() == ".json" else DocumentFormat.YAML


def _parse_optional_units(value: str | None) -> list[str] | None:
    if value is None:
        return None
    units = [item.strip() for item in value.split(",") if item.strip()]
    if not units or len(set(units)) != len(units):
        raise OperationError("--units must contain one or more unique Unit template names")
    return units


def _lexical_apply_input_path(value: str) -> Path:
    """Normalize a caller-spelled path without following any symlink."""

    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _revision_apply_input_relative_path(
    value: str,
    *,
    operation: str,
    revision_option: str,
) -> Path:
    """Map a caller-spelled path lexically into the repository namespace."""

    lexical = _lexical_apply_input_path(value)
    try:
        return lexical.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise OperationError(
            f"{operation} input {value!r} is outside the project repository and cannot be used with {revision_option}"
        ) from exc


def _resolve_checked_apply_path(path: Path, *, source_root: Path | None, value: str) -> Path:
    """Resolve an input path and, for snapshots, prove it remains inside the snapshot."""

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        # Preserve the caller's ordinary "input does not exist" diagnostic.
        # Strict resolution is needed because Python 3.13+ no longer raises
        # for symlink loops in the default non-strict mode.
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise OperationError(f"{value!r} contains an invalid or looping symbolic link: {exc}") from exc
    if source_root is not None:
        try:
            resolved.relative_to(source_root.resolve())
        except ValueError as exc:
            raise OperationError(
                f"apply input {value!r} resolves outside the selected source revision snapshot"
            ) from exc
    return resolved


def _validate_apply_input_selection(
    files: Sequence[str],
    source_revision: str | None,
    *,
    operation: str,
    revision_option: str,
) -> None:
    """Validate input selection before materializing a revision-backed source."""

    if not files:
        raise OperationError(f"{operation} requires at least one --file")
    if files.count("-") > 1:
        raise OperationError("standard input may be specified only once")
    if source_revision is None:
        return
    if "-" in files:
        raise OperationError(f"{operation} with {revision_option} does not accept standard input ('-')")
    for value in files:
        _revision_apply_input_relative_path(
            value,
            operation=operation,
            revision_option=revision_option,
        )


def _resolve_apply_input_path(
    value: str,
    source_revision: str | None,
    source_root: Path | None,
    *,
    operation: str = "apply",
    revision_option: str = "--source-revision",
) -> Path:
    """Resolve a user-spelled apply or promotion path from the caller's CWD."""

    if source_revision is None:
        return _resolve_checked_apply_path(_lexical_apply_input_path(value), source_root=None, value=value)
    if source_root is None:
        raise OperationError(f"revision-backed {operation} inputs require a materialized source snapshot")
    relative = _revision_apply_input_relative_path(
        value,
        operation=operation,
        revision_option=revision_option,
    )
    return _resolve_checked_apply_path(source_root / relative, source_root=source_root, value=value)


def _load_apply_documents(
    files: Sequence[str],
    *,
    source_revision: str | None = None,
    source_root: Path | None = None,
    operation: str = "apply",
    revision_option: str = "--source-revision",
) -> list[ApplyInputDocument]:
    """Load a deterministic resource stream from live or revision-backed paths."""

    _validate_apply_input_selection(
        files,
        source_revision,
        operation=operation,
        revision_option=revision_option,
    )
    loaded: list[ApplyInputDocument] = []
    paths: list[Path] = []
    for value in files:
        if value == "-":
            raw_input = sys.stdin.read()
            try:
                values = list(yaml.safe_load_all(raw_input))
                nodes = list(yaml.compose_all(raw_input))
            except yaml.YAMLError as exc:
                raise OperationError(f"standard input is invalid YAML: {exc}") from exc
            parsed_documents = [
                (value_document, node)
                for value_document, node in zip(values, nodes, strict=False)
                if value_document is not None and node is not None
            ]
            for index, (value_document, node) in enumerate(parsed_documents, 1):
                if value_document is None:
                    continue
                try:
                    document = require_json_value(value_document)
                except ValueError as exc:
                    raise OperationError(f"standard input document {index} is invalid: {exc}") from exc
                if not isinstance(document, dict):
                    raise OperationError(f"standard input document {index} must be a resource mapping")
                if len(parsed_documents) == 1:
                    # A single stdin document is acquired as one opaque byte
                    # stream.  This deliberately includes comments,
                    # document markers, trailing whitespace, and separators.
                    raw_document = raw_input.encode()
                else:
                    # For multi-document stdin, assign each byte to the
                    # adjacent parsed document.  The first segment starts at
                    # byte zero so leading comments and ``---`` belong to the
                    # first document; each later segment starts at its node's
                    # parser offset.  This gives deterministic, contiguous
                    # provenance without pretending the YAML parser can
                    # preserve comments in a JSON value.
                    start = 0 if index == 1 else node.start_mark.index
                    end = (
                        len(raw_input)
                        if index == len(parsed_documents)
                        else parsed_documents[index][1].start_mark.index
                    )
                    raw_document = raw_input[start:end].encode()
                loaded.append(
                    ApplyInputDocument(
                        origin=f"stdin#{index}",
                        document=document,
                        document_digest=f"sha256:{hashlib.sha256(raw_document).hexdigest()}",
                    )
                )
            continue
        path = _resolve_apply_input_path(
            value,
            source_revision,
            source_root,
            operation=operation,
            revision_option=revision_option,
        )
        if not path.exists():
            raise OperationError(f"apply input does not exist: {value}")
        if path.is_dir():
            try:
                children = sorted(path.rglob("*"))
            except (OSError, RuntimeError) as exc:
                raise OperationError(f"{value!r} contains an invalid or looping symbolic link: {exc}") from exc
            for child in children:
                checked_child = _resolve_checked_apply_path(child, source_root=source_root, value=value)
                if checked_child.is_file() and checked_child.suffix.lower() in {".json", ".yaml", ".yml"}:
                    paths.append(checked_child)
        elif path.suffix.lower() in {".json", ".yaml", ".yml"}:
            paths.append(path)
        else:
            raise OperationError(f"apply input must be YAML or JSON: {value}")
    for path in paths:
        try:
            raw_document = path.read_bytes()
            loaded.append(
                ApplyInputDocument(
                    origin=str(path),
                    document=RESOURCE_CATALOG.load_document(path),
                    document_digest=f"sha256:{hashlib.sha256(raw_document).hexdigest()}",
                )
            )
        except (DocumentFormatError, OperationError, OSError, RuntimeError) as exc:
            raise OperationError(f"{path}: {exc}") from exc
    identities: dict[tuple[str, str], str] = {}
    for item in loaded:
        origin, document = item.origin, item.document
        api_version = document.get("apiVersion")
        kind = document.get("kind")
        metadata = document.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not all(isinstance(value, str) and value for value in (api_version, kind, name)):
            raise OperationError(f"{origin}: resource requires apiVersion, kind, and metadata.name")
        family = "stack" if kind == "Stack" else ("stacktemplate" if kind == "StackTemplate" else "unit")
        identity = (family, cast(str, name))
        if previous := identities.get(identity):
            raise OperationError(f"duplicate apply resource {identity!r}: {previous} and {origin}")
        identities[identity] = origin
    return loaded


def _document_is_canonical_desired(document: JsonObject) -> bool:
    """Discriminate desired input structurally by controller identity fields."""

    metadata = document.get("metadata")
    return isinstance(metadata, dict) and "uid" in metadata


def _copy_apply_source_base(source: Path, destination: Path, environment: str) -> None:
    """Keep the selected source payload while excluding implicit environment resources."""

    source_environment = project_environment_root(source, environment)
    environment_paths = document_candidates(source_environment, "environment")
    if len(environment_paths) != 1:
        raise OperationError(f"expected exactly one environment document for {environment}")
    shutil.copytree(source, destination)
    target_environment = project_environment_root(destination, environment)
    for collection in ("units", "stacks"):
        shutil.rmtree(target_environment / collection, ignore_errors=True)
    project = load_project_config(destination)
    template_root = destination.joinpath(*project.stack_templates_path.parts)
    if template_root.is_dir():
        # StackTemplate resource documents are selected inputs and are
        # rewritten below.  Everything else under this directory is payload
        # for repository-backed Unit sources and must survive the copy.
        for path in _document_paths(template_root).values():
            document = load_json(path)
            if document.get("kind") == "StackTemplate":
                path.unlink()


def _materialize_apply_worktree(destination: Path) -> None:
    """Copy the current, non-ignored worktree without inventing a Git revision."""

    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for value in listed.stdout.split("\0"):
        if not value:
            continue
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise OperationError(f"Git returned an unsafe worktree path: {value!r}")
        source = REPOSITORY_ROOT.joinpath(*relative.parts)
        if not source.exists() and not source.is_symlink():
            # A tracked deletion is part of the current worktree.
            continue
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)


def _write_apply_authored_documents(
    source: Path,
    environment: str,
    documents: Sequence[ApplyInputDocument],
    document_digests: dict[str, str] | None = None,
) -> tuple[list[UnitResource[Any]], list[StackResource]]:
    environment_root = project_environment_root(source, environment)
    environment_root.mkdir(parents=True, exist_ok=True)
    units: list[UnitResource[Any]] = []
    stacks: list[StackResource] = []
    for item in documents:
        document = item.document
        if document_digests is not None and document.get("kind") == "StackTemplate":
            metadata_value = document.get("metadata")
            name = metadata_value.get("name") if isinstance(metadata_value, dict) else None
            if isinstance(name, str):
                document_digests[name] = item.document_digest
        kind = document.get("kind")
        metadata = document.get("metadata")
        assert isinstance(metadata, dict)
        name = cast(str, metadata["name"])
        if _document_is_canonical_desired(document):
            if kind == "StackTemplate":
                desired = RESOURCE_CATALOG.parse_stack_template(document, profile="desired", expected_name=name)
                if not desired.metadata.is_root or resource_deletion(desired) is not None:
                    raise OperationError(f"canonical StackTemplate {name!r} must be a non-deleting root")
                assert isinstance(desired.spec, DesiredStackTemplateSpec)
                normalized = StackResource(
                    desired.gvk,
                    ResourceMetadata(name=name),
                    StackTemplateInlineSpec(
                        parameters=list(desired.spec.parameters),
                        unitTemplates=dict(desired.spec.unitTemplates),
                    ),
                )
                directory = source.joinpath(*load_project_config(source).stack_templates_path.parts)
                document = RESOURCE_CATALOG.serialize_stack_resource(normalized, profile="authored")
            elif kind == "Stack":
                desired = RESOURCE_CATALOG.parse_stack(document, profile="desired", expected_name=name)
                if not desired.metadata.is_root or resource_deletion(desired) is not None:
                    raise OperationError(f"canonical Stack {name!r} must be a non-deleting root")
                assert isinstance(desired.spec, DesiredStackSpec)
                normalized = StackResource(
                    desired.gvk,
                    ResourceMetadata(name=name),
                    StackSpec(
                        template=desired.spec.templateRef.name,
                        parameters=desired.spec.parameters,
                        units=desired.spec.units,
                        artifactImports=desired.spec.artifactImports,
                    ),
                )
                stacks.append(normalized)
                directory = environment_root / "stacks"
                document = RESOURCE_CATALOG.serialize_stack_resource(normalized, profile="authored")
            else:
                # Canonical materialized Units are checked and rejected in
                # command_apply because their immutable materialization tree
                # cannot be carried through -f input.
                continue
        if kind == "Stack":
            if not stacks or stacks[-1].name != name:
                resource = RESOURCE_CATALOG.parse_stack(document, profile="authored", expected_name=name)
                stacks.append(resource)
            directory = environment_root / "stacks"
        elif kind == "StackTemplate":
            RESOURCE_CATALOG.parse_stack_template(document, profile="authored", expected_name=name)
            directory = source.joinpath(*load_project_config(source).stack_templates_path.parts)
        else:
            resource = RESOURCE_CATALOG.parse_unit(document, profile="authored", expected_name=name)
            units.append(resource)
            directory = environment_root / "units"
        write_document(directory / f"{name}.yaml", document, format=DocumentFormat.YAML)
    return units, stacks


def _validate_apply_source_revision(
    source_revision: str | None,
    units: Sequence[UnitResource[Any]],
    stacks: Sequence[StackResource],
) -> None:
    """Reject repository-backed input before apply can mutate deployment refs."""

    if source_revision is not None:
        return
    for unit in units:
        _driver, source = require_unit_specification(unit)
        if source is not None:
            raise OperationError(
                f"Unit {unit.name!r} uses repository-backed source; apply it with --source-revision <commit>"
            )
    # Stack source selection is deliberately not a source mode. The desired
    # StackTemplate is resolved from the complete candidate graph by the
    # projection engine, which also validates any stored source context.


def _explicit_applied_root_identities(
    documents: Sequence[ApplyInputDocument],
    roots: Sequence[UnitResource[Any] | StackResource],
) -> frozenset[tuple[str, str, str]]:
    """Return the independently supplied roots, including explicit templates."""

    applied = {(resource.gvk.api_version, resource.gvk.kind, resource.name) for resource in roots}
    for item in documents:
        document = item.document
        kind = document.get("kind")
        metadata = document.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if kind == "StackTemplate" and isinstance(name, str):
            applied.add((CORE_API_VERSION, "StackTemplate", name))
    return frozenset(applied)


def _copy_unrelated_desired_resources(
    current: Path,
    candidate: Path,
    applied: frozenset[tuple[str, str, str]],
    partition: str | None,
) -> None:
    current_resources = load_desired_resource_graph(current) if any(current.iterdir()) else {}
    candidate_resources = load_desired_resource_graph(candidate, validate=False)
    copied: set[tuple[str, str, str]] = set()
    for key, resource in current_resources.items():
        if key in copied or key in applied or resource_owner_reference(resource) is not None:
            continue
        if partition is not None and resource.metadata.partition == partition:
            continue
        for selected in _owned_resource_closure(current_resources, resource):
            selected_key = (
                selected.gvk.api_version,
                selected.gvk.kind,
                _unit_storage_name(selected) if isinstance(selected, UnitResource) else selected.name,
            )
            copied.add(selected_key)
            if selected_key in candidate_resources:
                continue
            source_path = _desired_resource_path(current, selected)
            target = candidate / source_path.relative_to(current)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            if isinstance(selected, UnitResource) and getattr(selected.spec, "materialization", None) is not None:
                copy_unit_materialization(current, candidate, selected_key[2], selected)


def _prune_omitted_partition_resources(
    current: Path,
    candidate: Path,
    applied: frozenset[tuple[str, str, str]],
    partition: str | None,
) -> None:
    """Retain omitted partition roots and their closure as deletion requests."""

    if partition is None or not any(current.iterdir()):
        return
    current_resources = load_desired_resource_graph(current)
    for key, resource in current_resources.items():
        if resource_owner_reference(resource) is not None or resource.metadata.partition != partition or key in applied:
            continue
        for selected in _owned_resource_closure(current_resources, resource):
            _write_desired_resource(
                candidate / _desired_resource_path(current, selected).relative_to(current),
                # Deletion intent fences the exact currently accepted
                # resource. A template fan-out may have produced a newer
                # candidate closure earlier in this build, but teardown must
                # never silently switch the effect-bearing snapshot first.
                mark_resource_for_deletion(selected),
            )


def _reject_applied_stacks_against_deleting_templates(
    candidate: Path,
    applied: frozenset[tuple[str, str, str]],
) -> None:
    """Reject an explicitly applied active Stack whose template was omitted and marked deleting."""

    resources = load_desired_resource_graph(candidate, validate=False)
    templates = {
        resource.name: resource
        for resource in resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate"
    }
    for key in sorted(applied):
        if key[1] != "Stack":
            continue
        stack = resources.get(key)
        if not isinstance(stack, StackResource) or not isinstance(stack.spec, DesiredStackSpec):
            continue
        if resource_deletion(stack) is not None:
            continue
        template = templates.get(stack.spec.templateRef.name)
        if template is not None and resource_deletion(template) is not None:
            raise OperationError(
                f"desired Stack {stack.name!r} references deleting StackTemplate {template.name!r}; "
                "apply the StackTemplate explicitly or wait for the referrer to be removed"
            )


def command_apply(args: argparse.Namespace) -> str | None:
    """Resolve explicit resources and atomically publish one desired snapshot."""

    _resource_name(args.environment, "environment name")
    partition = _resource_name(args.partition, "partition name") if args.partition is not None else None
    source_revision = (
        git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
        if args.source_revision is not None
        else None
    )
    if source_revision is not None:
        warn_if_source_revision_excludes_changes(source_revision)
    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, args.observed_ref)
    with tempfile.TemporaryDirectory(prefix="gitopsctr-apply-") as temporary_directory:
        temporary = Path(temporary_directory)
        source = temporary / "source"
        apply_source = temporary / "apply-source"
        current = temporary / "current"
        observed = temporary / "observed"
        candidate = temporary / "candidate"
        if source_revision is None:
            documents = _load_apply_documents(args.files)
            if not documents and partition is None:
                raise OperationError(
                    "apply produced zero documents; specify --partition for authoritative empty membership"
                )
            _materialize_apply_worktree(source)
        else:
            _validate_apply_input_selection(
                args.files,
                source_revision,
                operation="apply",
                revision_option="--source-revision",
            )
            materialize_revision(source_revision, source)
            documents = _load_apply_documents(
                args.files,
                source_revision=source_revision,
                source_root=source,
            )
        if source_revision is not None and not documents and partition is None:
            raise OperationError(
                "apply produced zero documents; specify --partition for authoritative empty membership"
            )
        _copy_apply_source_base(source, apply_source, args.environment)
        stack_template_document_digests: dict[str, str] = {}
        authored_units, authored_stacks = _write_apply_authored_documents(
            apply_source,
            args.environment,
            documents,
            stack_template_document_digests,
        )
        _validate_apply_source_revision(source_revision, authored_units, authored_stacks)
        current_revision = observed_tree(desired_ref, current)
        observed_revision = observed_tree(observed_ref, observed)
        persisted_promotion = (
            load_promotion_context(current, temporary)
            if any(item.document.get("kind") in {"Stack", "StackTemplate"} for item in documents)
            else None
        )
        projection_context = (
            capture_projection_context(apply_source, args.environment, persisted_promotion)
            if any(item.document.get("kind") in {"Stack", "StackTemplate"} for item in documents)
            else None
        )
        build_desired_candidate(
            args.environment,
            apply_source,
            source_revision,
            current,
            observed,
            observed_revision,
            candidate,
            dry=args.dry,
            verbose=getattr(args, "verbose", False),
            partition=partition,
            source_context_root=source if source_revision is not None else None,
            stack_template_document_digests=stack_template_document_digests,
            projection_context=projection_context,
        )
        applied = set(_explicit_applied_root_identities(documents, [*authored_units, *authored_stacks]))
        for item in documents:
            origin, document = item.origin, item.document
            if not _document_is_canonical_desired(document):
                continue
            kind = cast(str, document["kind"])
            metadata = cast(dict[str, Any], document["metadata"])
            name = cast(str, metadata["name"])
            if kind in {"Stack", "StackTemplate"}:
                # Canonical controller resources were normalized into the
                # authored candidate before projection. Their caller-supplied
                # identity, acquisition, and projection fields are ignored.
                continue
            resource = RESOURCE_CATALOG.parse_unit(document, profile="desired", expected_name=name)
            if getattr(resource.spec, "materialization", None) is not None:
                raise OperationError(
                    f"{origin}: canonical materialized Unit input is not supported because --file cannot "
                    "carry its persisted materialization tree; apply the authored Unit instead"
                )
            if resource_owner_reference(resource) is not None or resource_deletion(resource) is not None:
                raise OperationError(f"{origin}: apply accepts only non-deleting root resources")
            previous = (
                load_desired_resource_graph(current).get((resource.gvk.api_version, resource.gvk.kind, resource.name))
                if current_revision
                else None
            )
            if previous is not None:
                if resource_deletion(previous) is not None:
                    raise OperationError(f"desired resource {name!r} is deleting and cannot be applied")
                if partition is not None and previous.metadata.partition not in {None, partition}:
                    raise OperationError(
                        f"desired resource {name!r} belongs to partition {previous.metadata.partition!r}"
                    )
                metadata_value = previous.metadata.with_partition(partition, preserve_existing=partition is None)
            else:
                metadata_value = ResourceMetadata.root_from_provenance(
                    name, hashlib.sha256(canonical_json(document)).hexdigest(), partition=partition
                )
            resource = resource.with_metadata(metadata_value)
            target_directory = (
                "units"
                if isinstance(resource, UnitResource)
                else ("stack-templates" if resource.gvk.kind == "StackTemplate" else "stacks")
            )
            _write_desired_resource(candidate / target_directory / f"{name}.json", resource)
            applied.add((resource.gvk.api_version, resource.gvk.kind, resource.name))
        _copy_unrelated_desired_resources(current, candidate, frozenset(applied), partition)
        _prune_omitted_partition_resources(current, candidate, frozenset(applied), partition)
        _reject_applied_stacks_against_deleting_templates(candidate, frozenset(applied))
        load_desired_resource_graph(candidate)
        if current_revision is None and not directory_files(candidate):
            log_status("KEEP", f"{style_branch(desired_ref)} remains empty")
            return None
        if current_revision is None and change_gate(source, args.environment) == "pullRequest" and not args.dry:
            current_revision = _initialize_gated_desired_ref(source, args.environment, desired_ref, current)
        if current_revision is not None and directory_files(current) == directory_files(candidate):
            if not args.dry:
                acquisition: ControllerPinAcquisition | None = None
                try:
                    validate_desired_resource_transition(current, candidate)
                    lease_ref = effect_lease_ref(args.environment, desired_ref)
                    validate_effect_leases_preserved(
                        desired_ref,
                        current_revision,
                        candidate,
                        current,
                        lease_ref=lease_ref,
                    )
                    _ensure_stack_template_source_pins(args.environment, candidate)
                    _gc_superseded_stack_workload_pins(
                        args.environment,
                        candidate,
                        AcceptedDesiredTarget(desired_ref, current_revision),
                    )
                except BaseException:
                    if acquisition is not None:
                        log_status("KEEP", "retained StackTemplate source claims for existing desired state")
                    raise
            log_status("KEEP", f"{style_branch(desired_ref)} is already resolved")
            return current_revision
        candidate_id = candidate_identifier(
            "apply",
            args.environment,
            candidate,
            desired_ref,
            current_revision or "",
            {"partition": partition, "sourceRevision": source_revision},
        )
        candidate_ref = resolve_candidate_ref(
            REPOSITORY_ROOT, args.environment, "apply", candidate_id, args.candidate_ref
        )
        revision, outcome = publish_desired_change(
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            candidate_ref,
            f"Apply desired resources to {args.environment}",
            f"Apply desired resources to {args.environment}",
            f"Apply explicit resources{f' in partition `{partition}`' if partition else ''}.",
            args.dry,
            current if current_revision is not None else None,
            request_change=False,
            conflicting_refs=(observed_ref,),
        )
        if not args.dry:
            print(revision)
            write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)
        return revision


def command_create_stacktemplate(args: argparse.Namespace) -> None:
    _resource_name(args.name, "StackTemplate name")
    project = load_project_config(REPOSITORY_ROOT)
    source = Path(args.file)
    if not source.is_absolute():
        source = REPOSITORY_ROOT / source
    document = RESOURCE_CATALOG.load_document(source)
    template = RESOURCE_CATALOG.parse_stack_template(document, profile="authored", expected_name=args.name)
    target = _creation_target(
        REPOSITORY_ROOT.joinpath(*project.stack_templates_path.parts),
        args.name,
        suffix=project.write_format.suffix,
        force=args.or_update,
    )
    written = write_document(
        target,
        RESOURCE_CATALOG.serialize_stack_resource(template, profile="authored"),
        format=_document_format_for_path(target),
    )
    _print_created(written)


def command_create_stack(args: argparse.Namespace) -> None:
    _resource_name(args.name, "Stack name")
    project = load_project_config(REPOSITORY_ROOT)
    environment_root = project_environment_root(REPOSITORY_ROOT, args.environment)
    load_environment(REPOSITORY_ROOT, args.environment)
    parameters = _parse_stack_parameters(args.parameters)
    stack = StackResource(
        GVK(CORE_API_VERSION, "Stack"),
        ResourceMetadata(name=args.name),
        StackSpec(
            template=args.template,
            parameters=parameters,
            units=_parse_optional_units(args.units),
        ),
    )
    target = _creation_target(
        environment_root / "stacks",
        args.name,
        suffix=project.write_format.suffix,
        force=args.or_update,
    )
    written = write_document(
        target,
        RESOURCE_CATALOG.serialize_stack_resource(stack, profile="authored"),
        format=_document_format_for_path(target),
    )
    _print_created(written)


def _unit_storage_name(resource: UnitResource[Any]) -> str:
    owner = resource_owner_reference(resource)
    return (
        stack_generated_unit_name(owner.name, resource.name)
        if owner is not None and owner.kind == "Stack"
        else resource.name
    )


def _desired_resource_path(root: Path, resource: UnitResource[Any] | StackResource) -> Path:
    if isinstance(resource, UnitResource):
        return unit_document_path(root, _unit_storage_name(resource))
    directory = "stack-templates" if resource.gvk.kind == "StackTemplate" else "stacks"
    paths = document_candidates(root / directory, resource.name)
    if len(paths) != 1:
        raise OperationError(f"desired {resource.gvk.kind} {resource.name!r} is unavailable")
    return paths[0]


def _write_desired_resource(path: Path, resource: UnitResource[Any] | StackResource) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(resource, UnitResource):
        write_desired_candidate_unit(path, resource, REPOSITORY_ROOT)
    else:
        _write_desired_stack_resource(path, resource, REPOSITORY_ROOT)


def _owned_resource_closure(
    resources: Mapping[tuple[str, str, str], UnitResource[Any] | StackResource],
    target: UnitResource[Any] | StackResource,
) -> tuple[UnitResource[Any] | StackResource, ...]:
    selected = {(target.gvk.api_version, target.gvk.kind, target.name): target}
    changed = True
    while changed:
        changed = False
        for key, resource in resources.items():
            if key in selected:
                continue
            owner = resource_owner_reference(resource)
            if owner is None:
                continue
            if (owner.apiVersion, owner.kind, owner.name) in selected:
                parent = selected[(owner.apiVersion, owner.kind, owner.name)]
                if parent.metadata.uid != owner.uid:
                    continue
                selected[key] = resource
                changed = True
    return tuple(selected.values())


def _command_delete_state_resource(args: argparse.Namespace) -> bool:
    if not args.uid:
        raise OperationError("state deletion requires --uid")
    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, None)
    current_revision = fetch_ref(desired_ref)
    if current_revision is None:
        raise OperationError(f"desired ref {desired_ref!r} has no state")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        current = temporary / "current"
        candidate = temporary / "candidate"
        materialize_revision(current_revision, current)
        resources = load_desired_resource_graph(current)
        matches = [
            resource
            for key, resource in resources.items()
            if (key[2] == args.name if args.kind == "Unit" else resource.name == args.name)
            and (
                (args.kind == "Unit" and isinstance(resource, UnitResource))
                or (args.kind in {"Stack", "StackTemplate"} and resource.gvk.kind == args.kind)
            )
        ]
        if len(matches) != 1:
            raise OperationError(f"desired {args.kind} {args.name!r} is not present")
        target = matches[0]
        if target.metadata.uid != args.uid:
            raise OperationError(f"stale desired {args.kind} UID fence for {args.name!r}")
        if resource_owner_reference(target) is not None:
            raise OperationError(f"desired {args.kind} {args.name!r} is owned; delete its owner instead")
        if resource_deletion(target) is not None:
            deletion = resource_deletion(target)
            if deletion is not None and deletion.resourceDigest != resource_content_digest(target):
                raise OperationError(f"desired {args.kind} {args.name!r} changed after deletion started")
            return False
        shutil.copytree(current, candidate)
        for resource in _owned_resource_closure(resources, target):
            marked = mark_resource_for_deletion(resource)
            _write_desired_resource(_desired_resource_path(candidate, resource), marked)
        load_desired_resource_graph(candidate)
        candidate_id = candidate_identifier(
            "delete",
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            {"kind": args.kind, "name": args.name, "uid": args.uid},
        )
        candidate_ref = resolve_candidate_ref(
            REPOSITORY_ROOT, args.environment, "delete", candidate_id, args.candidate_ref
        )
        if candidate_ref_conflicts(candidate_ref, desired_ref, observed_ref):
            raise OperationError("deletion candidate ref conflicts with deployment state")
        revision, outcome = publish_desired_change(
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            candidate_ref,
            f"Mark {args.kind} {args.name} for deletion",
            f"Delete {args.kind} {args.name}",
            f"Mark `{args.kind} {args.name}` and its UID-owned resources for deletion.",
            args.dry,
            current,
            request_change=False,
            conflicting_refs=(observed_ref,),
        )
        if args.dry:
            return False
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)
        return True


def command_delete_resource(args: argparse.Namespace) -> None:
    _command_delete_state_resource(args)


def command_create_project(args: argparse.Namespace) -> None:
    project_document = {
        "$schema": resource_schema_url(CORE_API_VERSION, "Project"),
        "apiVersion": CORE_API_VERSION,
        "kind": "Project",
        "metadata": {"name": args.name},
        "spec": {
            "writeFormat": args.write_format,
            "environmentsPath": args.environments_path,
            "environmentDefaults": {
                "refs": {
                    "desired": args.desired_ref_template,
                    "observed": args.observed_ref_template,
                    "candidate": args.candidate_ref_template,
                }
            },
            "effectLease": {
                "store": {
                    "branch": {
                        "ref": "gitopsctr/leases",
                    }
                }
            },
        },
    }
    try:
        validate_project_document(project_document, REPOSITORY_ROOT / "gitopsctr.yaml")
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc

    candidates = [REPOSITORY_ROOT / name for name in PROJECT_CONFIG_NAMES if (REPOSITORY_ROOT / name).is_file()]
    if len(candidates) > 1:
        raise OperationError("multiple Project configuration files exist: " + ", ".join(map(str, candidates)))
    if candidates and not args.force:
        raise OperationError(f"resource already exists: {candidates[0]}")
    target = candidates[0] if candidates else REPOSITORY_ROOT / "gitopsctr.yaml"
    written = write_document(target, project_document, format=DocumentFormat.YAML)
    _print_created(written)


def command_create_environment(args: argparse.Namespace) -> None:
    _resource_name(args.name, "environment name")
    try:
        project = load_project_config(REPOSITORY_ROOT)
        environment_root = project_environment_root(REPOSITORY_ROOT, args.name)
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc
    target = _creation_target(
        environment_root,
        "environment",
        suffix=project.write_format.suffix,
        force=args.force,
    )
    environment = {"name": args.name, "changeGate": args.change_gate}
    validate_document(CORE_CONTRACTS["environment"], environment, f"environment specification {args.name}")
    written = write_document(
        target, serialize_environment_document(environment), format=_document_format_for_path(target)
    )
    _print_created(written)


def command_create_unit(args: argparse.Namespace) -> None:
    if not args.name or not args.driver:
        raise OperationError("source Unit creation requires --name and --driver")
    _resource_name(args.name, "unit name")
    safe_source_path(args.source_path, f"{args.name} source path")
    try:
        project = load_project_config(REPOSITORY_ROOT)
        environment_root = project_environment_root(REPOSITORY_ROOT, args.environment)
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc
    load_environment(REPOSITORY_ROOT, args.environment)

    driver = UNIT_DRIVERS[args.driver]
    scaffold = driver.scaffold_unit_spec(args.name, args.source_path)
    if scaffold is None:
        raise OperationError(
            f"unit driver {args.driver!r} does not support scaffolding; create the document from its authored schema"
        )
    model = RESOURCE_CATALOG.parse_contract(
        driver.unit_contract,
        scaffold,
        f"authored {args.driver} unit {args.name}",
    )
    unit = UnitResource(
        GVK(driver.api_version, driver.kind),
        ResourceMetadata(name=args.name),
        driver,
        model,
    )
    require_unit_specification(unit, args.name)
    target = _creation_target(
        environment_root / "units",
        args.name,
        suffix=project.write_format.suffix,
        force=args.force or getattr(args, "or_update", False),
    )
    written = write_document(
        target,
        serialize_unit_document(unit, profile="authored"),
        format=_document_format_for_path(target),
    )
    _print_created(written)


@dataclass(frozen=True)
class ValidationIssue:
    target: str
    detail: str


class ValidationCollector:
    def __init__(self, fail_fast: bool) -> None:
        self.fail_fast = fail_fast
        self.issues: list[ValidationIssue] = []
        self.documents: set[Path] = set()
        self.environments: set[str] = set()

    def invalid(self, target: Path | str, exc: Exception | str) -> None:
        detail = str(exc)
        label = str(target)
        if self.fail_fast:
            raise OperationError(f"{label}: {detail}")
        self.issues.append(ValidationIssue(label, detail))

    def valid_document(self, path: Path) -> None:
        self.documents.add(path.resolve())


def _validate_project_file(path: Path, collector: ValidationCollector) -> None:
    try:
        validate_project_document(load_document(path), path)
        collector.valid_document(path)
    except (DocumentFormatError, OSError) as exc:
        collector.invalid(path, exc)


def _validate_environment_file(path: Path, collector: ValidationCollector) -> None:
    try:
        environment = normalize_environment_document(load_json(path), path.parent.name)
        validate_document(CORE_CONTRACTS["environment"], environment, f"environment specification {path.parent.name}")
        collector.valid_document(path)
    except (DocumentFormatError, OperationError) as exc:
        collector.invalid(path, exc)


def _validate_unit_file(path: Path, collector: ValidationCollector) -> UnitResource[Any] | None:
    try:
        unit = parse_authored_unit_document(load_json(path), path.stem)
        require_unit_specification(unit, path.stem)
        collector.valid_document(path)
        return unit
    except (DocumentFormatError, OperationError) as exc:
        collector.invalid(path, exc)
        return None


def _validate_authored_file(path: Path, collector: ValidationCollector) -> None:
    if not path.is_file():
        collector.invalid(path, "file does not exist")
        return
    try:
        document = load_document(path)
    except DocumentFormatError as exc:
        collector.invalid(path, exc)
        return
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    if api_version == CORE_API_VERSION and kind == "Project":
        if path.name not in PROJECT_CONFIG_NAMES:
            collector.invalid(path, f"Project resource must use one of: {', '.join(PROJECT_CONFIG_NAMES)}")
        elif len([path.parent / name for name in PROJECT_CONFIG_NAMES if (path.parent / name).is_file()]) != 1:
            collector.invalid(path.parent, "multiple Project configuration files exist")
        else:
            _validate_project_file(path, collector)
    elif api_version == CORE_API_VERSION and kind == "Environment":
        if len(document_candidates(path.parent, "environment")) != 1:
            collector.invalid(path.parent, "multiple document formats exist for environment")
        else:
            _validate_environment_file(path, collector)
    elif api_version == UNIT_API_VERSION:
        if len(document_candidates(path.parent, path.stem)) != 1:
            collector.invalid(path.parent / path.stem, f"multiple document formats exist for unit {path.stem}")
        else:
            _validate_unit_file(path, collector)
    else:
        collector.invalid(path, f"unsupported authored resource {api_version}/{kind}")


def _validate_environment(environment_name: str, collector: ValidationCollector) -> None:
    if environment_name in collector.environments:
        return
    collector.environments.add(environment_name)
    try:
        _resource_name(environment_name, "environment name")
        environment_root = project_environment_root(REPOSITORY_ROOT, environment_name)
    except (DocumentFormatError, OperationError) as exc:
        collector.invalid(environment_name, exc)
        return

    environment_paths = document_candidates(environment_root, "environment")
    if len(environment_paths) != 1:
        collector.invalid(
            environment_root,
            f"expected exactly one environment document for {environment_name}; found {len(environment_paths)}",
        )
    else:
        _validate_environment_file(environment_paths[0], collector)

    units_root = environment_root / "units"
    stems = sorted({path.stem for path in units_root.glob("*") if path.suffix in {".json", ".yaml", ".yml"}})
    if not stems:
        return

    specifications: dict[str, UnitResource[Any]] = {}
    for stem in stems:
        paths = document_candidates(units_root, stem)
        if len(paths) != 1:
            collector.invalid(units_root / stem, f"multiple document formats exist for unit {stem}")
            continue
        specification = _validate_unit_file(paths[0], collector)
        if specification is not None:
            specifications[stem] = specification

    for consumer, specification in specifications.items():
        specification_document = specification.driver.unit_contract.dump(specification.spec)
        try:
            references = observation_reference_units(specification_document)
        except OperationError as exc:
            collector.invalid(units_root / consumer, exc)
            continue
        for reference in references:
            producer = reference
            if producer in specifications:
                producer_unit = specifications[producer]
                reconciles = producer_unit.driver.authored_reconciliation_required(producer_unit.spec)
                if not reconciles:
                    collector.invalid(
                        units_root / consumer,
                        f"{consumer} cannot observe materialization-only unit {producer!r}",
                    )
        try:
            artifacts = artifact_references(specification_document, "/spec")
        except OperationError as exc:
            collector.invalid(units_root / consumer, exc)
            continue
        for reference in artifacts:
            producer = reference.unit
            artifact_name = reference.name
            producer_specification = specifications.get(producer)
            if producer_specification is None:
                continue
            driver = producer_specification.driver
            if driver is not None and artifact_name not in driver.artifact_outputs:
                collector.invalid(
                    units_root / consumer,
                    f"{consumer} references unknown artifact {producer}/{artifact_name}",
                )
            elif driver is not None and driver.artifact_outputs[artifact_name].gvk != reference.gvk:
                collector.invalid(
                    units_root / consumer,
                    f"{consumer} expects artifact {producer}/{artifact_name} to be {reference.gvk}; "
                    f"producer declares {driver.artifact_outputs[artifact_name].gvk}",
                )


def command_validate(args: argparse.Namespace) -> None:
    collector = ValidationCollector(args.fail_fast)
    file_targets = list(dict.fromkeys(Path(value) for value in args.files))
    environment_targets = list(dict.fromkeys(args.environment or []))

    if file_targets or environment_targets:
        for path in file_targets:
            target = path if path.is_absolute() else REPOSITORY_ROOT / path
            _validate_authored_file(target.resolve(), collector)
        for environment_name in environment_targets:
            _validate_environment(environment_name, collector)
    else:
        try:
            project = load_project_config(REPOSITORY_ROOT)
            project_path = next(
                REPOSITORY_ROOT / name for name in PROJECT_CONFIG_NAMES if (REPOSITORY_ROOT / name).is_file()
            )
            collector.valid_document(project_path)
        except (DocumentFormatError, StopIteration) as exc:
            collector.invalid(REPOSITORY_ROOT / "gitopsctr.yaml", exc)
            project = None
        if project is not None:
            environments_root = REPOSITORY_ROOT.joinpath(*project.environments_path.parts)
            for path in sorted(environments_root.iterdir()) if environments_root.is_dir() else []:
                if path.is_dir():
                    _validate_environment(path.name, collector)

    if collector.issues:
        for issue in collector.issues:
            log_status("INVALID", f"{issue.target}: {issue.detail}")
        raise OperationError(
            f"validation failed with {len(collector.issues)} error{'s' if len(collector.issues) != 1 else ''}"
        )
    log_status(
        "VALID",
        f"{len(collector.documents)} document{'s' if len(collector.documents) != 1 else ''}"
        f" across {len(collector.environments)} environment{'s' if len(collector.environments) != 1 else ''}",
    )


class GroupedHelpFormatter(argparse.HelpFormatter):
    """Render the root command list in functional groups."""

    COMMAND_GROUPS = (
        ("Project", ("create", "apply", "delete", "validate")),
        ("Schemas", ("schemas",)),
        (
            "Deployment",
            (
                "promote",
                "rollback",
                "recover-effect-lease",
                "resolve-desired",
            ),
        ),
        (
            "Recovery",
            (
                "recover-opaque-unit",
                "resolve-opaque-unit",
            ),
        ),
        (
            "Inspection",
            ("get", "status", "verify", "dependencies"),
        ),
        ("Reconciliation", ("reconcile", "converge")),
        ("Git data", ("read-tree", "publish-tree")),
    )

    def _format_action(self, action: argparse.Action) -> str:
        if not isinstance(action, argparse._SubParsersAction) or action.dest != "command":
            return super()._format_action(action)

        choices = {choice.dest: choice for choice in action._choices_actions}
        sections: list[str] = []
        for title, command_names in self.COMMAND_GROUPS:
            entries = [choices[name] for name in command_names if name in choices]
            if not entries:
                continue
            if sections:
                sections.append("\n")
            sections.append(f"{title}:\n")
            self._indent()
            try:
                format_action = super()._format_action
                sections.extend(format_action(entry) for entry in entries)
            finally:
                self._dedent()
        return "".join(sections)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=GroupedHelpFormatter,
    )
    parser.add_argument(
        "--repository",
        help="Git working tree; defaults to the repository containing the current directory",
    )
    commands = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="COMMAND",
        required=True,
    )

    create = commands.add_parser("create", help="scaffold a source resource")
    create_commands = create.add_subparsers(dest="create_command", required=True)
    create_project = create_commands.add_parser("project", help="create the repository Project resource")
    create_project.add_argument("--name", required=True, help="DNS-1123 project name")
    create_project.add_argument("--write-format", choices=("yaml", "json"), default="yaml")
    create_project.add_argument(
        "--environments-path",
        default="deployment/environments",
        help="repository-relative authored environments directory",
    )
    create_project.add_argument(
        "--desired-ref-template",
        default=DEFAULT_DESIRED_REF_TEMPLATE,
        help="default desired-state ref template containing {environment}",
    )
    create_project.add_argument(
        "--observed-ref-template",
        default=DEFAULT_OBSERVED_REF_TEMPLATE,
        help="default observed-state ref template containing {environment}",
    )
    create_project.add_argument(
        "--candidate-ref-template",
        default=DEFAULT_CANDIDATE_REF_TEMPLATE,
        help="default candidate ref template containing {environment}; supports {id} and {operation}",
    )
    create_project.add_argument("--force", action="store_true", help="replace an existing Project resource")
    create_project.set_defaults(handler=command_create_project)

    create_environment = create_commands.add_parser("environment", help="create an authored Environment resource")
    create_environment.add_argument("--name", required=True)
    create_environment.add_argument("--change-gate", choices=("none", "pullRequest"), default="none")
    create_environment.add_argument("--force", action="store_true", help="replace an existing Environment resource")
    create_environment.set_defaults(handler=command_create_environment)

    create_unit = create_commands.add_parser("unit", help="create a driver-specific authored Unit resource")
    create_unit.add_argument("--environment", required=True)
    create_unit.add_argument("--name")
    create_unit.add_argument("--driver", choices=tuple(sorted(UNIT_DRIVERS)))
    create_unit.add_argument(
        "--source-path",
        default=".",
        help="path relative to the root of the selected source revision",
    )
    create_unit.add_argument("--force", action="store_true", help="replace an existing Unit resource")
    create_unit.set_defaults(handler=command_create_unit)

    create_template = create_commands.add_parser(
        "stacktemplate", help="create a source-authored StackTemplate from a document"
    )
    create_template.add_argument("--name", required=True)
    create_template.add_argument("--file", required=True, help="authored StackTemplate document")
    create_template.add_argument("--or-update", action="store_true", help="replace an existing source document")
    create_template.set_defaults(handler=command_create_stacktemplate)

    create_stack = create_commands.add_parser("stack", help="create a source Stack")
    create_stack.add_argument("--environment", required=True)
    create_stack.add_argument("--name", required=True)
    create_stack.add_argument("--template", required=True)
    create_stack.add_argument("--units", help="comma-separated Unit template names")
    create_stack.add_argument("--parameters", default="{}", help="Stack parameters as a JSON object")
    create_stack.add_argument("--or-update", action="store_true", help="update an existing resource")
    create_stack.set_defaults(handler=command_create_stack)

    apply = commands.add_parser("apply", help="resolve explicit resources into desired state")
    apply.add_argument("--environment", required=True)
    apply.add_argument("-f", "--file", dest="files", action="append", required=True)
    apply.add_argument("--partition", help="authoritative management partition")
    apply.add_argument(
        "--source-revision",
        help="required commit when an input uses repository-backed source; otherwise apply reads the worktree",
    )
    apply.add_argument("--desired-ref")
    apply.add_argument("--observed-ref")
    apply.add_argument("--candidate-ref")
    apply.add_argument("--dry", action="store_true")
    apply.add_argument("--verbose", action="store_true")
    apply.set_defaults(handler=command_apply)

    delete = commands.add_parser("delete", help="mark a desired resource for deletion")
    delete_commands = delete.add_subparsers(dest="delete_kind", required=True)
    for delete_name, delete_kind in (("stack", "Stack"), ("unit", "Unit"), ("stacktemplate", "StackTemplate")):
        delete_parser = delete_commands.add_parser(delete_name, help=f"delete a {delete_kind}")
        delete_parser.add_argument("--environment", required=True)
        delete_parser.add_argument("--name", required=True)
        delete_parser.add_argument("--uid", required=True, help="expected desired resource UID fence")
        delete_parser.add_argument("--desired-ref")
        delete_parser.add_argument("--candidate-ref")
        delete_parser.add_argument("--dry", action="store_true")
        delete_parser.set_defaults(handler=command_delete_resource, kind=delete_kind)

    validate = commands.add_parser("validate", help="validate Project, Environment, and Unit resources")
    validate.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="authored resource file relative to the repository root; defaults to the whole Project",
    )
    validate.add_argument(
        "--environment",
        action="append",
        help="environment to validate; repeat to validate multiple environments",
    )
    validate.add_argument("--fail-fast", action="store_true", help="stop after the first validation error")
    validate.set_defaults(handler=command_validate)

    schemas = commands.add_parser("schemas", help="show or export public schemas")
    schema_commands = schemas.add_subparsers(dest="schema_command", required=True)
    schemas_show = schema_commands.add_parser("show", help="print one core or driver schema")
    schemas_show.add_argument("driver", help="driver name or 'core'")
    schemas_show.add_argument("kind", help="document kind")
    schemas_show.set_defaults(handler=command_schemas_show)
    schemas_export = schema_commands.add_parser("export", help="write the deterministic schema catalog")
    schemas_export.add_argument("directory")
    schemas_export.add_argument("--check", action="store_true", help="fail if generated files differ")
    schemas_export.set_defaults(handler=command_schemas_export)

    read = commands.add_parser("read-tree", help="materialize a Git data ref")
    read.add_argument("--ref", required=True)
    read.add_argument("--revision")
    read.add_argument("--require-ancestor", action="store_true")
    read.add_argument("--allow-missing", action="store_true")
    read.add_argument("--output", required=True)
    read.set_defaults(handler=command_read_tree)

    publish = commands.add_parser("publish-tree", help="commit and push a Git data tree")
    publish.add_argument("--ref", required=True)
    publish.add_argument("--directory", required=True)
    publish.add_argument("--parent")
    publish.add_argument(
        "--expected-publication-head",
        type=parse_expected_publication_head,
        default=argparse.SUPPRESS,
        metavar="REVISION|absent",
        help="caller-authorized current publication head; defaults to --parent; use 'absent' for no head",
    )
    publish.add_argument("--message", required=True)
    publish.set_defaults(handler=command_publish_tree)

    promote = commands.add_parser(
        "promote",
        help="promote reviewed desired state",
    )
    promote.add_argument("--from-environment", required=True)
    promote.add_argument("--to-environment", required=True)
    promote.add_argument("-f", "--file", dest="files", action="append", required=True)
    promote.add_argument("--partition")
    promote.add_argument(
        "--source-desired-revision",
        help="exact source desired commit; defaults to the source desired ref head",
    )
    promote.add_argument(
        "--specification-revision",
        help="reviewed main commit; defaults to HEAD",
    )
    promote.add_argument(
        "--candidate-ref",
        help="new Git ref for the candidate; defaults to a deterministic promotion ref",
    )
    promote.add_argument("--dry", action="store_true")
    promote.set_defaults(handler=command_promote)

    rollback = commands.add_parser(
        "rollback",
        help="publish a forward-only rollback",
    )
    rollback.add_argument("--environment", required=True)
    rollback.add_argument("--to-desired-revision", required=True)
    rollback.add_argument(
        "--unit",
        action="append",
        help="qualified Unit name to roll back; repeat for multiple units (defaults to the full tree)",
    )
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--desired-ref", help="override the environment's desired ref")
    rollback.add_argument("--observed-ref", help="override the environment's observed ref")
    rollback.add_argument(
        "--candidate-ref",
        help="exact candidate ref override when the environment uses a pull-request change gate",
    )
    rollback.add_argument("--dry", action="store_true")
    rollback.set_defaults(handler=command_rollback)

    recover_effect_lease = commands.add_parser(
        "recover-effect-lease",
        help="recover an abandoned effect lease after verifying its effect stopped",
    )
    recover_effect_lease.add_argument("--environment", required=True)
    recover_effect_lease.add_argument("--unit", required=True, help="qualified Unit name")
    recover_effect_lease.add_argument("--uid", required=True, help="expected leased Unit UID fence")
    recover_effect_lease.add_argument("--token", required=True, help="exact persisted lease token")
    recover_effect_lease.add_argument(
        "--confirm-stopped",
        action="store_true",
        required=True,
        help="confirm that the external effect is no longer running",
    )
    recover_effect_lease.add_argument("--desired-ref", help="override the environment's desired ref")
    recover_effect_lease.set_defaults(handler=command_recover_effect_lease)

    recover_opaque = commands.add_parser(
        "recover-opaque-unit",
        help="recover a UID-fenced opaque cleanup root using installed driver code",
    )
    recover_opaque.add_argument("--environment", required=True)
    recover_opaque.add_argument("--unit", required=True)
    recover_opaque.add_argument("--uid", required=True, help="exact opaque cleanup UID fence")
    recover_opaque.add_argument(
        "--source-revision",
        required=True,
        help="authoritative full source commit used only to validate recovery eligibility",
    )
    recover_opaque.add_argument("--desired-ref", help="override the environment's desired ref")
    recover_opaque.add_argument(
        "--candidate-ref",
        help="exact candidate ref override when the environment uses a pull-request change gate",
    )
    recover_opaque.add_argument("--dry", action="store_true")
    recover_opaque.set_defaults(handler=command_recover_opaque_unit)

    resolve_opaque = commands.add_parser(
        "resolve-opaque-unit",
        help="resolve an unparseable opaque cleanup root after external cleanup",
    )
    resolve_opaque.add_argument("--environment", required=True)
    resolve_opaque.add_argument("--unit", required=True)
    resolve_opaque.add_argument("--uid", required=True, help="exact opaque cleanup UID fence")
    resolve_opaque.add_argument(
        "--deletion-generation",
        required=True,
        type=int,
        help="expected opaque cleanup deletion generation fence",
    )
    resolve_opaque.add_argument(
        "--reason",
        required=True,
        help="bounded operator reason for the confirmed external cleanup",
    )
    resolve_opaque.add_argument(
        "--confirm-external-cleanup",
        action="store_true",
        help="confirm that the external resource was cleaned up outside gitopsctr",
    )
    resolve_opaque.add_argument("--desired-ref", help="override the environment's desired ref")
    resolve_opaque.add_argument(
        "--candidate-ref",
        help="exact candidate ref override when the environment uses a pull-request change gate",
    )
    resolve_opaque.add_argument("--dry", action="store_true")
    resolve_opaque.set_defaults(handler=command_resolve_opaque_unit)

    resolve = commands.add_parser(
        "resolve-desired",
        help="resolve a commit from desired history",
    )
    resolve.add_argument("--desired-ref", required=True)
    resolve.add_argument("--desired-revision")
    resolve.set_defaults(handler=command_resolve_desired)

    status = commands.add_parser(
        "status",
        help="show deployment status",
    )
    status.add_argument("--environment", help="environment to inspect; omit for all environments")
    status.add_argument("--unit", help="limit detailed status to one qualified Unit name")
    status.add_argument("--desired-ref", help="override the environment's desired ref")
    status.add_argument(
        "--desired-revision",
        help="exact desired commit; defaults to the current desired ref head",
    )
    status.add_argument("--observed-ref", help="override the environment's observed ref")
    status.add_argument("--verbose", action="store_true")
    status.set_defaults(handler=command_status)

    get = commands.add_parser("get", help="inspect persisted resources")
    get.add_argument("selector", choices=inspectable_selectors(), help="singular or plural resource selector")
    get.add_argument("name", nargs="?", help="exact qualified resource name; omit to list the selected family")
    for identity_filter in identity_filter_options():
        assert identity_filter.filter_option is not None
        get.add_argument(
            identity_filter.filter_option,
            dest=identity_filter.option_destination,
            help=f"filter by the {identity_filter.name} identity segment",
        )
    get_scope = get.add_mutually_exclusive_group()
    get_scope.add_argument("--environment", help="environment namespace to inspect")
    get_scope.add_argument(
        "-A",
        "--all-environments",
        action="store_true",
        help="inspect every authored environment",
    )
    get.add_argument("--desired-ref", help="override the environment's desired ref")
    get.add_argument("--desired-revision", help="exact desired commit; defaults to the desired ref head")
    get.add_argument("--observed-ref", help="override the environment's observed ref")
    get.add_argument("--observed-revision", help="exact observed commit; defaults to the observed ref head")
    get.add_argument("-o", "--output", choices=("table", "wide", "yaml", "json"), default="table")
    get.add_argument(
        "--as-list",
        action="store_true",
        help="always wrap YAML/JSON output in an inspection ResourceList",
    )
    get_artifacts = get.add_mutually_exclusive_group()
    get_artifacts.add_argument("--artifact", help="show one validated Artifact described by the Receipt")
    get_artifacts.add_argument(
        "--artifacts",
        action="store_true",
        help="show all validated Artifacts described by the Receipt",
    )
    get.set_defaults(handler=command_get)

    verify = commands.add_parser(
        "verify",
        help="check desired units for drift",
    )
    verify.add_argument("--environment", required=True)
    verify.add_argument(
        "--unit",
        action="append",
        help="qualified Unit name to verify; repeat for multiple units (defaults to all desired Units)",
    )
    verify.set_defaults(handler=command_verify)

    reconcile = commands.add_parser(
        "reconcile",
        help="reconcile one deployment unit",
    )
    reconcile.add_argument(
        "--unit",
        required=True,
        help="qualified Unit name",
    )
    reconcile.add_argument("--desired-ref", help="override the environment's desired ref")
    reconcile.add_argument(
        "--desired-revision",
        help="exact desired commit; defaults to the current desired ref head",
    )
    reconcile.add_argument("--observed-ref", help="override the environment's observed ref")
    reconcile.add_argument("--plan", action="store_true")
    reconcile.add_argument(
        "--report",
        help="directory where the selected driver may write its report artifacts",
    )
    reconcile.add_argument("--environment", required=True)
    reconcile.add_argument(
        "--reapply",
        action="store_true",
        help="run the driver even when a clean receipt already exists",
    )
    reconcile.add_argument(
        "--verbose",
        action="store_true",
        help="show desired-state resolution and reconciliation internals",
    )
    reconcile.set_defaults(handler=command_reconcile)

    dependencies = commands.add_parser(
        "dependencies",
        help="show unit dependencies",
    )
    dependencies.add_argument("--environment", required=True)
    dependencies.add_argument("--source-revision", default="HEAD")
    dependencies.add_argument(
        "--unit",
        action="append",
        required=True,
        help="target qualified Unit name; repeat to show multiple dependency trees",
    )
    dependency_format = dependencies.add_mutually_exclusive_group()
    dependency_format.add_argument(
        "--list",
        action="store_true",
        help="print the dependency-first unit order, one unit per line",
    )
    dependency_format.add_argument(
        "--json",
        action="store_true",
        help="emit the scoped dependency graph as JSON",
    )
    dependencies.add_argument(
        "--depth",
        type=int,
        help="maximum dependency depth; zero prints only the selected target",
    )
    dependencies.set_defaults(handler=command_dependencies)

    converge = commands.add_parser(
        "converge",
        help="reconcile units and dependencies until clean",
    )
    converge.add_argument("--environment", required=True)
    converge.add_argument("--source-revision")
    converge_selection = converge.add_mutually_exclusive_group()
    converge_selection.add_argument(
        "--unit",
        action="append",
        help="target qualified Unit name; repeat for multiple targets (defaults to all Units)",
    )
    converge_selection.add_argument(
        "--partition",
        help="select every Unit in the partition, including owned descendants",
    )
    converge.add_argument("-f", "--file", dest="files", action="append", default=[])
    converge.add_argument("--desired-ref", help="override the environment's desired ref")
    converge.add_argument("--observed-ref", help="override the environment's observed ref")
    converge.add_argument("--candidate-ref")
    converge.add_argument(
        "--max-steps",
        type=int,
        help="maximum driver runs; defaults to twice the scoped unit count",
    )
    converge.add_argument(
        "--fail-on-repeat",
        action="store_true",
        help="fail if a unit reconciled by this invocation becomes ready again",
    )
    converge.add_argument(
        "--yes",
        action="store_true",
        help="approve every reconciliation without prompting",
    )
    converge.add_argument(
        "--verbose",
        action="store_true",
        help="show every relevant source commit, file, and specification field",
    )
    converge.set_defaults(handler=command_converge)

    return parser


def main() -> int:
    global REPOSITORY_ROOT
    args = build_parser().parse_args()
    try:
        if args.command != "schemas":
            REPOSITORY_ROOT = resolve_repository_root(args.repository)
        args.handler(args)
    except SourceRevisionUnavailableError as exc:
        log_status(
            "ERROR",
            f"{style_unit(exc.unit_name)}: desired source {describe_revision(exc.revision)} "
            "is unavailable under project policy",
        )
        print("      Re-run apply with an available source revision.", file=sys.stderr, flush=True)
        return 1
    except (DriverError, OperationError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or "").strip() or str(exc)
        else:
            detail = str(exc)
        log_status("FAILED", f"gitopsctr: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
