"""Resolve deployment state and run registered drivers.

Desired and observed documents are the contract. This module is the main
controller API used by local callers and the command-line adapter.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TextIO, TypedDict, cast

import yaml

from gitopsctr import operational
from gitopsctr.api import GVK, ApiError
from gitopsctr.artifacts import require_artifact_api
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    ArtifactDescriptor,
    ArtifactImport,
    AuthoredSource,
    DeletionMetadata,
    DesiredOwnerReference,
    DesiredSource,
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    GitSourceRequest,
    MaterializationDocument,
    ReceiptDesired,
    ResolvedArtifactImport,
    ResolvedGitSource,
    ResolvedStackTemplateSource,
    StackSpec,
    StackTemplateFromGit,
    StackTemplateFromPromotion,
    StackTemplateFromResource,
    StackTemplateReference,
    StackTemplateResource,
    StackTemplateSpec,
    StrictModel,
    scope_stack_template_resources,
    stack_generated_unit_name,
    with_schema,
)
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
    project_environment_root,
    validate_project_document,
    write_document,
)
from gitopsctr.inspection import command_get as inspect_resources
from gitopsctr.inspection import inspectable_selectors
from gitopsctr.registry import (
    API_KINDS,
    DRIVER_GVKS,
    DRIVER_NAMES_BY_GVK,
    DRIVER_VERSIONS,
    MATERIALIZATION_DRIVERS,
    PLANNING_DRIVERS,
    RECONCILIATION_DRIVERS,
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
    validate_desired_resource_graph,
)
from gitopsctr.schemas import encoded_schema, export_schemas, resource_schema_url, show_schema
from gitopsctr.state import ControllerPin, GatedCandidate, GitStateStore
from gitopsctr.templates import (
    ArtifactReference as ArtifactReferenceExpression,
)
from gitopsctr.templates import (
    ArtifactReferenceTarget,
    PromotionReference,
    ReceiptReference,
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
OBSERVED_TEARDOWN_EVIDENCE_PATH = PurePosixPath(".gitopsctr/teardowns/units")
EFFECT_LEASE_TTL_SECONDS = 300


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
class UnitChangeExplanation:
    previous_desired_revision: str
    previous_source_revision: str | None
    current_source_revision: str | None
    causes: tuple[str, ...]
    commits: tuple[str, ...]
    files: tuple[str, ...]
    specification_paths: tuple[str, ...]


class RevisionSnapshot(TypedDict):
    ref: str
    revision: str | None


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


def publish_tree(ref: str, directory: Path, parent: str | None, message: str) -> str:
    return state_store().publish(ref, directory, parent, message).revision


def verify_gated_candidate(candidate_revision: str | None, target_revision: str | None) -> GatedCandidate:
    """Verify a change-gated candidate against the exact target head used to build it."""

    return state_store().verify_gated_candidate(candidate_revision, target_revision)


def command_publish_tree(args: argparse.Namespace) -> None:
    commit = publish_tree(args.ref, Path(args.directory), args.parent, args.message)
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


def serialize_promotion_document(document: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.serialize_promotion(cast(JsonObject, document)))


def serialize_unit_document(
    unit: UnitResource[Any], *, profile: Literal["authored", "desired"] = "desired"
) -> dict[str, Any]:
    return cast(dict[str, Any], RESOURCE_CATALOG.serialize_unit(unit, profile=profile))


def load_receipt(path: Path, expected_unit: str | None = None) -> ReceiptResource[Any]:
    return RESOURCE_CATALOG.load_receipt(path, expected_unit)


resource_documents_enabled = RESOURCE_CATALOG.resource_documents_enabled
unit_document_path = RESOURCE_CATALOG.unit_document_path


def load_authored_unit(path: Path, expected_name: str | None = None) -> UnitResource[Any]:
    return RESOURCE_CATALOG.load_unit(path, expected_name, profile="authored")


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


def load_desired_resource_graph(
    root: Path, *, validate: bool = True
) -> dict[tuple[str, str, str], UnitResource[Any] | StackResource]:
    """Load and validate every desired resource in one desired ref before effects."""

    resources: dict[tuple[str, str, str], UnitResource[Any] | StackResource] = {}
    for unit_name, path in _current_desired_unit_paths(root).items():
        unit = load_desired_unit(path, unit_name)
        key = (unit.gvk.api_version, unit.gvk.kind, unit.name)
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
                resources.get(
                    (
                        CORE_API_VERSION,
                        "StackTemplate",
                        stack_resource.spec.template
                        if isinstance(stack_resource.spec.template, str)
                        else stack_resource.spec.template.name,
                    )
                )
                if isinstance(stack_resource, StackResource)
                and isinstance(stack_resource.spec, (StackSpec, DesiredStackSpec))
                and (
                    isinstance(stack_resource.spec.template, str)
                    or isinstance(stack_resource.spec.template.source, StackTemplateFromResource)
                )
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
                    (resource.apiVersion, resource.kind, resource.name)
                    for resource in scope_stack_template_resources(
                        stack_name,
                        template_resource.spec.expand(stack_resource.spec.parameters),
                    )
                    if (resource.apiVersion, resource.kind, resource.name) not in resources
                }
                tombstones = load_resource_incarnation_tombstones(root)
                if (
                    missing_resources
                    and any(name == unit_name for _api_version, _kind, name in missing_resources)
                    and all(key in tombstones for key in missing_resources)
                ):
                    return resources
            transition_blocks = load_desired_transition_blocks(root)
            if (
                transition_blocks.get(unit_name)
                and isinstance(stack_resource, StackResource)
                and isinstance(stack_resource.spec, DesiredStackSpec)
                and stack_resource.spec.resolvedProjection is not None
                and stack_resource.metadata.is_root
                and resource_owner_reference(stack_resource) is None
                and unit_name not in _current_desired_unit_paths(root)
            ):
                projected_units = stack_resource.spec.resolvedProjection.get("units")
                if isinstance(projected_units, dict) and unit_name.removeprefix(f"{stack_name}--") in projected_units:
                    validation_projection = dict(projected_units)
                    for blocked_name in transition_blocks:
                        logical_name = blocked_name.removeprefix(f"{stack_name}--")
                        if (
                            blocked_name.startswith(f"{stack_name}--")
                            and not unit_document_path(root, blocked_name).is_file()
                        ):
                            validation_projection.pop(logical_name, None)
                    validation_resources = dict(resources)
                    validation_resources[stack_key] = replace(
                        stack_resource,
                        spec=replace(
                            stack_resource.spec,
                            resolvedProjection=JsonObjectValue({"units": validation_projection}),
                        ),
                    )
                    validate_desired_resource_graph(validation_resources)
                    return resources
        raise OperationError(f"invalid desired resource graph: {exc}") from exc
    return resources


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

    templates = {
        resource.name: resource
        for resource in resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate"
    }
    edges: dict[str, set[str]] = {}
    for stack in (
        resource
        for resource in resources.values()
        if isinstance(resource, StackResource) and resource.gvk.kind == "Stack"
    ):
        if not isinstance(stack.spec, (StackSpec, DesiredStackSpec)):
            continue
        template_name = stack.spec.template if isinstance(stack.spec.template, str) else stack.spec.template.name
        uses_resource_template = isinstance(stack.spec.template, str) or isinstance(
            stack.spec.template.source, StackTemplateFromResource
        )
        template = templates.get(template_name) if uses_resource_template else None
        if template is None or not isinstance(template.spec, StackTemplateSpec):
            projection = stack.spec.resolvedProjection if isinstance(stack.spec, DesiredStackSpec) else None
            units = projection.get("units") if isinstance(projection, dict) else None
            if not isinstance(units, dict):
                continue
            for logical_name, value in units.items():
                if not isinstance(logical_name, str) or not isinstance(value, dict):
                    continue
                dependencies = value.get("dependsOn", [])
                if not isinstance(dependencies, list):
                    continue
                generated_name = stack_generated_unit_name(stack.name, logical_name)
                if include_missing or any(
                    isinstance(item, UnitResource) and item.name == generated_name for item in resources.values()
                ):
                    edges.setdefault(generated_name, set()).update(
                        stack_generated_unit_name(stack.name, dependency)
                        for dependency in dependencies
                        if isinstance(dependency, str)
                        and (
                            include_missing
                            or any(
                                isinstance(item, UnitResource)
                                and item.name == stack_generated_unit_name(stack.name, dependency)
                                for item in resources.values()
                            )
                        )
                    )
            continue
        expanded_by_name = {
            resource.name: resource
            for resource in scope_stack_template_resources(
                stack.name,
                template.spec.expand(stack.spec.parameters),
            )
        }
        for generated in expanded_by_name.values():
            generated_key = (generated.apiVersion, generated.kind, generated.name)
            if not include_missing and generated_key not in resources:
                # A deleting Stack may intentionally omit a finalized child.
                continue
            edges.setdefault(generated.name, set()).update(
                dependency
                for dependency in generated.dependsOn
                if include_missing
                or (
                    expanded_by_name[dependency].apiVersion,
                    expanded_by_name[dependency].kind,
                    dependency,
                )
                in resources
            )
    return {name: tuple(sorted(dependencies)) for name, dependencies in sorted(edges.items())}


def desired_unit_names(root: Path) -> tuple[str, ...]:
    """Return materialized desired Unit names, including Stack-owned Units."""

    names = {path.stem for path in (root / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}}
    if _current_desired_stack_paths(root, "Stack"):
        names.update(
            resource.name
            for resource in load_desired_resource_graph(root).values()
            if isinstance(resource, UnitResource)
        )
    return tuple(sorted(names))


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
    return hash_source_inputs(
        source_root,
        source_path,
        inputs,
        {
            "kind": "unit",
            "driver": driver,
            "driverVersion": DRIVER_VERSIONS[driver],
            "specification": specification.driver.unit_contract.dump(specification.spec),
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
    for name, path in _current_desired_unit_paths(root).items():
        try:
            resource = load_desired_unit(path, name)
        except (DocumentFormatError, DriverError, KeyError, TypeError, ValueError, OperationError):
            opaque_units[name] = opaque_document_payload(path)
            continue
        resources[(resource.gvk.api_version, resource.gvk.kind, resource.name)] = resource
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
                previous.name,
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
    directory = root / "artifacts" / unit_name
    selected = load_project_config(REPOSITORY_ROOT).write_format
    return directory / f"{artifact_name}{selected.suffix}"


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
    target = observed / "artifacts" / unit_name
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
        path = write_document(target / f"{name}{selected.suffix}", serialized, format=selected)
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
    expected_path = artifact_document_path(observed, receipt.name, artifact_name)
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
    directory = observed / "artifacts" / unit.name
    actual_paths = {path for path in directory.rglob("*") if path.is_file()} if directory.is_dir() else set()
    expected_paths = {artifact_document_path(observed, unit.name, name) for name in expected}
    if actual_paths != expected_paths:
        raise OperationError(f"persisted {driver_name} artifact files do not match its complete contract set")
    for artifact_name in expected:
        load_artifact_document(observed, unit, receipt, artifact_name)


def current_receipt(observed: Path, candidate_units: Path, unit_name: str) -> ReceiptResource[Any] | None:
    receipt_path = unit_document_path(observed, unit_name)
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
    if not isinstance(unit_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", unit_name):
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
            json_pointer(document, reference.pointer), file_blob(unit_document_path(observed, reference.unit))
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
            source_unit_path = unit_document_path(
                promotion.desired_root,
                stack_generated_unit_name(imported.fromPromotion.stack, imported.unit),
            )
            if not source_unit_path.is_file():
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
                promotion.observed_root, promotion.desired_root / "units", source_unit.name
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
) -> ResolvedUnitSourceResult:
    source_revision_policy = source_revision_policy or SourceRevisionPolicy()
    driver, source = require_unit_specification(specification)
    if source is None:
        return ResolvedUnitSourceResult(source=None, disposition=SourceResolutionDisposition.UNCHANGED)
    if source_revision is None:
        raise OperationError(
            f"Unit {specification.name!r} uses repository-backed source; apply it with --source-revision <commit>"
        )
    input_hash = unit_input_hash(specification, source_root)
    revision = source_revision
    prior = prior_unit_source(specification.name, current_desired)
    disposition = SourceResolutionDisposition.INPUTS_CHANGED if prior is None else SourceResolutionDisposition.UNCHANGED
    refresh_reason: str | None = None
    if prior is not None:
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
                or commit_is_ancestor(prior_revision, source_revision)
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
    if desired_ref == observed_ref:
        raise OperationError(f"{environment_name} desired and observed refs must differ")
    return desired_ref, observed_ref


def effect_lease_ref(environment: str, desired_ref: str) -> str | None:
    """Resolve the configured lease store for one environment.

    ``None`` disables leases. Low-level lease helpers keep their historical
    co-located behavior when called without this resolved value.
    """

    try:
        store = load_project_config(REPOSITORY_ROOT).effect_lease_store
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
        "finalize",
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


def resolve_candidate_ref(
    source_root: Path,
    environment_name: str,
    operation: Literal[
        "promotion",
        "rollback",
        "finalize",
        "apply",
        "delete",
        "resolve-opaque-unit",
    ],
    candidate_id: str,
    override: str | None = None,
) -> str:
    if override:
        return override
    template = candidate_ref_template(source_root, environment_name)
    return (
        template.replace("{environment}", environment_name)
        .replace("{operation}", operation)
        .replace("{id}", candidate_id)
    )


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
    applied_stacks: frozenset[str] = frozenset()


def _write_desired_stack_resource(path: Path, resource: StackResource, project_root: Path) -> Path:
    document = RESOURCE_CATALOG.serialize_stack_resource(resource, profile="desired")
    if resource_documents_enabled(project_root):
        selected = load_project_config(project_root).write_format
        path = path.with_suffix(selected.suffix)
    else:
        selected = DocumentFormat.YAML if path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    return write_document(path, document, format=selected)


def _stack_template_reference(spec: StackSpec) -> StackTemplateReference:
    template = spec.template
    return template if isinstance(template, StackTemplateReference) else StackTemplateReference(name=template)


def _clone_stack_template_repository(remote: str, checkout: Path) -> None:
    clone = subprocess.run(
        ("git", "clone", "--no-checkout", "--quiet", remote, str(checkout)),
        text=True,
        capture_output=True,
    )
    if clone.returncode != 0:
        raise OperationError(f"could not fetch StackTemplate source {remote!r}: {clone.stderr.strip()}")


def _checkout_stack_template_commit(checkout: Path, commit: str, location: str) -> None:
    checkout_result = subprocess.run(
        ("git", "-C", str(checkout), "checkout", "--quiet", "--detach", commit),
        text=True,
        capture_output=True,
    )
    if checkout_result.returncode != 0:
        raise OperationError(f"Git commit {commit!r} is not available in {location!r}")


def _load_exact_stack_template_document(
    root: Path,
    resource_path: str,
    template_name: str,
    *,
    expected_digest: str | None = None,
) -> StackResource:
    """Load and validate one exact, repository-relative StackTemplate document."""

    path = root.joinpath(*PurePosixPath(resource_path).parts)
    if not path.is_file():
        raise OperationError(f"pinned StackTemplate document {resource_path!r} is not available")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise OperationError(
            f"pinned StackTemplate document {resource_path!r} has digest {digest}, expected {expected_digest}"
        )
    try:
        document = RESOURCE_CATALOG.load_document(path)
        return RESOURCE_CATALOG.parse_stack_template(document, profile="authored", expected_name=template_name)
    except (ContractError, OperationError, ValueError) as error:
        raise OperationError(f"pinned StackTemplate document {resource_path!r} is invalid: {error}") from error


def _load_pinned_stack_template(source: ResolvedStackTemplateSource, template_name: str) -> StackResource:
    """Load a StackTemplate from its authoritative immutable source pin."""

    pin = source.fromGit
    with tempfile.TemporaryDirectory(prefix="gitopsctr-pinned-stack-template-") as temporary_directory:
        checkout = Path(temporary_directory) / "repo"
        if pin.remote is not None:
            _clone_stack_template_repository(pin.remote, checkout)
            _checkout_stack_template_commit(checkout, pin.commit, pin.remote)
            template_root = checkout
        else:
            try:
                materialize_revision(pin.commit, checkout)
            except (OperationError, subprocess.CalledProcessError) as error:
                raise OperationError(
                    f"Git commit {pin.commit!r} is not available for pinned StackTemplate source"
                ) from error
            assert pin.path is not None
            template_root = checkout.joinpath(*PurePosixPath(pin.path).parts)
        return _load_exact_stack_template_document(
            template_root,
            pin.resourcePath,
            template_name,
            expected_digest=pin.digest,
        )


def _fetch_repository_local_stack_template_pin(
    promotion: PromotionContext,
    source_stack: StackResource,
    source: ResolvedStackTemplateSource,
) -> None:
    """Fetch and verify the controller pin retaining a repository-local source."""

    pin = source.fromGit
    if pin.path is None:
        return
    if source_stack.metadata.uid is None:
        raise OperationError(f"promoted source Stack {source_stack.name!r} has no UID")
    name = _stack_source_pin_name(
        promotion.source_environment,
        source_stack.name,
        source_stack.metadata.uid,
        pin.commit,
    )
    try:
        matching = [candidate for candidate in state_store().list_controller_pins() if candidate.name == name]
    except OperationError as exc:
        raise OperationError(f"Git commit {pin.commit!r} is not available for pinned StackTemplate source") from exc
    if len(matching) != 1 or matching[0].revision != pin.commit:
        raise OperationError(
            f"promoted source Stack {source_stack.name!r} controller pin is unavailable or does not match {pin.commit}"
        )
    fetched = state_store().git("fetch", "--no-tags", "origin", matching[0].ref, check=False)
    if fetched.returncode != 0 or not commit_is_available(pin.commit):
        raise OperationError(f"Git commit {pin.commit!r} is not available from controller pin {matching[0].ref!r}")


def _resolve_stack_template(
    source_root: Path,
    template_ref: StackTemplateReference,
    templates: Mapping[str, StackResource],
    source_revision: str | None,
    promotion: PromotionContext | None = None,
) -> tuple[StackResource, ResolvedStackTemplateSource]:
    """Resolve one StackTemplate source for desired-state projection."""

    source = template_ref.source
    if isinstance(source, StackTemplateFromPromotion):
        if promotion is None:
            raise OperationError("fromPromotion StackTemplate source requires an active Promotion")
        source_path = document_candidates(promotion.desired_root / "stacks", source.fromPromotion.stack)
        if len(source_path) != 1:
            raise OperationError(f"promoted source Stack {source.fromPromotion.stack!r} is not available")
        source_stack = RESOURCE_CATALOG.parse_stack(
            RESOURCE_CATALOG.load_document(source_path[0]),
            profile="desired",
            expected_name=source.fromPromotion.stack,
        )
        if not isinstance(source_stack.spec, DesiredStackSpec):
            raise OperationError("promoted source Stack is not a desired Stack")
        source_template = cast(StackTemplateReference, source_stack.spec.template)
        if source_template.name != template_ref.name:
            raise OperationError(
                f"promoted source Stack uses StackTemplate {source_template.name!r}, "
                f"but target Stack references {template_ref.name!r}"
            )
        if source_stack.spec.resolvedSource is None:
            raise OperationError("promoted source Stack has no resolved source")
        if source_stack.spec.resolvedSource.fromGit.path is not None and not commit_is_available(
            source_stack.spec.resolvedSource.fromGit.commit
        ):
            _fetch_repository_local_stack_template_pin(promotion, source_stack, source_stack.spec.resolvedSource)
        return (
            _load_pinned_stack_template(source_stack.spec.resolvedSource, template_ref.name),
            source_stack.spec.resolvedSource,
        )
    if isinstance(source, StackTemplateFromGit):
        request = source.fromGit
        if request.remote is not None:
            with tempfile.TemporaryDirectory(prefix="gitopsctr-stack-template-") as temporary_directory:
                checkout = Path(temporary_directory) / "repo"
                _clone_stack_template_repository(request.remote, checkout)
                selected = request.commit
                if selected is None:
                    assert request.ref is not None
                    revision = subprocess.run(
                        ("git", "-C", str(checkout), "rev-parse", f"{request.ref}^{{commit}}"),
                        text=True,
                        capture_output=True,
                    )
                    if revision.returncode != 0:
                        raise OperationError(f"Git ref {request.ref!r} does not exist in {request.remote!r}")
                    selected = revision.stdout.strip()
                _checkout_stack_template_commit(checkout, selected, request.remote)
                project = load_project_config(checkout)
                catalog_root = checkout.joinpath(*project.stack_templates_path.parts)
                paths = document_candidates(catalog_root, template_ref.name)
                if len(paths) != 1:
                    raise OperationError(f"expected exactly one StackTemplate document for {template_ref.name!r}")
                template_path = paths[0]
                resource_path = template_path.relative_to(checkout).as_posix()
                template = _load_exact_stack_template_document(
                    checkout,
                    resource_path,
                    template_ref.name,
                )
                return template, ResolvedStackTemplateSource(
                    fromGit=ResolvedGitSource(
                        remote=request.remote,
                        commit=selected,
                        resourcePath=resource_path,
                        digest=hashlib.sha256(template_path.read_bytes()).hexdigest(),
                        ref=request.ref,
                    )
                )
        selected = request.commit
        if selected is None and request.ref is not None:
            revision = git("rev-parse", "--verify", f"{request.ref}^{{commit}}", check=False)
            if revision.returncode != 0:
                raise OperationError(f"Git ref {request.ref!r} does not exist in the source repository")
            selected = revision.stdout.strip()
        if selected is None:
            if source_revision is None:
                raise OperationError(
                    f"Stack {template_ref.name!r} uses a repository-local StackTemplate source; "
                    "apply it with --source-revision <commit>"
                )
            selected = source_revision
        with tempfile.TemporaryDirectory(prefix="gitopsctr-stack-template-") as temporary_directory:
            if selected == source_revision:
                checkout = source_root
            else:
                checkout = Path(temporary_directory) / "repo"
                try:
                    materialize_revision(selected, checkout)
                except (OperationError, subprocess.CalledProcessError) as error:
                    raise OperationError(
                        f"Git commit {selected!r} is not available in the source repository"
                    ) from error
            assert request.path is not None
            template_root = checkout.joinpath(*PurePosixPath(request.path).parts)
            project = load_project_config(template_root)
            catalog_root = template_root.joinpath(*project.stack_templates_path.parts)
            paths = document_candidates(catalog_root, template_ref.name)
            if len(paths) != 1:
                raise OperationError(f"expected exactly one StackTemplate document for {template_ref.name!r}")
            resource_path = paths[0].relative_to(template_root).as_posix()
            template = _load_exact_stack_template_document(template_root, resource_path, template_ref.name)
            return template, ResolvedStackTemplateSource(
                fromGit=ResolvedGitSource(
                    path=request.path,
                    commit=selected,
                    resourcePath=resource_path,
                    digest=hashlib.sha256(paths[0].read_bytes()).hexdigest(),
                    ref=request.ref,
                )
            )
    template = templates.get(template_ref.name)
    if template is None:
        raise OperationError(f"Stack {template_ref.name!r} references missing StackTemplate")
    if source_revision is None:
        raise OperationError(
            f"Stack {template_ref.name!r} uses StackTemplate {template_ref.name!r} from the source repository; "
            "apply it with --source-revision <commit>"
        )
    path_candidates = document_candidates(
        source_root.joinpath(*load_project_config(source_root).stack_templates_path.parts), template_ref.name
    )
    resource_path = (
        path_candidates[0].relative_to(source_root).as_posix() if len(path_candidates) == 1 else template_ref.name
    )
    return template, ResolvedStackTemplateSource(
        fromGit=ResolvedGitSource(
            path=".",
            commit=source_revision,
            resourcePath=resource_path,
            digest=hashlib.sha256(path_candidates[0].read_bytes() if len(path_candidates) == 1 else b"").hexdigest(),
        )
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
        previous_tombstone = load_resource_incarnation_tombstones(current_desired).get((CORE_API_VERSION, kind, name))
        if previous_tombstone is not None and metadata.uid == previous_tombstone.uid:
            metadata = ResourceMetadata.root_from_provenance(
                name,
                provenance + "\0reincarnation:" + previous_tombstone.uid,
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


def project_stack_resources(
    source_root: Path,
    environment_name: str,
    source_revision: str | None,
    candidate: Path,
    project_root: Path,
    current_desired: Path | None = None,
    promotion: PromotionContext | None = None,
    partition: str | None = None,
) -> StackProjection:
    """Persist Stack roots and expand concrete Stacks into authored Unit inputs."""

    (candidate / "stack-templates").mkdir(parents=True, exist_ok=True)
    (candidate / "stacks").mkdir(parents=True, exist_ok=True)
    templates, stacks = _load_authored_stack_resources(source_root, environment_name)
    generated: dict[str, UnitResource[Any]] = {}
    owners: dict[str, DesiredOwnerReference] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    artifact_imports: dict[str, tuple[ArtifactImport, ...]] = {}
    selected_resource_templates: set[str] = set()
    for name, authored_stack in stacks.items():
        assert isinstance(authored_stack.spec, StackSpec)
        template_ref = _stack_template_reference(authored_stack.spec)
        template, resolved_source = _resolve_stack_template(
            source_root, template_ref, templates, source_revision, promotion
        )
        if isinstance(template_ref.source, StackTemplateFromResource):
            selected_resource_templates.add(template_ref.name)
            authored_template_spec = cast(StackTemplateSpec, template.spec)
            desired_template = StackResource(
                template.gvk,
                _stack_root_metadata(
                    "StackTemplate",
                    template_ref.name,
                    source_revision,
                    current_desired,
                    partition,
                ),
                DesiredStackTemplateSpec(
                    parameters=authored_template_spec.parameters,
                    unitTemplates=authored_template_spec.unitTemplates,
                    requestedSource=StackTemplateFromGit(fromGit=GitSourceRequest(path=".")),
                    resolvedSource=resolved_source,
                ),
            )
            _write_desired_stack_resource(
                candidate / "stack-templates" / f"{template_ref.name}.json",
                desired_template,
                project_root,
            )
        stack = StackResource(
            authored_stack.gvk,
            _stack_root_metadata("Stack", name, source_revision, current_desired, partition),
            DesiredStackSpec(
                template=template_ref,
                parameters=authored_stack.spec.parameters,
                units=authored_stack.spec.units,
                artifactImports=authored_stack.spec.artifactImports,
                requestedSource=template_ref.source,
                resolvedSource=resolved_source,
                resolvedProjection=None,
            ),
        )
        template_spec = cast(StackTemplateSpec, template.spec)
        expanded_template = template_spec.expand(authored_stack.spec.parameters)
        selected_names = set(authored_stack.spec.units or (resource.name for resource in expanded_template))
        known_names = {resource.name for resource in expanded_template}
        unknown = sorted(selected_names - known_names)
        if unknown:
            raise OperationError(f"Stack {name!r} selects unknown Unit templates: {', '.join(unknown)}")
        promoted_imports: Mapping[str, ResolvedArtifactImport] = {}
        if isinstance(template_ref.source, StackTemplateFromPromotion) and promotion is not None:
            source_paths = document_candidates(
                promotion.desired_root / "stacks", template_ref.source.fromPromotion.stack
            )
            if len(source_paths) == 1:
                promoted_source_stack = RESOURCE_CATALOG.parse_stack(
                    RESOURCE_CATALOG.load_document(source_paths[0]),
                    profile="desired",
                    expected_name=template_ref.source.fromPromotion.stack,
                )
                if isinstance(promoted_source_stack.spec, DesiredStackSpec):
                    promoted_imports = promoted_source_stack.spec.resolvedArtifactImports or {}
        for imported in authored_stack.spec.artifactImports:
            has_promoted_evidence = any(
                key == f"{imported.unit}/{imported.name}" or key.endswith(f"--{imported.unit}/{imported.name}")
                for key in promoted_imports
            )
            if imported.unit not in known_names and not has_promoted_evidence:
                raise OperationError(f"Stack {name!r} imports an artifact from unknown Unit template {imported.unit!r}")
            if imported.unit in selected_names:
                raise OperationError(
                    f"Stack {name!r} imports an artifact from selected Unit template {imported.unit!r}"
                )
        for resource in expanded_template:
            if resource.name in selected_names:
                omitted = sorted(set(resource.dependsOn) - selected_names)
                if omitted:
                    raise OperationError(
                        f"Stack {name!r} selects {resource.name!r} but omits dependencies: {', '.join(omitted)}"
                    )
        expanded_template = tuple(resource for resource in expanded_template if resource.name in selected_names)
        projection_document: JsonObjectValue = JsonObjectValue(
            {
                "units": {
                    resource.name: {
                        "apiVersion": resource.apiVersion,
                        "kind": resource.kind,
                        "spec": cast(JsonObjectValue, dump_template_value(cast(TemplateValue, resource.spec))),
                        "dependsOn": list(resource.dependsOn),
                    }
                    for resource in expanded_template
                }
            }
        )
        stack = replace(stack, spec=replace(cast(DesiredStackSpec, stack.spec), resolvedProjection=projection_document))
        _write_desired_stack_resource(candidate / "stacks" / f"{name}.json", stack, project_root)
        assert isinstance(template.spec, StackTemplateSpec)
        for resource in scope_stack_template_resources(
            name,
            tuple(
                StackTemplateResource(
                    apiVersion=item.apiVersion,
                    kind=item.kind,
                    name=item.name,
                    spec=item.spec,
                    dependsOn=item.dependsOn,
                )
                for item in expanded_template
            ),
        ):
            document: JsonObject = {
                "apiVersion": resource.apiVersion,
                "kind": resource.kind,
                "metadata": {"name": resource.name},
                "spec": cast(JsonObjectValue, dump_template_value(cast(TemplateValue, resource.spec))),
            }
            unit = RESOURCE_CATALOG.parse_unit(document, profile="authored", expected_name=resource.name)
            require_unit_specification(unit, resource.name)
            if resource.name in generated:
                raise OperationError(
                    f"generated Unit {resource.name!r} is produced by more than one Stack; names must be globally unique"
                )
            generated[resource.name] = unit
            dependencies[resource.name] = tuple(resource.dependsOn)
            artifact_imports[resource.name] = tuple(authored_stack.spec.artifactImports)
            assert stack.metadata.uid is not None
            owners[resource.name] = DesiredOwnerReference(
                apiVersion=stack.gvk.api_version,
                kind=stack.gvk.kind,
                name=stack.name,
                uid=stack.metadata.uid,
            )
    # Keep desired Stack roots available while their owned Units are being
    # finalized.  Deletion metadata keeps the UID-fenced graph intact until
    # child-first finalization removes the resources.
    if current_desired is not None:
        for kind in ("StackTemplate", "Stack"):
            source_names = selected_resource_templates if kind == "StackTemplate" else set(stacks)
            for name, previous_path in _current_desired_stack_paths(current_desired, kind).items():
                if name in source_names:
                    continue
                target = candidate / ("stack-templates" if kind == "StackTemplate" else "stacks") / previous_path.name
                previous = (
                    RESOURCE_CATALOG.parse_stack_template(
                        RESOURCE_CATALOG.load_document(previous_path), profile="desired", expected_name=name
                    )
                    if kind == "StackTemplate"
                    else RESOURCE_CATALOG.parse_stack(
                        RESOURCE_CATALOG.load_document(previous_path), profile="desired", expected_name=name
                    )
                )
                if partition is not None and previous.metadata.partition == partition:
                    previous = cast(StackResource, mark_resource_for_deletion(previous))
                _write_desired_stack_resource(target, previous, project_root)
    return StackProjection(
        generated,
        owners,
        dependencies,
        artifact_imports,
        applied_stacks=frozenset(stacks),
    )


def load_convergence_specifications(
    source_root: Path,
    environment_name: str,
    current_desired: Path,
    projection_revision: str,
    projection_root: Path,
) -> tuple[dict[str, UnitResource[Any]], dict[str, tuple[str, ...]]]:
    """Load source and desired-only Units participating in convergence.

    Source Unit documents remain the authored authority. Stack-generated and
    already-applied Units are added from the desired snapshot for inspection.
    """

    specifications = load_environment_specifications(source_root, environment_name)
    dependency_edges: dict[str, tuple[str, ...]] = {}
    if _current_desired_stack_paths(current_desired, "Stack"):
        # A desired Stack is an immutable projection. Reconcile must not
        # rebuild it from a mutable source branch or remote repository.
        resources = load_desired_resource_graph(current_desired)
        dependency_edges.update(stack_dependency_edges(resources))
        transition_blocks = load_desired_transition_blocks(current_desired)
        for resource in resources.values():
            if not isinstance(resource, UnitResource) or resource_deletion(resource) is not None:
                continue
            if resource.name in transition_blocks:
                continue
            owner = resource_owner_reference(resource)
            is_stack_owned = owner is not None and owner.kind == "Stack"
            if not (is_stack_owned or owner is None):
                continue
            existing = specifications.get(resource.name)
            if existing is not None and existing.gvk != resource.gvk:
                raise OperationError(f"desired-only Unit {resource.name!r} collides with a source Unit")
            specifications[resource.name] = resource
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
        dependency_edges.update(projection.dependencies)

    return specifications, {name: tuple(sorted(values)) for name, values in dependency_edges.items()}


def require_environment_unit(source_root: Path, environment_name: str, unit_name: str) -> None:
    specifications = load_environment_specifications(source_root, environment_name)
    if unit_name not in specifications:
        available = ", ".join(sorted(specifications))
        raise OperationError(
            f"unknown unit {unit_name!r} for environment {environment_name!r}; available units: {available}"
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
        receipt_path = unit_document_path(observed, unit_name)
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
    receipt_path = unit_document_path(observed, unit_name)
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


def log_reconciliation_summary(environment_name: str, source_root: Path, desired: Path, observed: Path) -> None:
    specifications = load_environment_specifications(source_root, environment_name)
    statuses = reconciliation_statuses(sorted(specifications), desired, observed)
    log_reconciliation_status(environment_name, statuses)


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


def convergence_plan_rows(
    statuses: list[tuple[str, str, str]],
    order: Sequence[str],
) -> list[tuple[str, str, str]]:
    """Turn receipt-level status into the operator-facing convergence schedule."""
    by_unit = {unit_name: (status, reason) for unit_name, status, reason in statuses}
    next_unit = next((unit_name for unit_name in order if by_unit.get(unit_name, (None,))[0] == "READY"), None)
    rows = []
    for unit_name in order:
        status, reason = by_unit[unit_name]
        if status == "READY" and unit_name == next_unit:
            disposition = "NEXT"
        elif status == "READY":
            disposition = "LATER"
            reason = f"re-evaluate after {next_unit}"
        else:
            disposition = status
        rows.append((unit_name, disposition, reason))
    return rows


def log_convergence_plan(
    rows: list[tuple[str, str, str]],
    previous: list[tuple[str, str, str]] | None = None,
) -> None:
    previous_by_unit = {unit_name: (status, reason) for unit_name, status, reason in previous or []}
    changed = [row for row in rows if previous_by_unit.get(row[0]) != row[1:] or row[1] == "NEXT"]
    log_heading("Plan" if previous is None else "Plan update")
    for unit_name, status, reason in changed:
        styled_name = style_unit(unit_name)
        message = styled_name if status in {"CLEAN", "MATERIALIZED"} else f"{styled_name}: {reason}"
        log_status(status, message)


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
    source_root: Path,
    source_revision: str | None,
    current_desired: Path,
    candidate: Path,
) -> UnitResource[Any]:
    unit_name = resolved.name
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
                unit_name=unit_name,
                unit=resolved.spec,
                output_root=output_root,
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

    def document(self) -> JsonObject:
        return {
            "schema": 1,
            "kind": "ResourceIncarnationTombstone",
            "resource": {
                "apiVersion": self.api_version,
                "kind": self.kind,
                "name": self.name,
                "uid": self.uid,
                "deletionGeneration": self.deletion_generation,
            },
        }

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
        if not isinstance(resource, dict) or set(resource) != {
            "apiVersion",
            "kind",
            "name",
            "uid",
            "deletionGeneration",
        }:
            raise ValueError("invalid resource incarnation identity")
        api_version = resource.get("apiVersion")
        kind = resource.get("kind")
        name = resource.get("name")
        uid = resource.get("uid")
        deletion_generation = resource.get("deletionGeneration")
        if not all(isinstance(value, str) and value for value in (api_version, kind, name, uid)):
            raise ValueError("invalid resource incarnation identity")
        GVK(cast(str, api_version), cast(str, kind))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cast(str, name)):
            raise ValueError("invalid resource incarnation name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", cast(str, uid)):
            raise ValueError("invalid resource incarnation UID")
        if type(deletion_generation) is not int or deletion_generation < 1:
            raise ValueError("invalid resource incarnation deletion generation")
        return cls(
            api_version=cast(str, api_version),
            kind=cast(str, kind),
            name=cast(str, name),
            uid=cast(str, uid),
            deletion_generation=deletion_generation,
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
    expires_at: int | None
    snapshot: EffectLeaseSnapshot | None = None

    def document(self) -> JsonObject:
        return {
            "schema": 1,
            "kind": "UnitEffectLease",
            "unitName": self.unit_name,
            "uid": self.uid,
            "token": self.token,
            "owner": self.owner,
            "desiredRevision": self.desired_revision,
            "expiresAt": self.expires_at,
            **({"snapshot": self.snapshot.document()} if self.snapshot is not None else {}),
        }

    @classmethod
    def from_document(cls, document: object, expected_name: str) -> EffectLease:
        if not isinstance(document, dict) or set(document) not in (
            {
                "schema",
                "kind",
                "unitName",
                "uid",
                "token",
                "owner",
                "desiredRevision",
                "expiresAt",
            },
            {
                "schema",
                "kind",
                "unitName",
                "uid",
                "token",
                "owner",
                "desiredRevision",
                "expiresAt",
                "snapshot",
            },
        ):
            raise ValueError("invalid effect lease envelope")
        uid = document.get("uid")
        token = document.get("token")
        owner = document.get("owner")
        desired_revision = document.get("desiredRevision")
        expires_at = document.get("expiresAt")
        if (
            type(document.get("schema")) is not int
            or document.get("schema") != 1
            or document.get("kind") != "UnitEffectLease"
            or document.get("unitName") != expected_name
            or not isinstance(uid, str)
            or not isinstance(token, str)
            or not isinstance(owner, str)
            or not isinstance(desired_revision, str)
            or (
                expires_at is not None
                and (not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at < 1)
            )
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", uid)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", token)
            or not owner
            or not re.fullmatch(r"[0-9a-f]{40}", desired_revision)
        ):
            raise ValueError("invalid effect lease fence")
        ResourceMetadata(name=expected_name, uid=uid).validate_desired()
        snapshot = EffectLeaseSnapshot.from_document(document["snapshot"]) if "snapshot" in document else None
        return cls(
            unit_name=expected_name,
            uid=uid,
            token=token,
            owner=owner,
            desired_revision=desired_revision,
            expires_at=expires_at,
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
    ttl_seconds: int = EFFECT_LEASE_TTL_SECONDS,
    lease_ref: str | None = None,
) -> EffectLeaseAcquisition:
    """Renew one lease against the latest head while fencing the same Unit snapshot."""

    if ttl_seconds < 1:
        raise OperationError("effect lease TTL must be positive")
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
            existing = load_desired_effect_leases(lease_root).get(acquisition.lease.unit_name)
            if (
                existing is None
                or existing.token != acquisition.lease.token
                or existing.uid != acquisition.lease.uid
                or acquisition.lease.snapshot is None
                or existing.snapshot != acquisition.lease.snapshot
                or effect_lease_snapshot(current, acquisition.lease.unit_name, acquisition.lease.uid)
                != acquisition.lease.snapshot
            ):
                raise EffectLeaseUnavailable(
                    f"effect lease for {acquisition.lease.unit_name!r} no longer fences the same Unit snapshot"
                )
            renewed = replace(
                existing,
                desired_revision=current_revision,
                expires_at=None,
            )
            write_effect_lease(lease_root, renewed)
            try:
                published_revision = publish_tree(
                    _effect_lease_publish_ref(desired_ref, lease_ref),
                    lease_root,
                    lease_revision,
                    f"Renew effect lease for {renewed.unit_name} ({renewed.token})",
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
    interval = interval_seconds if interval_seconds is not None else min(30.0, EFFECT_LEASE_TTL_SECONDS / 3)
    return EffectLeaseHeartbeat(desired_ref, acquisition, max(0.01, interval), lease_ref).start()


@dataclass(frozen=True)
class TeardownEvidence:
    """Observed-state proof that one UID-fenced teardown completed."""

    unit_name: str
    uid: str
    deletion_generation: int
    desired_revision: str
    details: JsonObject = field(default_factory=dict)

    def document(self) -> JsonObject:
        return {
            "schema": 1,
            "kind": "UnitTeardownEvidence",
            "unitName": self.unit_name,
            "uid": self.uid,
            "deletionGeneration": self.deletion_generation,
            "desiredRevision": self.desired_revision,
            "details": self.details,
        }

    @classmethod
    def from_document(cls, document: object, expected_name: str) -> TeardownEvidence:
        required_fields = {
            "schema",
            "kind",
            "unitName",
            "uid",
            "deletionGeneration",
            "desiredRevision",
        }
        if not isinstance(document, dict) or set(document) not in (
            required_fields,
            required_fields | {"details"},
        ):
            raise ValueError("invalid teardown evidence envelope")
        raw_uid = document.get("uid")
        raw_generation = document.get("deletionGeneration")
        raw_revision = document.get("desiredRevision")
        raw_details = document.get("details", {})
        if (
            type(document.get("schema")) is not int
            or document.get("schema") != 1
            or document.get("kind") != "UnitTeardownEvidence"
            or document.get("unitName") != expected_name
            or not isinstance(raw_uid, str)
            or not isinstance(raw_generation, int)
            or isinstance(raw_generation, bool)
            or raw_generation < 1
            or not isinstance(raw_revision, str)
            or not isinstance(raw_details, dict)
        ):
            raise ValueError("invalid teardown evidence envelope")
        ResourceMetadata(name=expected_name, uid=raw_uid).validate_desired()
        if not re.fullmatch(r"[0-9a-f]{40}", raw_revision):
            raise ValueError("invalid teardown evidence desired revision")
        try:
            details = cast(JsonObject, require_json_value(raw_details))
        except ValueError as exc:
            raise ValueError("invalid teardown evidence details") from exc
        return cls(
            unit_name=expected_name,
            uid=raw_uid,
            deletion_generation=raw_generation,
            desired_revision=raw_revision,
            details=details,
        )


def desired_effect_lease_paths(root: Path) -> tuple[Path, ...]:
    directory = root / DESIRED_EFFECT_LEASES_PATH
    if not directory.is_dir():
        return ()
    paths = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix in {".json", ".yaml", ".yml"})
    names: dict[str, Path] = {}
    for path in paths:
        if path.stem in names:
            raise OperationError(f"multiple effect lease formats exist for {path.stem!r}")
        names[path.stem] = path
    return tuple(paths)


def resource_incarnation_path(root: Path, tombstone: ResourceIncarnationTombstone) -> Path:
    return (
        root
        / DESIRED_RESOURCE_INCARNATIONS_PATH
        / PurePosixPath(tombstone.api_version)
        / tombstone.kind
        / f"{tombstone.name}.json"
    )


def load_resource_incarnation_tombstones(
    root: Path,
) -> dict[tuple[str, str, str], ResourceIncarnationTombstone]:
    directory = root / DESIRED_RESOURCE_INCARNATIONS_PATH
    tombstones: dict[tuple[str, str, str], ResourceIncarnationTombstone] = {}
    if not directory.is_dir():
        return tombstones
    for path in sorted(directory.rglob("*.json")):
        try:
            tombstone = ResourceIncarnationTombstone.from_document(load_json(path))
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid resource incarnation tombstone at {path.relative_to(root)}") from exc
        key = (tombstone.api_version, tombstone.kind, tombstone.name)
        if key in tombstones or path != resource_incarnation_path(root, tombstone):
            raise OperationError(f"ambiguous resource incarnation tombstone for {key!r}")
        tombstones[key] = tombstone
    return tombstones


def write_resource_incarnation_tombstone(root: Path, tombstone: ResourceIncarnationTombstone) -> Path:
    path = resource_incarnation_path(root, tombstone)
    return write_document(path, tombstone.document(), format=DocumentFormat.JSON)


def copy_resource_incarnation_tombstones(current: Path, candidate: Path) -> None:
    for tombstone in load_resource_incarnation_tombstones(current).values():
        source = resource_incarnation_path(current, tombstone)
        target = resource_incarnation_path(candidate, tombstone)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def finalized_incarnation_for_resource(
    tombstones: Mapping[tuple[str, str, str], ResourceIncarnationTombstone],
    api_version: str,
    kind: str,
    name: str,
) -> ResourceIncarnationTombstone | None:
    return tombstones.get((api_version, kind, name))


def load_desired_effect_leases(root: Path) -> dict[str, EffectLease]:
    leases: dict[str, EffectLease] = {}
    for path in desired_effect_lease_paths(root):
        name = path.stem
        try:
            lease = EffectLease.from_document(load_json(path), name)
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid effect lease for {name!r}") from exc
        leases[name] = lease
    return leases


def effect_lease_snapshot(root: Path, unit_name: str, uid: str) -> EffectLeaseSnapshot:
    unit_paths = document_candidates(root / "units", unit_name)
    if len(unit_paths) > 1:
        raise OperationError(f"multiple desired Unit formats exist for leased unit {unit_name!r}")
    unit_path = unit_paths[0] if unit_paths else None
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
    if lease.snapshot is None:
        lease = replace(lease, snapshot=effect_lease_snapshot(root, lease.unit_name, lease.uid))
    directory = root / DESIRED_EFFECT_LEASES_PATH
    for path in document_candidates(directory, lease.unit_name):
        path.unlink()
    return write_document(directory / f"{lease.unit_name}.json", lease.document(), format=DocumentFormat.JSON)


def remove_effect_lease(root: Path, unit_name: str) -> None:
    for path in document_candidates(root / DESIRED_EFFECT_LEASES_PATH, unit_name):
        path.unlink()


def effect_lease_owner() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    host = os.uname().nodename
    return f"{run_id or 'local'}-{attempt}-{host}-{os.getpid()}"


def effect_lease_token() -> str:
    return f"lease-{hashlib.sha256(os.urandom(32)).hexdigest()}"


def effect_lease_now() -> int:
    return int(datetime.now(UTC).timestamp())


def effect_lease_active(_lease: EffectLease) -> bool:
    """All persisted leases are active until token-fenced release or recovery."""

    return True


def acquire_effect_lease(
    desired_ref: str,
    desired_revision: str,
    unit_name: str,
    uid: str,
    *,
    ttl_seconds: int = EFFECT_LEASE_TTL_SECONDS,
    precondition: Callable[[Path], None] | None = None,
    resume_existing: bool = False,
    lease_ref: str | None = None,
) -> EffectLeaseAcquisition:
    if ttl_seconds < 1:
        raise OperationError("effect lease TTL must be positive")
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
            existing = leases.get(unit_name)
            if existing is not None:
                if resume_existing:
                    if existing.uid != uid:
                        raise EffectLeaseUnavailable(
                            f"effect lease for {unit_name!r} is fenced to a different Unit UID"
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
                unit_name=unit_name,
                uid=uid,
                token=effect_lease_token(),
                owner=effect_lease_owner(),
                desired_revision=current_revision,
                expires_at=None,
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
        acquisition.lease.unit_name,
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


def teardown_evidence_filename(unit_name: str, uid: str, deletion_generation: int) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", unit_name) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", uid):
        raise OperationError("teardown evidence identity is not safe for a filename")
    if deletion_generation < 1:
        raise OperationError("teardown evidence generation must be positive")
    return f"{unit_name}.{uid}.{deletion_generation}.json"


def load_teardown_evidence(
    root: Path,
    unit_name: str,
    uid: str | None = None,
    deletion_generation: int | None = None,
) -> TeardownEvidence | None:
    directory = root / OBSERVED_TEARDOWN_EVIDENCE_PATH
    if not directory.is_dir():
        return None
    candidates = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix in {".json", ".yaml", ".yml"}
        and (
            path.name in {f"{unit_name}.json", f"{unit_name}.yaml", f"{unit_name}.yml"}
            or path.name.startswith(f"{unit_name}.")
        )
    )
    selected: list[Path] = []
    for path in candidates:
        try:
            evidence = TeardownEvidence.from_document(load_json(path), unit_name)
        except (DocumentFormatError, KeyError, TypeError, ValueError) as exc:
            raise OperationError(f"invalid teardown evidence for {unit_name!r}") from exc
        if path.name != teardown_evidence_filename(unit_name, evidence.uid, evidence.deletion_generation):
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
                unit_name=unit_name,
                uid=uid,
                deletion_generation=deletion_generation,
                desired_revision=desired_revision,
                details=evidence_details,
            )
            receipt_paths = document_candidates(observed / "units", unit_name)
            artifact_path = observed / "artifacts" / unit_name
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
                / teardown_evidence_filename(unit_name, uid, deletion_generation)
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
                        unit_name=unit_name,
                        uid=uid,
                        deletion_generation=deletion_generation,
                        desired_revision=desired_revision,
                        details=evidence_details,
                    )
            write_document(evidence_path, evidence.document(), format=DocumentFormat.JSON)
            if existing is not None and not had_active_observation and observed_revision is not None:
                return observed_revision
            try:
                return publish_tree(
                    observed_ref,
                    observed,
                    observed_revision,
                    f"Record teardown of {unit_name} generation {deletion_generation}",
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not update {observed_ref} after concurrent updates")


def desired_uid_provenance(
    unit: UnitResource[Any],
    source: DesiredSource | None,
    source_revision: str | None,
    previous_finalized_uid: str | None = None,
) -> str:
    return json.dumps(
        {
            "apiVersion": unit.gvk.api_version,
            "kind": unit.gvk.kind,
            "name": unit.name,
            "source": source.to_dict() if source is not None else None,
            "sourceRevision": source_revision,
            "previousFinalizedUid": previous_finalized_uid,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def root_metadata_for_resource(
    unit: UnitResource[Any],
    source: DesiredSource | None = None,
    source_revision: str | None = None,
    previous_finalized_uid: str | None = None,
) -> ResourceMetadata:
    retained_source = source if source is not None else getattr(unit.spec, "source", None)
    if retained_source is not None and not isinstance(retained_source, DesiredSource):
        retained_source = None
    return ResourceMetadata.root_from_provenance(
        unit.name,
        desired_uid_provenance(unit, retained_source, source_revision, previous_finalized_uid),
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
    inputs = source.get("inputs")
    return DesiredSource(
        path=source["path"],
        revision=source.get("revision") if isinstance(source.get("revision"), str) else None,
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
            incarnations = load_resource_incarnation_tombstones(current)
            if any(tombstone.name == args.unit and tombstone.uid == args.uid for tombstone in incarnations.values()):
                raise OperationError(f"opaque cleanup {args.unit!r} has already been finalized")
            if any(
                effect_lease_active(lease)
                for lease in load_desired_effect_leases(lease_root).values()
                if lease.unit_name == args.unit
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
            for path in document_candidates(candidate / "units", args.unit):
                path.unlink()
            write_desired_candidate_unit(
                candidate / "units" / f"{args.unit}{opaque.path.suffix}",
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
                "finalize",
                args.environment,
                candidate,
                desired_ref,
                current_revision,
                {"unit": args.unit, "uid": args.uid, "operation": "recover-opaque-unit"},
            )
            candidate_ref = resolve_candidate_ref(
                REPOSITORY_ROOT,
                args.environment,
                "finalize",
                candidate_id,
                args.candidate_ref,
            )
            if candidate_ref in {desired_ref, observed_ref}:
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
        if candidate_ref in {desired_ref, observed_ref}:
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
    previous_finalized_uid: str | None = None,
    partition: str | None = None,
) -> ResourceMetadata:
    """Select a durable desired identity without reusing a colliding incarnation."""

    if previous is None:
        return root_metadata_for_resource(authored, source, source_revision, previous_finalized_uid).with_partition(
            partition
        )
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
    stems = sorted(
        {path.stem for path in units.glob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}}
    )
    for stem in stems:
        candidates = document_candidates(units, stem)
        if len(candidates) > 1:
            raise OperationError(f"multiple document formats exist for source-absent unit {stem}")
        if candidates:
            paths[stem] = candidates[0]
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
) -> BuildDesiredResult:
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
    candidate_units.mkdir(parents=True)
    (candidate / "stack-templates").mkdir(parents=True)
    (candidate / "stacks").mkdir(parents=True)
    copy_resource_incarnation_tombstones(current_desired, candidate)
    stack_projection = project_stack_resources(
        source_root,
        environment_name,
        source_revision,
        candidate,
        source_root,
        current_desired,
        promotion,
        partition,
    )
    imported_artifact_fingerprints: dict[str, dict[str, str]] = {}
    imported_artifact_evidence: dict[str, dict[str, ResolvedArtifactImport]] = {}
    for unit_name, generated_unit in stack_projection.generated_units.items():
        if unit_name in specifications:
            raise OperationError(f"generated Stack Unit {unit_name!r} collides with a source Unit")
        specifications[unit_name] = generated_unit
    if source_revision_policy is None:
        source_revision_policy = (
            load_project_config(source_root).source_revision_policy
            if any((source_root / name).is_file() for name in PROJECT_CONFIG_NAMES)
            else SourceRevisionPolicy()
        )
    if promotion is not None:
        write_preferred_document(candidate / "promotion.json", promotion.document(), source_root)

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
    incarnation_tombstones = load_resource_incarnation_tombstones(current_desired)
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
                previous = load_desired_unit(previous_unit, unit_name)
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
        source_resolution = resolved_unit_source(
            specification,
            source_root,
            source_revision,
            current_desired,
            source_revision_policy,
            source_revision_operation,
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
            unit_artifact_imports = stack_projection.artifact_imports.get(authored.name, ())
            unit_owner = stack_projection.owners.get(authored.name)
            target_stack_uid = unit_owner.uid if unit_owner is not None else None
            try:
                resolution = authored.driver.resolve_unit(
                    authored.spec,
                    UnitResolutionContext(
                        source=resolved_source,
                        resolve_template=lambda value, pointer, target_unit=authored.name, target_gvk=authored.gvk, artifact_imports=unit_artifact_imports, target_stack_uid=target_stack_uid: (
                            resolve_template(
                                value,
                                candidate,
                                observed,
                                observed_revision,
                                promotion=promotion,
                                target_unit=target_unit,
                                target_gvk=target_gvk,
                                pointer=pointer,
                                dry=dry,
                                environment_document=load_environment(source_root, environment_name),
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
                source_root,
                source_revision,
                current_desired,
                candidate,
            )
            previous_unit = unit_document_path(current_desired, unit_name)
            previous = load_desired_unit(previous_unit, unit_name) if previous_unit.is_file() else None
            previous_incarnation = (
                finalized_incarnation_for_resource(
                    incarnation_tombstones,
                    resolved.gvk.api_version,
                    resolved.gvk.kind,
                    unit_name,
                )
                if previous is None
                else None
            )
            owner = stack_projection.owners.get(unit_name)
            if owner is not None and preserve_stack_owned_metadata and previous is not None:
                previous_owner = resource_owner_reference(previous)
                if previous_owner is not None and previous_owner.kind == "Stack" and previous_owner.name == owner.name:
                    resolved = resolved.with_metadata(previous.metadata)
                else:
                    resolved = resolved.with_metadata(_stack_owned_metadata(unit_name, owner))
            else:
                resolved = resolved.with_metadata(
                    _stack_owned_metadata(unit_name, owner)
                    if owner is not None
                    else desired_metadata_for_candidate(
                        authored,
                        previous,
                        resolved_source,
                        source_revision,
                        previous_incarnation.uid if previous_incarnation is not None else None,
                        partition,
                    )
                )
            candidate_unit = write_desired_candidate_unit(candidate_units / f"{unit_name}.json", resolved, source_root)
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
            previous_resource = load_desired_unit(previous, unit_name)
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
            write_desired_candidate_unit(candidate_units / previous.name, retained, source_root)
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

    for unit_name, retained in retained_transitions.items():
        previous_path = unit_document_path(current_desired, unit_name)
        write_desired_candidate_unit(candidate_units / previous_path.name, retained, source_root)
        if getattr(retained.spec, "materialization", None) is not None:
            copy_unit_materialization(current_desired, candidate, unit_name, retained)
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=retained,
            source=getattr(retained.spec, "source", None),
        )
        retained = cast(UnitResource[Any], mark_resource_for_deletion(retained))
        write_desired_candidate_unit(candidate / "units" / previous_path.name, retained, source_root)
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=retained,
            source=getattr(retained.spec, "source", None),
        )
    for unit_name, previous_path in _current_desired_unit_paths(current_desired).items():
        if unit_name in specifications:
            continue
        try:
            previous = load_desired_unit(previous_path, unit_name)
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
        retained = previous
        write_desired_candidate_unit(candidate_units / previous_path.name, retained, source_root)
        if getattr(retained.spec, "materialization", None) is not None:
            copy_unit_materialization(current_desired, candidate, unit_name, previous)
        owner = resource_owner_reference(retained)
        removed_from_applied_stack = (
            owner is not None and owner.kind == "Stack" and owner.name in stack_projection.applied_stacks
        )
        omitted_partition_root = partition is not None and owner is None and retained.metadata.partition == partition
        if removed_from_applied_stack or omitted_partition_root:
            retained = cast(UnitResource[Any], mark_resource_for_deletion(retained))
            write_desired_candidate_unit(candidate_units / previous_path.name, retained, source_root)
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
    )


def retryable_push_failure(exc: subprocess.CalledProcessError) -> bool:
    detail = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
    return any(marker in detail for marker in ("non-fast-forward", "fetch first", "stale info", "failed to push"))


def require_revision(value: Any, description: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise OperationError(f"{description} must be a full Git commit")
    return value


def load_promotion_context(current_desired: Path, temporary: Path) -> PromotionContext | None:
    paths = document_candidates(current_desired, "promotion")
    if not paths:
        return None
    if len(paths) > 1:
        raise OperationError("multiple promotion document formats exist")
    path = paths[0]
    document = normalize_promotion_document(load_json(path))
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
    desired_root = temporary / "promotion-source"
    materialize_revision(desired_revision, desired_root)
    observed_root = None
    if observed_revision is not None:
        observed_root = temporary / "promotion-observed"
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


def historical_receipt_matches(desired: Path, observed: Path, unit_name: str) -> bool:
    unit_path = unit_document_path(desired, unit_name)
    receipt_path = unit_document_path(observed, unit_name)
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
        receipt.name != unit_name
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
    unit_names = sorted(
        {path.stem for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}}
    )
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


def promotion_lineage(desired: Path) -> dict[str, Any] | None:
    paths = document_candidates(desired, "promotion")
    return normalize_promotion_document(load_json(paths[0])) if paths else None


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
) -> tuple[str, ChangeRequestResult | ManualChangeRequest | None]:
    load_desired_resource_graph(candidate)
    validate_effect_leases_preserved(
        target_ref,
        target_revision,
        candidate,
        current_root,
        allow_removed_units,
        lease_ref=lease_ref,
    )
    if git("check-ref-format", "--branch", candidate_ref, check=False).returncode != 0:
        raise OperationError(f"invalid change candidate ref: {candidate_ref!r}")
    if candidate_ref == target_ref:
        raise OperationError("change candidate ref conflicts with target desired state")
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
        candidate_revision = publish_tree(
            candidate_ref,
            candidate,
            target_revision,
            commit_message,
        )
    verify_gated_candidate(candidate_revision, target_revision)
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
    revision = publish_tree(desired_ref, baseline, None, f"Initialize desired {environment} state")
    shutil.copytree(baseline, current, dirs_exist_ok=True)
    log_status("INIT", f"created inert {style_branch(desired_ref)} at {describe_revision(revision)}")
    return revision


def command_promote(args: argparse.Namespace) -> None:
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
        authored_units, authored_stacks = _write_apply_authored_documents(
            explicit_source, args.to_environment, explicit_documents
        )
        if any(_document_is_canonical_desired(document) for _origin, document in explicit_documents):
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
            partition=args.partition,
        )
        applied = _applied_root_closure(candidate, [*authored_units, *authored_stacks])
        _copy_unrelated_desired_resources(current_target, candidate, applied, args.partition)
        _prune_omitted_partition_resources(current_target, candidate, applied, args.partition)
        candidate_resources = load_desired_resource_graph(candidate)
        if target_revision is None and not candidate_resources:
            log_status("KEEP", f"{style_branch(target_desired_ref)} remains empty")
            return
        if target_revision is None and gate == "pullRequest":
            target_revision = _initialize_gated_desired_ref(
                source_root,
                args.to_environment,
                target_desired_ref,
                current_target,
            )
        target_lease_ref = effect_lease_ref(args.to_environment, target_desired_ref)
        if target_lease_ref == target_desired_ref:
            copy_active_effect_leases(current_target, candidate)
        validate_effect_leases_preserved(
            target_desired_ref,
            target_revision,
            candidate,
            current_target,
            lease_ref=target_lease_ref,
        )

        commit_message = f"Promote {args.from_environment} to {args.to_environment} from {source_desired_revision}"
        title = f"Promote {args.from_environment} to {args.to_environment}"
        body = (
            f"Promotes reconciled desired state from `{source_desired_revision}`. "
            f"After merge, reconcile `{args.to_environment}`."
        )
        outcome: ChangeRequestResult | ManualChangeRequest | None = None
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
            if candidate_ref in {
                source_desired_ref,
                source_observed_ref,
                target_desired_ref,
                target_observed_ref,
            }:
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
            )
            log_status(
                "CANDIDATE",
                f"{style_branch(candidate_ref)} at {describe_revision(change_revision)} targets "
                f"{style_branch(target_desired_ref)}",
            )
        else:
            if args.candidate_ref:
                raise OperationError("--candidate-ref requires changeGate pullRequest")
            candidate_ref = ""
            change_revision = publish_tree(
                target_desired_ref,
                candidate,
                target_revision,
                commit_message,
            )
            log_status(
                "UPDATE",
                f"{style_branch(target_desired_ref)} advanced to {describe_revision(change_revision)}",
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
) -> tuple[str, ChangeRequestResult | ManualChangeRequest | None]:
    load_desired_resource_graph(candidate)
    if current_root is not None:
        validate_desired_resource_transition(current_root, candidate, finalized_resources)
    lease_ref = effect_lease_ref(environment, target_ref)
    validate_effect_leases_preserved(
        target_ref,
        target_revision,
        candidate,
        current_root,
        allow_removed_units,
        lease_ref=lease_ref,
    )
    gate = change_gate(REPOSITORY_ROOT, environment)
    if dry:
        log_status("DRY", f"{style_branch(target_ref)} would receive {title.lower()}")
        return target_revision or "", None
    _ensure_stack_source_pins(environment, candidate)
    if gate == "pullRequest":
        if target_revision is None:
            raise OperationError("pull-request publication requires an initialized desired ref")
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
        )
        log_status(
            "CANDIDATE",
            f"{style_branch(candidate_ref)} at {describe_revision(revision)} targets {style_branch(target_ref)}",
        )
        return revision, outcome
    revision = publish_tree(target_ref, candidate, target_revision, commit_message)
    log_status("UPDATE", f"{style_branch(target_ref)} advanced to {describe_revision(revision)}")
    return revision, None


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
    finalized_incarnation: ResourceIncarnationTombstone | None = None,
) -> None:
    """Keep historical payload while carrying forward the current incarnation identity."""

    historical = load_desired_unit(candidate_path, candidate_path.stem)
    current = load_desired_unit(current_path, current_path.stem) if current_path.is_file() else None
    if current is not None:
        metadata = current.metadata
    elif finalized_incarnation is not None:
        historical_source = getattr(historical.spec, "source", None)
        if not isinstance(historical_source, DesiredSource):
            historical_source = None
        metadata = root_metadata_for_resource(
            historical,
            source=historical_source,
            source_revision=historical_source.revision if historical_source is not None else None,
            previous_finalized_uid=finalized_incarnation.uid,
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
    for unit_path in document_candidates(candidate / "units", unit_name):
        unit_path.unlink()
    selected = DocumentFormat.YAML if current_path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    write_document(
        candidate / "units" / f"{unit_name}{current_path.suffix}",
        serialize_unit_document(current_unit, profile="desired"),
        format=selected,
    )
    copy_unit_materialization(current, candidate, unit_name, current_unit)


def merge_current_cleanup_state(current: Path, candidate: Path) -> None:
    """Carry deletion metadata and opaque cleanup roots through a rollback."""

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
    for resource in resources.values():
        if resource_deletion(resource) is None:
            continue
        source_path = _desired_resource_path(current, resource)
        target_path = _desired_resource_path(candidate, resource)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        if isinstance(resource, UnitResource):
            copy_unit_materialization(current, candidate, resource.name, resource)
    for name in current_blocks:
        if name in current_roots:
            continue
        current_path = unit_document_path(current, name)
        if current_path.is_file():
            copy_current_blocked_unit(current, candidate, name)
    write_desired_transition_blocks(candidate, current_blocks)


def active_teardown_dependents(root: Path, target: UnitResource[Any]) -> tuple[str, ...]:
    """Find active owned or observation-dependent descendants of a teardown target."""

    resources = load_desired_resource_graph(root, validate=False)
    explicit_dependencies = stack_dependency_edges(resources, include_missing=True)
    opaque_roots = load_desired_cleanup_roots(root)
    target_identity = (target.gvk.api_version, target.gvk.kind, target.name, target.metadata.uid or "")
    pending = [target_identity]
    dependents: set[str] = set()
    for opaque_name in opaque_roots:
        if opaque_name != target.name:
            dependents.add(f"{opaque_name} (opaque cleanup root lacks a validated deletion identity)")
    while pending:
        parent_identity = pending.pop()
        parent_names = {parent_identity[2]}
        for _key, child in resources.items():
            child_identity = (child.gvk.api_version, child.gvk.kind, child.name, child.metadata.uid or "")
            if child_identity == parent_identity or child.name in dependents:
                continue
            owner = resource_owner_reference(child)
            owner_matches = (
                owner is not None and (owner.apiVersion, owner.kind, owner.name, owner.uid) == parent_identity
            )
            dependency_matches = isinstance(child, UnitResource) and bool(
                (desired_observation_reference_units(child) | set(explicit_dependencies.get(child.name, ())))
                & parent_names
            )
            if owner_matches or dependency_matches:
                dependents.add(child.name)
                pending.append(child_identity)
    return tuple(sorted(dependents))


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
                + ", ".join(f"{lease.unit_name} by {lease.owner}" for lease in active_leases)
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

        target_inventory = validate_rollback_desired_inventory(target_revision, target, "rollback target")
        current_inventory = (
            validate_rollback_desired_inventory(current_revision, current, "current desired state")
            if mode == "units"
            else None
        )
        target_units = sorted(target_inventory.units)
        requested_units = sorted(set(args.unit or target_units))
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
        merge_current_cleanup_state(current, candidate)
        current_cleanup_names = set(load_desired_cleanup_roots(current))
        finalized_incarnations = load_resource_incarnation_tombstones(candidate)
        for candidate_path in _current_desired_unit_paths(candidate).values():
            candidate_unit = load_desired_unit(candidate_path, candidate_path.stem)
            canonicalize_rollback_unit(
                candidate_path,
                unit_document_path(current, candidate_path.stem),
                finalized_incarnation_for_resource(
                    finalized_incarnations,
                    candidate_unit.gvk.api_version,
                    candidate_unit.gvk.kind,
                    candidate_path.stem,
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
        if candidate_ref in {desired_ref, observed_ref}:
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
        )
        if args.dry:
            print(json.dumps(provenance, indent=2, sort_keys=True))
            return
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)


def _release_stack_pin(environment: str, stack: StackResource) -> None:
    if stack.metadata.uid is None:
        raise OperationError(f"Stack {stack.name!r} has no UID")
    _release_stack_source_pins(environment, stack.name, stack.metadata.uid)


def _release_finalized_stack_pin(
    environment: str,
    name: str,
    uid: str,
    deletion_generation: int,
    desired_root: Path,
) -> bool:
    tombstone = load_resource_incarnation_tombstones(desired_root).get((CORE_API_VERSION, "Stack", name))
    if tombstone is None or tombstone.uid != uid or tombstone.deletion_generation != deletion_generation:
        return False
    return _release_stack_source_pins(environment, name, uid)


def _release_finalized_unit_lease(
    name: str,
    uid: str,
    deletion_generation: int,
    desired_ref: str,
    current_revision: str,
    desired_root: Path,
    lease_ref: str | None,
) -> bool:
    """Release a separate-store lease after desired finalization succeeded."""

    if lease_ref is None:
        return False
    tombstones = load_resource_incarnation_tombstones(desired_root)
    matches = [
        tombstone
        for tombstone in tombstones.values()
        if tombstone.name == name
        and tombstone.uid == uid
        and tombstone.deletion_generation == deletion_generation
        and f"{tombstone.api_version}/{tombstone.kind}" in DRIVER_NAMES_BY_GVK
    ]
    if len(matches) != 1:
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
        raise OperationError(f"effect lease for finalized Unit {name!r} is fenced to another UID")
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


def _command_finalize(args: argparse.Namespace) -> bool:
    category = {
        "unit": "Unit",
        "stack": "Stack",
        "stacktemplate": "StackTemplate",
    }[args.kind.lower()]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.name):
        raise OperationError(f"invalid resource name: {args.name!r}")
    if not isinstance(args.uid, str) or not args.uid:
        raise OperationError("finalize requires --uid")
    if not isinstance(args.deletion_generation, int) or args.deletion_generation < 1:
        raise OperationError("finalize requires --deletion-generation >= 1")
    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, args.observed_ref)
    lease_ref = effect_lease_ref(args.environment, desired_ref)
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        current = temporary / "current"
        current_revision = observed_tree(desired_ref, current)
        if current_revision is None:
            return False
        resources = load_desired_resource_graph(current, validate=False)
        matches = [
            resource
            for resource in resources.values()
            if resource.name == args.name and _resource_matches_category(resource, category)
        ]
        if len(matches) != 1:
            if category == "Stack" and not args.dry:
                return _release_finalized_stack_pin(
                    args.environment,
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    current,
                )
            if category == "Unit" and not args.dry:
                return _release_finalized_unit_lease(
                    args.name,
                    args.uid,
                    args.deletion_generation,
                    desired_ref,
                    current_revision,
                    current,
                    lease_ref,
                )
            return False
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
            raise OperationError(
                "owned resources must be finalized first: " + ", ".join(sorted(child.name for child in children))
            )
        if isinstance(resource, StackResource) and resource.gvk.kind == "StackTemplate":
            referencing_stacks = [
                item.name
                for item in resources.values()
                if isinstance(item, StackResource)
                and item.gvk.kind == "Stack"
                and isinstance(item.spec, (StackSpec, DesiredStackSpec))
                and (
                    item.spec.template == resource.name
                    or (
                        isinstance(item.spec.template, StackTemplateReference)
                        and item.spec.template.name == resource.name
                    )
                )
            ]
            if referencing_stacks:
                raise OperationError("Stacks reference this StackTemplate: " + ", ".join(sorted(referencing_stacks)))

        unit: UnitResource[Any] | None = resource if isinstance(resource, UnitResource) else None
        observed = temporary / "observed"
        lease_acquisition: EffectLeaseAcquisition | None = None
        teardown_details: Mapping[str, object] = {}
        source = getattr(unit.spec, "source", None) if unit is not None else None
        if unit is not None:
            require_unit(unit, unit.name)
            validate_unit_materialization(current, unit.name, unit)
            dependents = active_teardown_dependents(current, unit)
            if dependents:
                raise OperationError("active owned/dependent Units must be finalized first: " + ", ".join(dependents))
            if args.dry:
                log_status(
                    "DRY",
                    f"{style_unit(unit.name)}: teardown would run at generation {deletion.generation}",
                )
                return False
            observed_tree(observed_ref, observed)
            receipt_path = unit_document_path(observed, unit.name)
            previous_receipt = load_receipt(receipt_path, unit.name) if receipt_path.is_file() else None
            existing_evidence = load_teardown_evidence(observed, unit.name, args.uid, deletion.generation)
            if existing_evidence is not None:
                teardown_details = existing_evidence.details
            elif not isinstance(unit.driver, TeardownCapability):
                raise OperationError(f"driver {unit.driver_name} does not support teardown")
            if source is not None and not isinstance(source, DesiredSource):
                raise OperationError(f"retained source identity for {unit.name!r} is invalid")
            source_root = None
            if source is not None and source.revision is not None:
                source_root = temporary / "source"
                materialize_revision(source.revision, source_root)

            def assert_no_dependents(desired_root: Path) -> None:
                latest = load_desired_unit(unit_document_path(desired_root, unit.name), unit.name)
                if latest.metadata.uid != args.uid:
                    raise EffectLeaseUnavailable(f"desired Unit {unit.name!r} changed before finalization")
                active = active_teardown_dependents(desired_root, latest)
                if active:
                    raise EffectLeaseUnavailable("active dependents appeared: " + ", ".join(active))

            if lease_ref is not None:
                lease_acquisition = acquire_effect_lease(
                    desired_ref,
                    current_revision,
                    unit.name,
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
                        previous_receipt=previous_receipt,
                        report=Path(args.report).resolve() if args.report else None,
                        execution=DriverExecution.console(),
                    )
                )
                if result is not None and not isinstance(result, TeardownResult):
                    raise DriverError("teardown returned an invalid result")
                teardown_details = result.details if result is not None else {}
            publish_teardown_observation_cas(
                observed_ref,
                unit.name,
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
        path = _desired_resource_path(candidate, resource)
        for candidate_path in document_candidates(path.parent, resource.name):
            candidate_path.unlink()
        if unit is not None:
            materialization = getattr(unit.spec, "materialization", None)
            if materialization is not None:
                materialized_path = candidate / materialization.path
                if materialized_path.is_dir():
                    shutil.rmtree(materialized_path)
            remove_effect_lease(candidate, resource.name)
        write_resource_incarnation_tombstone(
            candidate,
            ResourceIncarnationTombstone(
                api_version=resource.gvk.api_version,
                kind=resource.gvk.kind,
                name=resource.name,
                uid=args.uid,
                deletion_generation=deletion.generation,
            ),
        )
        load_desired_resource_graph(candidate)
        candidate_id = candidate_identifier(
            "finalize",
            args.environment,
            candidate,
            desired_ref,
            current_revision,
            {"kind": category, "name": args.name, "uid": args.uid, "deletionGeneration": deletion.generation},
        )
        candidate_ref = resolve_candidate_ref(
            REPOSITORY_ROOT, args.environment, "finalize", candidate_id, args.candidate_ref
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
            frozenset({resource.name}) if unit is not None else frozenset(),
            request_change=False,
            finalized_resources=frozenset(
                {
                    ResourceFinalizationFence(
                        resource.gvk.api_version,
                        resource.gvk.kind,
                        resource.name,
                        args.uid,
                        deletion.generation,
                    )
                }
            ),
        )
        if not args.dry and isinstance(resource, StackResource) and resource.gvk.kind == "Stack" and outcome is None:
            _release_stack_pin(args.environment, resource)
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
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)
        return True


def command_finalize(args: argparse.Namespace) -> bool:
    with unit_effect_lock(args.environment, getattr(args, "name", "<invalid>")):
        return _command_finalize(args)


def command_recover_effect_lease(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    if not args.confirm_stopped:
        raise OperationError("lease recovery requires --confirm-stopped")
    desired_ref, _observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        args.desired_ref,
        None,
    )
    with unit_effect_lock(args.environment, args.unit):
        revision = recover_effect_lease(
            desired_ref,
            args.unit,
            args.uid,
            args.token,
            lease_ref=effect_lease_ref(args.environment, desired_ref),
        )
    if revision is not None:
        print(revision)


def command_resolve_desired(args: argparse.Namespace) -> None:
    revision = resolve_ref(args.desired_ref, args.desired_revision)
    print(revision)


def _selected_stack_template_resources(
    stack_name: str,
    resources: Sequence[StackTemplateResource],
    selected: str | None,
) -> tuple[StackTemplateResource, ...]:
    """Select a dependency-closed Unit projection from a StackTemplate."""

    if selected is None:
        return tuple(resources)
    names = [name.strip() for name in selected.split(",") if name.strip()]
    if not names or len(set(names)) != len(names):
        raise OperationError("--units must contain one or more unique Unit template names")
    by_name = {resource.name: resource for resource in resources}
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise OperationError(f"Stack {stack_name!r} selects unknown Unit templates: {', '.join(unknown)}")
    selected_names = set(names)
    for resource in resources:
        if resource.name in selected_names:
            omitted = sorted(set(resource.dependsOn) - selected_names)
            if omitted:
                raise OperationError(
                    f"Stack {stack_name!r} selects {resource.name!r} but omits dependencies: {', '.join(omitted)}"
                )
    return tuple(resource for resource in resources if resource.name in selected_names)


def _stack_source_pin_prefix(environment: str, stack_name: str, uid: str) -> str:
    """Return the controller-pin namespace owned by one Stack incarnation."""

    return f"stacks/{environment}/{stack_name}/{uid}/"


def _stack_source_pin_name(environment: str, stack_name: str, uid: str, revision: str) -> str:
    """Name one immutable repository-local StackTemplate source pin."""

    return f"{_stack_source_pin_prefix(environment, stack_name, uid)}{revision}"


def _required_stack_source_pins(environment: str, desired_root: Path) -> tuple[tuple[str, str], ...]:
    """Collect exact local source commits referenced by desired Stacks.

    Remote Git sources cannot be retained by refs in the deployment repository;
    their recorded remote and commit remain the external durability contract.
    """

    required: set[tuple[str, str]] = set()
    for resource in load_desired_resource_graph(desired_root, validate=False).values():
        if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
            continue
        if not isinstance(resource.spec, DesiredStackSpec) or resource.spec.resolvedSource is None:
            continue
        source = resource.spec.resolvedSource.fromGit
        if source.path is None:
            continue
        if resource.metadata.uid is None:
            raise OperationError(f"Stack {resource.name!r} has no UID")
        required.add(
            (
                _stack_source_pin_name(environment, resource.name, resource.metadata.uid, source.commit),
                source.commit,
            )
        )
    return tuple(sorted(required))


def _ensure_stack_source_pins(environment: str, desired_root: Path) -> tuple[ControllerPin, ...]:
    """Retain every repository-local template commit before desired publication."""

    required = _required_stack_source_pins(environment, desired_root)
    if not required:
        return ()
    return state_store().create_controller_pins(dict(required))


def _release_stack_source_pins(environment: str, stack_name: str, uid: str) -> bool:
    """Release all exact source pins owned by one finalized Stack incarnation."""

    prefix = _stack_source_pin_prefix(environment, stack_name, uid)
    store = state_store()
    pins = tuple(pin for pin in store.list_controller_pins() if pin.name.startswith(prefix))
    for pin in pins:
        suffix = pin.name.removeprefix(prefix)
        if not re.fullmatch(r"[0-9a-f]{40}", suffix) or suffix != pin.revision:
            raise OperationError(f"Stack {stack_name!r}: controller source pin has an invalid identity")
    for pin in pins:
        store.release_controller_pin(pin.name, pin.revision)
    return bool(pins)


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
        if args.unit is not None:
            status_names = {unit_name for unit_name, _status, _reason in statuses}
            if args.unit not in status_names:
                available = ", ".join(sorted(status_names)) or "none"
                raise OperationError(
                    f"unknown unit {args.unit!r} for environment {args.environment!r}; available units: {available}"
                )
            statuses = [item for item in statuses if item[0] == args.unit]
        log_reconciliation_status(
            args.environment,
            statuses,
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
        requested = args.unit or sorted(available)
        invalid = sorted({unit_name for unit_name in requested if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", unit_name)})
        if invalid:
            raise OperationError(f"invalid unit name(s): {', '.join(invalid)}")
        selected = sorted(set(requested))
        unknown = sorted(set(selected) - available.keys())
        if unknown:
            choices = ", ".join(sorted(available)) or "none"
            raise OperationError(
                f"{desired_ref} has no materialized unit(s): {', '.join(unknown)}; available units: {choices}"
            )
        if not selected:
            raise OperationError(f"{desired_ref} has no materialized units")
        ensure_desired_units_materialized(desired)
        load_desired_resource_graph(desired)

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
            log_status("VERIFY", f"{style_unit(unit_name)} ({driver_name})")
            source_root = temporary / "sources" / unit_name if source is not None else None
            if source is not None:
                assert source.revision is not None
                assert source_root is not None
                materialize_revision(source.revision, source_root)
            result = VERIFICATION_DRIVERS[driver_name].verify(
                VerificationContext(
                    environment=args.environment,
                    desired_root=desired,
                    desired_revision=desired_revision,
                    source_root=source_root,
                    source_revision=source.revision if source is not None else None,
                    source_path=source.path if source is not None else None,
                    unit_name=unit_name,
                    unit=unit.spec,
                    execution=DriverExecution.console(),
                )
            )
            if result.status is VerificationStatus.CLEAN:
                log_status("CLEAN", style_unit(unit_name))
            elif result.status is VerificationStatus.DRIFT:
                drifted.append(unit_name)
                log_status("DRIFT", style_unit(unit_name))
            else:
                raise DriverError(f"{driver_name} returned an invalid verification status: {result.status!r}")

    if drifted:
        log_status("RESULT", f"DRIFT: {style_units(drifted)}")
        raise OperationError(f"verification detected drift in: {', '.join(drifted)}")
    log_status("RESULT", "CLEAN")


def require_unit(unit: UnitResource[Any], unit_name: str) -> tuple[str, DesiredSource | None]:
    if unit.name != unit_name:
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
    """Serialize reconcile/finalize effects for one environment and Unit."""

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


def reference_paths(
    value: object,
    reference_type: str,
    pointer: str = "",
    current_unit: str | None = None,
) -> set[str]:
    """Collect validated logical unit names referenced by one reference type."""
    if reference_type not in {"fromReceipt", "fromArtifact", "fromPromotion"}:
        raise OperationError(f"unknown reference type: {reference_type}")
    found: set[str] = set()
    for reference in template_references(_template(value, pointer)):
        if isinstance(reference, ReceiptReference) and reference_type == "fromReceipt":
            found.add(reference.fromReceipt.unit)
        elif isinstance(reference, ArtifactReferenceExpression) and reference_type == "fromArtifact":
            found.add(reference.fromArtifact.unit)
        elif isinstance(reference, PromotionReference) and reference_type == "fromPromotion":
            unit = reference.fromPromotion.unit or current_unit
            if unit is None:
                raise OperationError("implicit fromPromotion unit requires the current unit name")
            found.add(unit)
    return found


def _json_pointer_child(pointer: str, child: str | int) -> str:
    token = str(child).replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{token}"


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


def log_dependency_graph(graph: Mapping[str, tuple[str, ...]]) -> None:
    for unit_name, dependencies in graph.items():
        log_status(
            "DEPEND",
            f"{style_unit(unit_name)}: {', '.join(style_unit(dependency) for dependency in dependencies) or 'none'}",
        )


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
            if receipt.name != unit_name:
                raise OperationError(f"candidate receipt name is not {unit_name!r}")
            validate_artifact_output_identity(driver, unit, artifact_documents)
            receipt_path = unit_document_path(observed, unit_name)
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
                )
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not update {observed_ref} after concurrent updates")


def write_reconcile_outputs(changed: bool, desired_revision: str = "") -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"reconciled={'true' if changed else 'false'}\n")
            stream.write(f"desired_changed={'true' if desired_revision else 'false'}\n")
            stream.write(f"desired_revision={desired_revision}\n")


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


def _command_reconcile(args: argparse.Namespace) -> bool:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    verbose = getattr(args, "verbose", False)

    def detail(status: str, message: str) -> None:
        if verbose:
            log_status(status, message)

    load_environment(REPOSITORY_ROOT, args.environment)
    if verbose:
        log_heading(f"Reconcile {style_unit(args.unit)}")
        log_status("START", f"environment {style_environment(args.environment)}")
        log_status("MODE", "plan" if args.plan else "apply")
    else:
        log_heading(f"{style_unit(args.unit)} · {style_environment(args.environment)}")
    report = Path(args.report).resolve() if args.report else None
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        observed = temporary / "observed"
        desired_ref, observed_ref = deployment_refs(
            REPOSITORY_ROOT,
            args.environment,
            args.desired_ref,
            args.observed_ref,
        )
        lease_ref = effect_lease_ref(args.environment, desired_ref)
        detail("REFS", f"desired {style_branch(desired_ref)}; observed {style_branch(observed_ref)}")
        observed_revision = observed_tree(observed_ref, observed)
        desired_revision = resolve_ref(desired_ref, args.desired_revision)
        materialize_revision(desired_revision, desired)
        detail("DESIRED", f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}")
        detail(
            "OBSERVED",
            f"{style_branch(observed_ref)} at {describe_revision(observed_revision)}"
            if observed_revision
            else f"{style_branch(observed_ref)} has no receipts yet",
        )
        if transition_reason := load_desired_transition_blocks(desired).get(args.unit):
            log_status("WAIT", transition_reason)
            log_status("DONE", f"{style_unit(args.unit)}: no changes")
            write_reconcile_outputs(False)
            return False
        deletion_path = unit_document_path(desired, args.unit)
        if deletion_path.is_file():
            deleting_unit = load_desired_unit(deletion_path, args.unit)
            if resource_deletion(deleting_unit) is not None:
                log_status("WAIT", deletion_reason(deleting_unit))
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
                return False
        unit_path = unit_document_path(desired, args.unit)
        if not unit_path.is_file():
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(args.unit)}: no changes")
            write_reconcile_outputs(False)
            return False
        unit = load_desired_unit(unit_path, args.unit)
        ensure_desired_units_materialized(desired)
        load_desired_resource_graph(desired)
        if raw_unit_contains_reference(load_json(unit_path)):
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(args.unit)}: no changes")
            write_reconcile_outputs(False)
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
            log_status("DONE", f"{style_unit(args.unit)}: materialized for external delivery")
            write_reconcile_outputs(False)
            return False

        unit_blob = file_blob(unit_path)
        receipt_path = unit_document_path(observed, args.unit)
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
                    log_status("DONE", f"{style_unit(args.unit)}: clean")
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
                write_reconcile_outputs(False)
                return False

        source_root = temporary / "source" if source is not None else None
        if not args.plan:
            assert unit.metadata.uid is not None
            assert_desired_ref_fence(desired_ref, desired_revision, args.unit, unit.metadata.uid)
        if source is not None:
            assert source.revision is not None
            assert source_root is not None
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
                log_status("WAIT", f"{style_unit(args.unit)}: {exc}")
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
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
                        f"{style_unit(args.unit)}: pre-effect lease release failed; explicit recovery remains: "
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
        heartbeat: EffectLeaseHeartbeat | None = None
        driver_started = False
        if lease_acquisition is not None:
            try:
                heartbeat = start_effect_lease_heartbeat(desired_ref, lease_acquisition, lease_ref=lease_ref)
            except BaseException:
                try:
                    release_pre_effect_lease(desired_ref, lease_acquisition, lease_ref=lease_ref)
                except Exception as release_exc:
                    log_status(
                        "WAIT",
                        f"{style_unit(args.unit)}: pre-effect lease release failed; explicit recovery remains: "
                        f"{release_exc}",
                    )
                raise
        try:
            execution: dict[str, Any] = {
                "environment": args.environment,
                "desired_root": desired,
                "desired_revision": desired_revision,
                "source_root": source_root,
                "source_revision": source.revision if source is not None else None,
                "source_path": source.path if source is not None else None,
                "unit_name": args.unit,
                "unit": unit.spec,
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
                    log_status("DONE", f"{style_unit(args.unit)}: no remote changes")
                else:
                    log_status("PLAN", "SUCCEEDED")
                    log_status("EFFECTS", "None; planning does not change remote state")
                write_reconcile_outputs(False)
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
            if heartbeat is not None:
                try:
                    heartbeat.stop()
                except Exception:
                    pass
            if lease_acquisition is not None and not driver_started:
                try:
                    release_pre_effect_lease(desired_ref, lease_acquisition, lease_ref=lease_ref)
                except Exception as release_exc:
                    log_status(
                        "WAIT",
                        f"{style_unit(args.unit)}: pre-effect lease release failed; explicit recovery remains: "
                        f"{release_exc}",
                    )
            log_compact_failure()
            raise
        if heartbeat is not None:
            try:
                lease_acquisition = heartbeat.stop()
            except EffectLeaseUnavailable as exc:
                log_status("WAIT", f"{style_unit(args.unit)}: {exc}; reconciliation result was not published")
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
                return False
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
                log_status("WAIT", f"{style_unit(args.unit)}: {exc}; reconciliation result was not published")
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
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
                    name=args.unit,
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
                log_status("WAIT", f"{style_unit(args.unit)}: effect lease release pending expiry: {exc}")
                write_reconcile_outputs(True)
                if verbose:
                    log_status("DONE", f"{style_unit(args.unit)}: reconciled successfully; lease release deferred")
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
        write_reconcile_outputs(True)
        if verbose:
            log_status("DONE", f"{style_unit(args.unit)}: reconciled successfully")
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
) -> None:
    log_heading(f"Convergence result for {style_environment(environment)}")
    if result == "CLEAN":
        driver_summary = f"drivers ran for {style_units(steps)}" if steps else "no drivers ran"
        log_status("RESULT", f"CLEAN: {len(scope)}/{len(scope)} units; {driver_summary}")
    else:
        log_status("RESULT", result)
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{style_unit(unit_name)}: {status.lower()}; {reason}")


def command_dependencies(args: argparse.Namespace) -> None:
    source_revision = git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_root = Path(temporary_directory) / "source"
        materialize_revision(source_revision, source_root)
        current_desired = Path(temporary_directory) / "current-desired"
        current_desired.mkdir()
        specifications, stack_dependencies = load_convergence_specifications(
            source_root,
            args.environment,
            current_desired,
            source_revision,
            Path(temporary_directory) / "stack-projection",
        )
        selection = convergence_scope(specifications, args.unit, args.depth, stack_dependencies)
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
        print("\n".join(order))
        return
    for index, target in enumerate(targets):
        if index:
            print()
        print("\n".join(graph.render_tree(target, lambda unit_name: style_unit(unit_name, sys.stdout))))


def _partition_unit_names(desired: Path, partition: str) -> list[str]:
    resources = load_desired_resource_graph(desired)
    selected: set[tuple[str, str, str]] = set()
    for resource in resources.values():
        if resource_owner_reference(resource) is None and resource.metadata.partition == partition:
            selected.update(
                (item.gvk.api_version, item.gvk.kind, item.name)
                for item in _owned_resource_closure(resources, resource)
            )
    return sorted(
        resource.name
        for key, resource in resources.items()
        if key in selected and isinstance(resource, UnitResource) and resource_deletion(resource) is None
    )


def _desired_convergence_model(
    desired: Path,
) -> tuple[dict[str, UnitResource[Any]], dict[str, tuple[str, ...]]]:
    resources = load_desired_resource_graph(desired)
    units = {
        resource.name: resource
        for resource in resources.values()
        if isinstance(resource, UnitResource) and resource_deletion(resource) is None
    }
    return units, stack_dependency_edges(resources)


def command_converge(args: argparse.Namespace) -> None:
    """Converge persisted desired Units, optionally reapplying one input snapshot."""

    if args.max_steps is not None and args.max_steps < 1:
        raise OperationError("--max-steps must be a positive integer")
    if args.partition is not None:
        _resource_name(args.partition, "partition name")
    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref, args.observed_ref)
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
    previous_ready: set[str] = set()
    max_steps = args.max_steps
    iteration = 0
    with tempfile.TemporaryDirectory(prefix="gitopsctr-converge-") as temporary_directory:
        temporary = Path(temporary_directory)
        while True:
            iteration += 1
            if apply_arguments is not None:
                applied_revision = command_apply(apply_arguments)
                target_revision = fetch_ref(desired_ref)
                if applied_revision is not None and applied_revision != target_revision:
                    raise OperationError(
                        "apply produced a reviewed candidate; merge it before converging the target desired state"
                    )
            desired_revision = fetch_ref(desired_ref)
            if desired_revision is None:
                raise OperationError(f"desired ref {desired_ref!r} has no state; apply resources first")
            desired = temporary / f"desired-{iteration}"
            observed = temporary / f"observed-{iteration}"
            materialize_revision(desired_revision, desired)
            observed_tree(observed_ref, observed)
            specifications, stack_dependencies = _desired_convergence_model(desired)
            selected_units = _partition_unit_names(desired, args.partition) if args.partition is not None else args.unit
            if args.partition is not None and not selected_units:
                raise OperationError(f"partition {args.partition!r} selects no desired Units")
            selection = convergence_scope(specifications, selected_units, additional_dependencies=stack_dependencies)
            scope = selection.scope
            order = convergence_order(specifications, scope, stack_dependencies)
            if max_steps is None:
                max_steps = max(2, 2 * len(scope))
            statuses = reconciliation_statuses(scope, desired, observed)
            if args.verbose:
                log_reconciliation_status(args.environment, statuses, desired_revision, desired, observed, True)
            if all(status in {"CLEAN", "MATERIALIZED"} for _, status, _ in statuses):
                log_compact_convergence_summary(args.environment, scope, steps, "CLEAN")
                return
            ready = [
                name for name in order if dict((name, status) for name, status, _ in statuses).get(name) == "READY"
            ]
            if args.fail_on_repeat and any(name in previous_ready for name in ready):
                repeated = sorted(name for name in ready if name in previous_ready)
                raise OperationError("convergence heuristic detected repeated ready unit(s): " + ", ".join(repeated))
            previous_ready.update(ready)
            if not ready:
                waiting = [f"{name} ({reason})" for name, status, reason in statuses if status == "WAIT"]
                raise OperationError("convergence stalled with no ready unit: " + ", ".join(waiting))
            if len(steps) >= max_steps:
                raise OperationError(f"convergence did not finish within {max_steps} reconciliation steps")
            unit_name = ready[0]
            if not args.yes:
                require_reconciliation_approval(unit_name)
            command_reconcile(
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
                )
            )
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


def _source_resource_path(kind: str, name: str, environment: str | None, project: Any) -> Path:
    if kind == "StackTemplate":
        return REPOSITORY_ROOT.joinpath(*project.stack_templates_path.parts) / f"{name}{project.write_format.suffix}"
    if environment is None:
        raise OperationError(f"{kind} operations require --environment")
    environment_root = project_environment_root(REPOSITORY_ROOT, environment)
    directory = {"Stack": "stacks", "Unit": "units"}.get(kind)
    if directory is None:
        raise OperationError(f"source operations do not support {kind}")
    return environment_root / directory / f"{name}{project.write_format.suffix}"


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
) -> list[tuple[str, JsonObject]]:
    """Load a deterministic resource stream from live or revision-backed paths."""

    _validate_apply_input_selection(
        files,
        source_revision,
        operation=operation,
        revision_option=revision_option,
    )
    loaded: list[tuple[str, JsonObject]] = []
    paths: list[Path] = []
    for value in files:
        if value == "-":
            try:
                values = list(yaml.safe_load_all(sys.stdin.read()))
            except yaml.YAMLError as exc:
                raise OperationError(f"standard input is invalid YAML: {exc}") from exc
            for index, value_document in enumerate(values, 1):
                if value_document is None:
                    continue
                try:
                    document = require_json_value(value_document)
                except ValueError as exc:
                    raise OperationError(f"standard input document {index} is invalid: {exc}") from exc
                if not isinstance(document, dict):
                    raise OperationError(f"standard input document {index} must be a resource mapping")
                loaded.append((f"stdin#{index}", document))
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
            loaded.append((str(path), RESOURCE_CATALOG.load_document(path)))
        except (DocumentFormatError, OperationError, OSError, RuntimeError) as exc:
            raise OperationError(f"{path}: {exc}") from exc
    identities: dict[tuple[str, str], str] = {}
    for origin, document in loaded:
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
    documents: Sequence[tuple[str, JsonObject]],
) -> tuple[list[UnitResource[Any]], list[StackResource]]:
    environment_root = project_environment_root(source, environment)
    environment_root.mkdir(parents=True, exist_ok=True)
    units: list[UnitResource[Any]] = []
    stacks: list[StackResource] = []
    for origin, document in documents:
        if _document_is_canonical_desired(document):
            continue
        kind = document.get("kind")
        metadata = document.get("metadata")
        assert isinstance(metadata, dict)
        name = cast(str, metadata["name"])
        if kind == "Stack":
            resource = RESOURCE_CATALOG.parse_stack(document, profile="authored", expected_name=name)
            stacks.append(resource)
            directory = environment_root / "stacks"
        elif kind == "StackTemplate":
            raise OperationError(f"{origin}: apply StackTemplate directly is not supported; apply a Stack")
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
    for stack in stacks:
        assert isinstance(stack.spec, StackSpec)
        template = _stack_template_reference(stack.spec)
        requires_revision = isinstance(template.source, StackTemplateFromResource)
        if isinstance(template.source, StackTemplateFromGit):
            request = template.source.fromGit
            requires_revision = request.path == "." and request.commit is None and request.ref is None
        if requires_revision:
            raise OperationError(
                f"Stack {stack.name!r} uses repository-backed StackTemplate {template.name!r}; "
                "apply it with --source-revision <commit>"
            )


def _applied_root_closure(
    candidate: Path,
    roots: Sequence[UnitResource[Any] | StackResource],
) -> frozenset[tuple[str, str, str]]:
    """Return physical roots applied by this request, including selected resource templates."""

    candidate_resources = load_desired_resource_graph(candidate, validate=False)
    applied = {(resource.gvk.api_version, resource.gvk.kind, resource.name) for resource in roots}
    for resource in roots:
        if not isinstance(resource, StackResource) or resource.gvk.kind != "Stack":
            continue
        projected = candidate_resources.get((resource.gvk.api_version, resource.gvk.kind, resource.name))
        if not isinstance(projected, StackResource) or not isinstance(projected.spec, DesiredStackSpec):
            raise OperationError(f"applied Stack {resource.name!r} was not projected into desired state")
        template = cast(StackTemplateReference, projected.spec.template)
        if not isinstance(template.source, StackTemplateFromResource):
            continue
        template_key = (CORE_API_VERSION, "StackTemplate", template.name)
        selected_template = candidate_resources.get(template_key)
        if not isinstance(selected_template, StackResource) or selected_template.gvk.kind != "StackTemplate":
            raise OperationError(
                f"applied Stack {resource.name!r} selected StackTemplate {template.name!r}, but it was not persisted"
            )
        applied.add(template_key)
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
            selected_key = (selected.gvk.api_version, selected.gvk.kind, selected.name)
            copied.add(selected_key)
            if selected_key in candidate_resources:
                continue
            source_path = _desired_resource_path(current, selected)
            target = candidate / source_path.relative_to(current)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            if isinstance(selected, UnitResource) and getattr(selected.spec, "materialization", None) is not None:
                copy_unit_materialization(current, candidate, selected.name, selected)


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
    candidate_resources = load_desired_resource_graph(candidate, validate=False)
    for key, resource in current_resources.items():
        if resource_owner_reference(resource) is not None or resource.metadata.partition != partition or key in applied:
            continue
        for selected in _owned_resource_closure(current_resources, resource):
            selected_target = candidate_resources.get(
                (selected.gvk.api_version, selected.gvk.kind, selected.name), selected
            )
            _write_desired_resource(
                candidate / _desired_resource_path(current, selected).relative_to(current),
                mark_resource_for_deletion(selected_target),
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
        authored_units, authored_stacks = _write_apply_authored_documents(apply_source, args.environment, documents)
        _validate_apply_source_revision(source_revision, authored_units, authored_stacks)
        current_revision = observed_tree(desired_ref, current)
        observed_revision = observed_tree(observed_ref, observed)
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
        )
        applied = set(_applied_root_closure(candidate, [*authored_units, *authored_stacks]))
        for origin, document in documents:
            if not _document_is_canonical_desired(document):
                continue
            kind = cast(str, document["kind"])
            metadata = cast(dict[str, Any], document["metadata"])
            name = cast(str, metadata["name"])
            if kind in {"Stack", "StackTemplate"}:
                resource = (
                    RESOURCE_CATALOG.parse_stack(document, profile="desired", expected_name=name)
                    if kind == "Stack"
                    else RESOURCE_CATALOG.parse_stack_template(document, profile="desired", expected_name=name)
                )
            else:
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
        load_desired_resource_graph(candidate)
        if current_revision is None and not directory_files(candidate):
            log_status("KEEP", f"{style_branch(desired_ref)} remains empty")
            return None
        if current_revision is None and change_gate(source, args.environment) == "pullRequest" and not args.dry:
            current_revision = _initialize_gated_desired_ref(source, args.environment, desired_ref, current)
        if current_revision is not None and directory_files(current) == directory_files(candidate):
            if not args.dry:
                _ensure_stack_source_pins(args.environment, current)
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


def _desired_resource_path(root: Path, resource: UnitResource[Any] | StackResource) -> Path:
    if isinstance(resource, UnitResource):
        return unit_document_path(root, resource.name)
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
            for resource in resources.values()
            if resource.name == args.name
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
        if candidate_ref in {desired_ref, observed_ref}:
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
                "finalize",
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
        help="unit to roll back; repeat for multiple units (defaults to the full tree)",
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
    recover_effect_lease.add_argument("--unit", required=True)
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

    finalize = commands.add_parser(
        "finalize",
        help="finalize one resource marked for deletion",
    )
    finalize.add_argument("--environment", required=True)
    finalize.add_argument("kind", type=str.lower, choices=("unit", "stack", "stacktemplate"))
    finalize.add_argument("--name", required=True)
    finalize.add_argument("--uid", required=True, help="expected resource UID fence")
    finalize.add_argument(
        "--deletion-generation",
        required=True,
        type=int,
        help="expected deletion generation fence",
    )
    finalize.add_argument("--desired-ref", help="override the environment's desired ref")
    finalize.add_argument("--observed-ref", help="override the environment's observed ref")
    finalize.add_argument(
        "--candidate-ref",
        help="exact candidate ref override when the environment uses a pull-request change gate",
    )
    finalize.add_argument("--report", help="directory where the driver may write teardown reports")
    finalize.add_argument("--dry", action="store_true")
    finalize.set_defaults(handler=command_finalize)

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
    status.add_argument("--unit", help="limit detailed status to one unit in the selected environment")
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
    get.add_argument("name", nargs="?", help="exact resource name; omit to list the selected family")
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
    get.add_argument("-o", "--output", choices=("table", "yaml", "json"), default="table")
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
        help="unit to verify; repeat for multiple units (defaults to all desired units)",
    )
    verify.set_defaults(handler=command_verify)

    reconcile = commands.add_parser(
        "reconcile",
        help="reconcile one deployment unit",
    )
    reconcile.add_argument(
        "--unit",
        required=True,
        help="unit name under units/ in the desired ref",
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
        help="target unit; repeat to show multiple dependency trees",
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
        help="target unit to converge; repeat for multiple targets (defaults to all units)",
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
