"""Resolve deployment state and run registered reconciliation drivers.

Desired and observed JSON remain the contract; GitHub Actions and local callers use the same CLI.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TextIO, TypedDict, cast

import yaml

from gitopsctr.api import GVK, ApiError
from gitopsctr.artifacts import require_artifact_api
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    ArtifactDescriptor,
    AuthoredSource,
    DesiredSource,
    MaterializationDocument,
    ReceiptDesired,
    StrictModel,
    with_schema,
)
from gitopsctr.dependencies import (
    convergence_order,
    convergence_scope,
    dependency_graph,
    downstream_unit_closure,
    observation_reference_units,
)
from gitopsctr.document import ContractError, DocumentContract, JsonObject, JsonObjectValue
from gitopsctr.driver import (
    DriverError,
    MaterializationContext,
    MaterializationResult,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    ReconciliationOutput,
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
    UnitResource,
    validate_desired_resource_graph,
)
from gitopsctr.schemas import encoded_schema, export_schemas, resource_schema_url, show_schema
from gitopsctr.state import GitStateStore
from gitopsctr.templates import (
    ArtifactReference as ArtifactReferenceExpression,
)
from gitopsctr.templates import (
    ArtifactReferenceTarget,
    PromotionReference,
    ReceiptReference,
    TemplateError,
    TemplateValue,
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


@dataclass(frozen=True)
class PromotionContext:
    source_environment: str
    desired_ref: str
    desired_revision: str
    observed_ref: str
    observed_revision: str | None
    specification_revision: str
    desired_root: Path

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
class RefAdvance:
    kind: str
    ref: str
    before: str | None
    after: str
    unit: str | None = None


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


class UnitStatusSnapshot(TypedDict):
    unit: str
    status: str
    reason: str


class EnvironmentSnapshot(TypedDict):
    desired: RevisionSnapshot
    observed: RevisionSnapshot
    statuses: list[UnitStatusSnapshot]


class EnvironmentRow(EnvironmentSnapshot):
    environment: str
    counts: dict[str, int]


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
    if status.upper() == "RESULT":
        result = message.split(":", 1)[0].strip().upper()
        if result == "FAILED":
            return "error"
        if result == "DRIFT":
            return "warning"
        if result in {"CLEAN", "VALID"}:
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
strict_resource_documents = RESOURCE_CATALOG.strict_resource_documents


def load_authored_unit(path: Path, expected_name: str | None = None) -> UnitResource[Any]:
    return RESOURCE_CATALOG.load_unit(path, expected_name, profile="authored")


def load_desired_unit(path: Path, expected_name: str | None = None) -> UnitResource[Any]:
    return RESOURCE_CATALOG.load_unit(path, expected_name, profile="desired")


def persisted_unit_driver_name(path: Path) -> str | None:
    """Inspect only envelope identity when an obsolete payload cannot be parsed."""

    document = RESOURCE_CATALOG.load_document(path)
    if document.get("apiVersion") is None:
        value = document.get("driver")
        return value if isinstance(value, str) else None
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
    """Read legacy-compatible source identity without treating it as a desired model."""

    document = RESOURCE_CATALOG.load_document(path)
    specification = document.get("spec", document)
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
    """Write canonical desired state while retaining legacy-path format when unconfigured."""

    if resource_documents_enabled(project_root):
        return write_unit(path, unit, project_root)
    selected = DocumentFormat.YAML if path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
    return write_document(path, serialize_unit_document(unit, profile="desired"), format=selected)


def load_desired_resource_graph(root: Path) -> dict[tuple[str, str, str], UnitResource[Any]]:
    """Load and validate every desired Unit in one desired ref before effects."""

    resources: dict[tuple[str, str, str], UnitResource[Any]] = {}
    for unit_name, path in _current_desired_unit_paths(root).items():
        unit = load_desired_unit(path, unit_name)
        key = (unit.gvk.api_version, unit.gvk.kind, unit.name)
        if key in resources:
            raise OperationError(f"duplicate desired resource identity: {key!r}")
        resources[key] = unit
    try:
        validate_desired_resource_graph(resources)
    except ValueError as exc:
        raise OperationError(f"invalid desired resource graph: {exc}") from exc
    return resources


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
    plugin = unit.driver
    if not isinstance(plugin, ReconciliationCapability):
        return False
    try:
        return plugin.reconciliation_required(unit.spec)
    except DriverError as exc:
        raise OperationError(str(exc)) from exc


def materialization_tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise OperationError("materialization output must be a directory")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise OperationError(f"materialization output contains a symbolic link: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise OperationError(f"materialization output contains a non-file: {path.relative_to(root)}")
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                "contentHash": hashlib.sha256(content).hexdigest(),
            }
        )
    if not entries:
        raise OperationError("materialization output is empty")
    payload = {"materializationHashVersion": 1, "files": entries}
    return f"sha256:{hashlib.sha256(canonical_json(payload)).hexdigest()}"


def validate_unit_materialization(desired_root: Path, unit_name: str, unit: UnitResource[Any]) -> None:
    plugin_name = unit.driver_name
    expects_materialization = plugin_name in MATERIALIZATION_DRIVERS
    descriptor = getattr(unit.spec, "materialization", None)
    if not expects_materialization:
        if descriptor is not None:
            raise OperationError(f"{unit_name} records materialization for a plugin without that capability")
        return
    if descriptor is None:
        raise OperationError(f"{unit_name} has an invalid materialization descriptor")
    expected_path = f"materialized/{unit_name}"
    if descriptor.path != expected_path:
        raise OperationError(f"{unit_name} materialization path must be {expected_path}")
    digest = descriptor.digest
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise OperationError(f"{unit_name} has an invalid materialization digest")
    if not descriptor.mediaType:
        raise OperationError(f"{unit_name} has an invalid materialization media type")
    actual = materialization_tree_digest(desired_root / expected_path)
    if actual != digest:
        raise OperationError(f"{unit_name} materialized payload does not match its digest")


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
    legacy: dict[str, Any] | None,
) -> tuple[str, str] | None:
    current_path = unit_document_path(current_desired, unit_name)
    if current_path.is_file():
        source = persisted_unit_source_identity(current_path)
        revision = source.revision
        input_hash = source.input_hash
        if isinstance(revision, str) and isinstance(input_hash, str):
            return revision, input_hash
    if legacy is not None and unit_name in {"application-images", "aws-application"}:
        section = "terraform" if unit_name == "aws-application" else ""
        revision = (
            legacy.get(section, {}).get("revision")
            if section
            else legacy.get("source_revision") or legacy.get("application_revision")
        )
        if isinstance(revision, str):
            return revision, ""
    return None


def file_blob(path: Path) -> str:
    return git("hash-object", str(path)).stdout.strip()


def sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


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
        if isinstance(metadata, dict) and metadata.get("name") != name:
            log_status(
                "WARN",
                f"{driver_name} artifact {name!r} has resource name {metadata.get('name')!r}",
            )
        if (
            not isinstance(metadata, dict)
            or not isinstance(producer, dict)
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
    producer = document.get("producer")
    metadata = document.get("metadata")
    source = getattr(unit.spec, "source", None)
    if isinstance(metadata, dict) and metadata.get("name") != artifact_name:
        log_status(
            "WARN",
            f"persisted {driver_name} artifact {artifact_name!r} has resource name {metadata.get('name')!r}",
        )
    if (
        not isinstance(metadata, dict)
        or not isinstance(producer, dict)
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
        if observed_revision is None:
            raise ReferenceUnavailable(f"receipt does not exist: {reference.unit}")
        receipt = current_receipt(observed, candidate / "units", reference.unit)
        if receipt is None:
            raise ReferenceUnavailable(f"receipt is stale: {reference.unit}")
        validate_artifact_reference_target(reference)
        producer_unit = load_desired_unit(unit_document_path(candidate, reference.unit), reference.unit)
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

    return resolve_template_value(
        value,
        ResolutionContext(
            receipt=resolve_receipt,
            artifact=resolve_artifact,
            promotion=resolve_promotion,
            unit=target_unit,
            dry=dry,
        ),
        pointer,
    )


@dataclass(frozen=True)
class ResolvedUnitSourceResult:
    """Resolved source plus the input-change and refresh decisions for one unit."""

    source: DesiredSource | None
    inputs_changed: bool
    refresh_reason: str | None = None


class SourceRevisionUnavailableError(OperationError):
    """A retained source revision is unavailable under the selected project policy."""

    def __init__(self, unit_name: str, revision: str, operation: Literal["advance", "plan"]) -> None:
        self.unit_name = unit_name
        self.revision = revision
        self.operation = operation
        super().__init__(f"{unit_name} desired source {revision} is unavailable under project policy")


def resolved_unit_source(
    specification: UnitResource[Any],
    source_root: Path,
    source_revision: str,
    current_desired: Path,
    legacy: dict[str, Any] | None,
    source_revision_policy: SourceRevisionPolicy | None = None,
    source_revision_operation: Literal["advance", "plan"] = "advance",
) -> ResolvedUnitSourceResult:
    source_revision_policy = source_revision_policy or SourceRevisionPolicy()
    driver, source = require_unit_specification(specification)
    if source is None:
        return ResolvedUnitSourceResult(source=None, inputs_changed=False)
    input_hash = unit_input_hash(specification, source_root)
    revision = source_revision
    prior = prior_unit_source(specification.name, current_desired, legacy)
    inputs_changed = prior is None
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
                inputs_changed = True
                action = (
                    source_revision_policy.when_unavailable_during_plan
                    if source_revision_operation == "plan"
                    else source_revision_policy.when_unavailable_during_advance
                )
                if action is SourceRevisionAction.ERROR:
                    raise SourceRevisionUnavailableError(specification.name, prior_revision, source_revision_operation)
                unavailable_reason = "is outside candidate history" if prior_available else "is unavailable"
                dry_suffix = " in the dry candidate only" if source_revision_operation == "plan" else ""
                refresh_reason = (
                    f"retained source {describe_revision(prior_revision)} {unavailable_reason}; "
                    f"use {describe_revision(source_revision)}{dry_suffix}"
                )
        else:
            inputs_changed = True
    return ResolvedUnitSourceResult(
        source=DesiredSource(
            path=source.path,
            inputs=source.inputs,
            revision=revision,
            inputHash=input_hash,
            driverVersion=DRIVER_VERSIONS[driver],
        ),
        inputs_changed=inputs_changed,
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
    if environment_document.get("apiVersion") is None:
        raise OperationError(f"legacy environment document is not valid in a Project: {environment_paths[0]}")
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


def resolve_advance_source_revision(
    source_root: Path,
    environment_name: str,
    source_revision: str | None,
) -> str | None:
    promoted = load_environment(source_root, environment_name).get("promotion") is not None
    if promoted:
        if source_revision is not None:
            raise OperationError(f"promotion-tracked environment {environment_name} does not accept --source-revision")
        return None
    if source_revision is None:
        raise OperationError(f"source-tracked environment {environment_name} requires --source-revision")
    return git("rev-parse", f"{source_revision}^{{commit}}").stdout.strip()


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
    operation: Literal["promotion", "rollback"],
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
    operation: Literal["promotion", "rollback"],
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
    if not unit_paths:
        raise OperationError(f"environment has no units: {environment_name}")
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


def require_environment_unit(source_root: Path, environment_name: str, unit_name: str) -> None:
    specifications = load_environment_specifications(source_root, environment_name)
    if unit_name not in specifications:
        available = ", ".join(sorted(specifications))
        raise OperationError(
            f"unknown unit {unit_name!r} for environment {environment_name!r}; available units: {available}"
        )


def reconciliation_statuses(unit_names: Sequence[str], desired: Path, observed: Path) -> list[tuple[str, str, str]]:
    statuses = []
    for unit_name in unit_names:
        unit_path = unit_document_path(desired, unit_name)
        receipt_path = unit_document_path(observed, unit_name)
        if not unit_path.is_file():
            statuses.append((unit_name, "WAIT", "desired inputs are not materialized"))
            continue
        if raw_unit_contains_reference(load_json(unit_path)):
            statuses.append((unit_name, "WAIT", "desired inputs are not materialized"))
            continue
        unit = load_desired_unit(unit_path, unit_name)
        validate_unit_materialization(desired, unit_name, unit)
        if not unit_requires_reconciliation(unit):
            statuses.append((unit_name, "MATERIALIZED", "desired payload is published for external delivery"))
            continue
        if not receipt_path.is_file():
            statuses.append((unit_name, "READY", "no observation receipt"))
            continue
        receipt = load_receipt(receipt_path, unit_name)
        if receipt.spec.desired.unitBlob == file_blob(unit_path):
            validate_receipt_artifacts(observed, unit, receipt)
            statuses.append((unit_name, "CLEAN", "observation matches desired state"))
        else:
            statuses.append(
                (
                    unit_name,
                    "READY",
                    "desired inputs changed since its last receipt",
                )
            )
    return statuses


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
    source_revision: str,
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


@dataclass(frozen=True)
class BuildDesiredResult:
    """Outcome of desired-state construction, including units blocked by unavailable inputs."""

    blocked: Mapping[str, str]
    cleanup_inputs: Mapping[str, DesiredCleanupInput] = field(default_factory=dict)


@dataclass(frozen=True)
class DesiredCleanupInput:
    """Retained source identity needed before a source-absent unit can be finalized."""

    unit_name: str
    desired: UnitResource[Any] | None
    source: DesiredSource | None
    raw_document: JsonObject | None = None


def desired_metadata_for_candidate(authored: UnitResource[Any], previous: UnitResource[Any] | None) -> ResourceMetadata:
    """Select a durable desired identity without reusing a colliding incarnation."""

    if previous is None:
        return ResourceMetadata.new_source_tracked(authored.name)
    if previous.is_legacy_compatibility:
        return ResourceMetadata.new_source_tracked(authored.name)
    previous.metadata.validate_desired()
    lifecycle = previous.metadata.lifecycle
    if lifecycle is None:
        raise OperationError(f"{authored.name} has no desired lifecycle authority")
    if lifecycle.owner is not None:
        raise OperationError(
            f"desired unit {authored.name!r} collides with a UID-fenced owned resource; refusing source adoption"
        )
    assert lifecycle.management is not None
    if lifecycle.management.mode != "sourceTracked":
        raise OperationError(
            f"desired unit {authored.name!r} collides with a directly managed resource; refusing source adoption"
        )
    if previous.gvk != authored.gvk or previous.driver_name != authored.driver_name:
        raise OperationError(
            f"desired unit {authored.name!r} changes GVK/driver; retain the previous source-tracked resource first"
        )
    return previous.metadata


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
    source_revision: str,
    current_desired: Path,
    observed: Path,
    observed_revision: str | None,
    candidate: Path,
    promotion: PromotionContext | None = None,
    dry: bool = False,
    verbose: bool = True,
    source_revision_policy: SourceRevisionPolicy | None = None,
    source_revision_operation: Literal["advance", "plan"] = "advance",
) -> BuildDesiredResult:
    if verbose:
        log_heading(f"Resolve desired state for {style_environment(environment_name)}")
        log_status("SOURCE", f"candidate revision {describe_revision(source_revision)}")
        log_status("DESIRED", "no current state" if not any(current_desired.iterdir()) else "loaded")
        log_status(
            "OBSERVED",
            f"revision {describe_revision(observed_revision)}" if observed_revision else "no observations yet",
        )
    legacy_path = current_desired / "release.json"
    legacy = load_json(legacy_path) if legacy_path.is_file() else None
    specifications = load_environment_specifications(source_root, environment_name)
    if source_revision_policy is None:
        source_revision_policy = (
            load_project_config(source_root).source_revision_policy
            if any((source_root / name).is_file() for name in PROJECT_CONFIG_NAMES)
            else SourceRevisionPolicy()
        )
    candidate_units = candidate / "units"
    candidate_units.mkdir(parents=True)
    if promotion is not None:
        write_preferred_document(candidate / "promotion.json", promotion.document(), source_root)

    prepared: dict[str, tuple[UnitResource[Any], DesiredSource | None]] = {}
    retained_transitions: dict[str, UnitResource[Any]] = {}
    retained_raw_transitions: dict[str, tuple[Path, JsonObject]] = {}
    for unit_name, specification in specifications.items():
        previous_unit = unit_document_path(current_desired, unit_name)
        previous_driver = persisted_unit_driver_name(previous_unit) if previous_unit.is_file() else None
        if previous_unit.is_file() and previous_driver not in {None, specification.driver_name}:
            raw_previous = load_json(previous_unit)
            raw_metadata = raw_previous.get("metadata")
            if isinstance(raw_metadata, dict) and set(raw_metadata) != {"name"}:
                try:
                    persisted_metadata = ResourceMetadata.from_dict(raw_metadata)
                    persisted_metadata.validate_desired()
                except (KeyError, TypeError, ValueError) as exc:
                    raise OperationError(
                        f"desired unit {unit_name!r} has invalid persisted lifecycle metadata"
                    ) from exc
                lifecycle = persisted_metadata.lifecycle
                if lifecycle is not None and (
                    lifecycle.owner is not None
                    or (lifecycle.management is not None and lifecycle.management.mode == "direct")
                ):
                    raise OperationError(
                        f"desired unit {unit_name!r} collides with a directly managed or UID-owned resource"
                    )
            retained_raw_transitions[unit_name] = (previous_unit, raw_previous)
            if verbose:
                log_status(
                    "RETAIN",
                    f"{style_unit(unit_name)}: unavailable previous driver; retain legacy cleanup root",
                )
            continue
        previous = load_desired_unit(previous_unit, unit_name) if previous_unit.is_file() else None
        if previous is not None and not previous.is_legacy_compatibility:
            previous.metadata.validate_desired()
            lifecycle = previous.metadata.lifecycle
            assert lifecycle is not None
            if lifecycle.owner is not None or (
                lifecycle.management is not None and lifecycle.management.mode == "direct"
            ):
                if previous.gvk != specification.gvk or previous.driver_name != specification.driver_name:
                    raise OperationError(
                        f"desired unit {unit_name!r} collides with a directly managed or UID-owned resource"
                    )
        if previous is not None and (
            previous.gvk != specification.gvk or previous.driver_name != specification.driver_name
        ):
            retained = (
                previous.with_metadata(ResourceMetadata.new_source_tracked(unit_name))
                if previous.is_legacy_compatibility
                else previous
            )
            retained_transitions[unit_name] = retained
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
            legacy,
            source_revision_policy,
            source_revision_operation,
        )
        prepared[unit_name] = (specification, source_resolution.source)
        if source_resolution.refresh_reason is not None:
            if verbose:
                log_status("REFRESH", f"{style_unit(unit_name)}: {source_resolution.refresh_reason}")
            continue
        if not previous_unit.is_file():
            resolution_message = "new unit; use candidate revision"
        elif source_resolution.inputs_changed:
            resolution_message = "inputs changed; use candidate revision"
        elif source_resolution.source is None:
            resolution_message = "source-less unit"
        else:
            resolution_message = f"inputs unchanged; retain {describe_revision(source_resolution.source.revision)}"
        if verbose:
            log_status("CHECK", f"{style_unit(unit_name)}: {resolution_message}")

    unresolved = set(prepared)
    unavailable: dict[str, str] = {}
    blocked: dict[str, str] = {}
    while unresolved:
        progressed = False
        for unit_name in sorted(unresolved):
            authored, resolved_source = prepared[unit_name]
            try:
                resolution = authored.driver.resolve_unit(
                    authored.spec,
                    UnitResolutionContext(
                        source=resolved_source,
                        resolve_template=lambda value, pointer, target_unit=authored.name, target_gvk=authored.gvk: (
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
            resolved = resolved.with_metadata(desired_metadata_for_candidate(authored, previous))
            candidate_unit = write_desired_candidate_unit(candidate_units / f"{unit_name}.json", resolved, source_root)
            previous_inputs = getattr(previous.spec, "resolvedInputs", None) if previous is not None else None
            previous_receipts = previous_inputs.receipts if previous_inputs is not None else None
            previous_artifacts = previous_inputs.artifacts if previous_inputs is not None else None
            previous_promotions = previous_inputs.promotions if previous_inputs is not None else None
            fingerprints = resolution.resolved_inputs
            promotions = fingerprints.promotions if fingerprints is not None else None
            receipts = fingerprints.receipts if fingerprints is not None else None
            artifacts = fingerprints.artifacts if fingerprints is not None else None
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
            progressed = True
        if not progressed:
            break

    for unit_name in sorted(unresolved):
        previous = unit_document_path(current_desired, unit_name)
        previous_driver = persisted_unit_driver_name(previous) if previous.is_file() else None
        next_driver = prepared[unit_name][0].driver_name
        if previous_driver == next_driver:
            previous_resource = load_desired_unit(previous, unit_name)
            retained = previous_resource.with_metadata(
                desired_metadata_for_candidate(prepared[unit_name][0], previous_resource)
            )
            write_desired_candidate_unit(candidate_units / previous.name, retained, source_root)
            copy_unit_materialization(current_desired, candidate, unit_name, previous_resource)
            resolution = "retain previous desired state"
        elif previous_driver is not None:
            resolution = f"omit previous {previous_driver} desired state while transitioning to {next_driver}"
        else:
            resolution = "omit until its inputs are available"
        if verbose:
            log_status("WAIT", f"{style_unit(unit_name)}: {unavailable[unit_name]}; {resolution}")
        blocked[unit_name] = unavailable[unit_name]

    cleanup_inputs: dict[str, DesiredCleanupInput] = {}
    for unit_name, (previous_path, raw_previous) in retained_raw_transitions.items():
        selected = DocumentFormat.YAML if previous_path.suffix in {".yaml", ".yml"} else DocumentFormat.JSON
        write_document(candidate_units / previous_path.name, raw_previous, format=selected)
        raw_specification = raw_previous.get("spec", raw_previous)
        raw_source = raw_specification.get("source") if isinstance(raw_specification, dict) else None
        cleanup_source = None
        if isinstance(raw_source, dict) and isinstance(raw_source.get("path"), str):
            cleanup_revision = raw_source.get("revision")
            cleanup_driver_version = raw_source.get("driverVersion")
            cleanup_input_hash = raw_source.get("inputHash")
            cleanup_raw_inputs = raw_source.get("inputs")
            cleanup_inputs_value = (
                cast(list[str], cleanup_raw_inputs)
                if isinstance(cleanup_raw_inputs, list) and all(isinstance(value, str) for value in cleanup_raw_inputs)
                else None
            )
            cleanup_source = DesiredSource(
                path=cast(str, raw_source["path"]),
                revision=cleanup_revision if isinstance(cleanup_revision, str) else None,
                driverVersion=cleanup_driver_version if isinstance(cleanup_driver_version, int) else None,
                inputHash=cleanup_input_hash if isinstance(cleanup_input_hash, str) else None,
                inputs=cleanup_inputs_value,
            )
        cleanup_inputs[unit_name] = DesiredCleanupInput(
            unit_name=unit_name,
            desired=None,
            source=cleanup_source,
            raw_document=raw_previous,
        )
        blocked[unit_name] = "previous desired driver is unavailable; legacy cleanup root retained"
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
        blocked[unit_name] = "desired resource identity changed; previous cleanup root retained"
    for unit_name, previous_path in _current_desired_unit_paths(current_desired).items():
        if unit_name in specifications:
            continue
        previous = load_desired_unit(previous_path, unit_name)
        retained = previous
        if previous.is_legacy_compatibility:
            retained = previous.with_metadata(ResourceMetadata.new_source_tracked(unit_name))
        write_desired_candidate_unit(candidate_units / previous_path.name, retained, source_root)
        if getattr(retained.spec, "materialization", None) is not None:
            copy_unit_materialization(current_desired, candidate, unit_name, previous)
        lifecycle = retained.metadata.lifecycle
        if lifecycle is not None and lifecycle.management is not None and lifecycle.management.mode == "sourceTracked":
            cleanup_inputs[unit_name] = DesiredCleanupInput(
                unit_name=unit_name,
                desired=retained,
                source=getattr(retained.spec, "source", None),
            )
            if verbose:
                log_status("RETAIN", f"{style_unit(unit_name)}: source absent; cleanup inputs retained")
    return BuildDesiredResult(blocked=blocked, cleanup_inputs=cleanup_inputs)


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
    return PromotionContext(
        source_environment=source_environment,
        desired_ref=desired_ref,
        desired_revision=desired_revision,
        observed_ref=observed_ref,
        observed_revision=observed_revision,
        specification_revision=specification_revision,
        desired_root=desired_root,
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


def advance_desired(
    environment: str,
    source_revision: str | None,
    desired_ref: str | None = None,
    observed_ref: str | None = None,
    require_source_ref: str | None = None,
    dry: bool = False,
    summarize: bool = True,
    verbose: bool = True,
    warn_uncommitted: bool = False,
) -> tuple[str | None, bool]:
    desired_override = desired_ref
    observed_override = observed_ref
    requested_source_revision = resolve_advance_source_revision(REPOSITORY_ROOT, environment, source_revision)
    if require_source_ref and requested_source_revision is None:
        raise OperationError("--require-source-ref applies only to source-tracked environments")
    if verbose:
        log_heading(f"Advance desired state for {style_environment(environment)}")
        log_status(
            "START",
            (
                f"environment {style_environment(environment)} from {describe_revision(requested_source_revision)}"
                if requested_source_revision is not None
                else f"environment {style_environment(environment)} from its merged promotion"
            ),
        )
    if warn_uncommitted:
        warn_if_source_revision_excludes_changes(requested_source_revision)
    if requested_source_revision is None:
        desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, environment, desired_ref, observed_ref)
    else:
        with tempfile.TemporaryDirectory() as probe_directory:
            probe_root = Path(probe_directory) / "source"
            materialize_revision(requested_source_revision, probe_root)
            desired_ref, observed_ref = deployment_refs(probe_root, environment, desired_ref, observed_ref)
    if verbose:
        log_status("REFS", f"desired {style_branch(desired_ref)}; observed {style_branch(observed_ref)}")
    for attempt in range(5):
        if attempt and verbose:
            log_status("RETRY", f"desired-state publish attempt {attempt + 1}/5")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            current_desired = temporary / "current"
            observed = temporary / "observed"
            candidate = temporary / "candidate"
            current_revision = observed_tree(desired_ref, current_desired)
            promotion = load_promotion_context(current_desired, temporary)
            if requested_source_revision is None and promotion is None:
                raise OperationError(f"promotion-tracked environment {environment} requires a merged promotion")
            if requested_source_revision is not None and promotion is not None:
                raise OperationError(f"source-tracked environment {environment} contains promotion state")
            effective_source_revision = (
                promotion.specification_revision if promotion is not None else requested_source_revision
            )
            assert effective_source_revision is not None
            if promotion is not None and verbose:
                log_status(
                    "PIN",
                    f"use reviewed specification {describe_revision(effective_source_revision)} from promotion",
                )
            if require_source_ref:
                required_head = fetch_ref(require_source_ref)
                if required_head != requested_source_revision:
                    log_status("SKIP", f"source revision is superseded by {require_source_ref}")
                    return None, False
            source_root = temporary / "source"
            materialize_revision(effective_source_revision, source_root)
            pinned_refs = deployment_refs(source_root, environment, desired_override, observed_override)
            if pinned_refs != (desired_ref, observed_ref):
                raise OperationError("reviewed specification changes deployment refs")
            if promotion is not None and promotion.source_environment not in allowed_promotion_sources(
                source_root, environment
            ):
                raise OperationError(
                    f"{promotion.source_environment} is not an allowed promotion source for {environment}"
                )
            observed_revision = observed_tree(observed_ref, observed)
            build_desired_candidate(
                environment,
                source_root,
                effective_source_revision,
                current_desired,
                observed,
                observed_revision,
                candidate,
                promotion=promotion,
                dry=dry,
                verbose=verbose,
                source_revision_operation="advance",
            )
            load_desired_resource_graph(candidate)
            if current_revision and directory_files(current_desired) == directory_files(candidate):
                if verbose:
                    log_status(
                        "KEEP",
                        f"{style_branch(desired_ref)} already resolved at {describe_revision(current_revision)}",
                    )
                if summarize:
                    log_reconciliation_summary(environment, source_root, candidate, observed)
                return current_revision, False
            if dry:
                if verbose:
                    log_status("DRY", f"{style_branch(desired_ref)} would be updated")
                if summarize:
                    log_reconciliation_summary(environment, source_root, candidate, observed)
                return current_revision, True
            try:
                revision = publish_tree(
                    desired_ref,
                    candidate,
                    current_revision,
                    f"Desired {environment} state from {effective_source_revision}",
                )
                if verbose:
                    log_status("UPDATE", f"{style_branch(desired_ref)} advanced to {describe_revision(revision)}")
                if summarize:
                    log_reconciliation_summary(environment, source_root, candidate, observed)
                return revision, True
            except subprocess.CalledProcessError as exc:
                if attempt == 4 or not retryable_push_failure(exc):
                    raise
    raise OperationError(f"could not advance {desired_ref} after concurrent updates")


def command_advance_desired(args: argparse.Namespace) -> None:
    revision, changed = advance_desired(
        args.environment,
        args.source_revision,
        args.desired_ref,
        args.observed_ref,
        args.require_source_ref,
        args.dry,
        warn_uncommitted=True,
    )
    if revision:
        print(revision)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"desired_changed={'true' if changed else 'false'}\n")
            stream.write(f"desired_revision={revision or ''}\n")


def publish_change_candidate(
    candidate: Path,
    candidate_ref: str,
    target_ref: str,
    target_revision: str | None,
    commit_message: str,
    title: str,
    body: str,
) -> tuple[str, ChangeRequestResult | ManualChangeRequest]:
    load_desired_resource_graph(candidate)
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
        with Path(output).open("a") as stream:
            stream.write(f"change_revision={revision}\n")
            stream.write(f"target_ref={target_ref}\n")
            stream.write(f"candidate_ref={candidate_ref}\n")
            stream.write(f"change_status={outcome.status if outcome else 'published'}\n")
            stream.write(f"change_url={outcome.url if isinstance(outcome, ChangeRequestResult) else ''}\n")


def command_promote(args: argparse.Namespace) -> None:
    specification_revision = git("rev-parse", f"{args.specification_revision or 'HEAD'}^{{commit}}").stdout.strip()
    log_heading(f"Promote {style_environment(args.from_environment)} to {style_environment(args.to_environment)}")
    log_status("SPEC", f"reviewed source {describe_revision(specification_revision)}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        source_root = temporary / "source"
        materialize_revision(specification_revision, source_root)
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
        if target_revision is None and gate == "pullRequest":
            baseline = temporary / "target-baseline"
            baseline_environment = {
                "name": args.to_environment,
                "state": "unpromoted",
            }
            if resource_documents_enabled(source_root):
                write_document(
                    baseline / f"environment{load_project_config(source_root).write_format.suffix}",
                    serialize_environment_document(baseline_environment),
                    format=load_project_config(source_root).write_format,
                )
            else:
                write_json(baseline / "environment.json", baseline_environment)
            target_revision = publish_tree(
                target_desired_ref,
                baseline,
                None,
                f"Initialize desired {args.to_environment} state",
            )
            log_status(
                "INIT",
                f"created inert {style_branch(target_desired_ref)} at {describe_revision(target_revision)}",
            )
        promotion = PromotionContext(
            source_environment=args.from_environment,
            desired_ref=source_desired_ref,
            desired_revision=source_desired_revision,
            observed_ref=source_observed_ref,
            observed_revision=source_observed_revision,
            specification_revision=specification_revision,
            desired_root=source_desired,
        )
        build_desired_candidate(
            args.to_environment,
            source_root,
            specification_revision,
            current_target,
            target_observed,
            target_observed_revision,
            candidate,
            promotion=promotion,
        )
        load_desired_resource_graph(candidate)

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
    target_revision: str,
    candidate_ref: str,
    commit_message: str,
    title: str,
    body: str,
    dry: bool,
) -> tuple[str, ChangeRequestResult | ManualChangeRequest | None]:
    load_desired_resource_graph(candidate)
    gate = change_gate(REPOSITORY_ROOT, environment)
    if dry:
        log_status("DRY", f"{style_branch(target_ref)} would receive {title.lower()}")
        return target_revision, None
    if gate == "pullRequest":
        revision, outcome = publish_change_candidate(
            candidate,
            candidate_ref,
            target_ref,
            target_revision,
            commit_message,
            title,
            body,
        )
        log_status(
            "CANDIDATE",
            f"{style_branch(candidate_ref)} at {describe_revision(revision)} targets {style_branch(target_ref)}",
        )
        return revision, outcome
    revision = publish_tree(target_ref, candidate, target_revision, commit_message)
    log_status("UPDATE", f"{style_branch(target_ref)} advanced to {describe_revision(revision)}")
    return revision, None


def validate_materialized_desired(
    environment: str,
    desired_revision: str,
    desired: Path,
    source: Path,
    description: str,
) -> tuple[dict[str, UnitResource[Any]], list[str]]:
    specifications = load_environment_specifications(source, environment)
    expected_units = sorted(specifications)
    desired_units = sorted(
        {path.stem for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}}
    )
    if desired_units != expected_units:
        raise OperationError(f"{description} {describe_revision(desired_revision)} is not fully materialized")
    for unit_name in desired_units:
        unit = load_desired_unit(unit_document_path(desired, unit_name), unit_name)
        if unit_contains_reference(unit):
            raise OperationError(f"{description} unit {unit_name} contains unresolved inputs")
        driver, _source = require_unit(unit, unit_name)
        validate_unit_materialization(desired, unit_name, unit)
        if driver != specifications[unit_name].driver_name:
            raise OperationError(f"{description} unit {unit_name} does not match its specification driver")
    return specifications, desired_units


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

        current_specification_revision = desired_specification_revision(
            current_revision,
            current,
            temporary / "current-promotion",
        )
        target_specification_revision = desired_specification_revision(
            target_revision,
            target,
            temporary / "target-promotion",
        )
        current_source = temporary / "current-source"
        target_source = temporary / "target-source"
        materialize_revision(current_specification_revision, current_source)
        materialize_revision(target_specification_revision, target_source)
        target_specifications, target_units = validate_materialized_desired(
            args.environment,
            target_revision,
            target,
            target_source,
            "rollback target",
        )
        if mode == "units":
            current_specifications, _current_units = validate_materialized_desired(
                args.environment,
                current_revision,
                current,
                current_source,
                "current desired state",
            )
        else:
            current_specifications = load_environment_specifications(current_source, args.environment)
        current_environment = load_environment(current_source, args.environment)
        target_environment = load_environment(target_source, args.environment)
        current_promotion = promotion_lineage(current)
        target_promotion = promotion_lineage(target)
        current_promoted = current_environment.get("promotion") is not None
        target_promoted = target_environment.get("promotion") is not None
        if current_promoted != (current_promotion is not None):
            raise OperationError("current desired state has an incompatible environment mode")
        if target_promoted != (target_promotion is not None):
            raise OperationError("rollback target has an incompatible environment mode")
        current_refs = deployment_refs(
            current_source,
            args.environment,
            args.desired_ref,
            args.observed_ref,
        )
        target_refs = deployment_refs(
            target_source,
            args.environment,
            args.desired_ref,
            args.observed_ref,
        )
        if current_refs != (desired_ref, observed_ref):
            raise OperationError("current specification changes deployment refs")
        requested_units = sorted(set(args.unit or target_units))
        unknown = sorted(
            (set(requested_units) - set(current_specifications)) | (set(requested_units) - set(target_units))
        )
        if unknown:
            raise OperationError("unknown rollback unit(s): " + ", ".join(unknown))
        if mode == "full" and set(current_specifications) != set(target_units):
            raise OperationError("full-tree rollback requires the current and target unit sets to match")
        if mode == "full" and (current_promoted != target_promoted or current_refs != target_refs):
            raise OperationError("full-tree rollback cannot change environment revision mode or deployment refs")
        downstream = downstream_unit_closure(current_specifications, requested_units) if mode == "units" else []
        materialized_units = sorted(set(requested_units) | set(downstream)) if mode == "units" else target_units
        missing_from_target = sorted(set(materialized_units) - set(target_units))
        if missing_from_target:
            raise OperationError("rollback target is missing downstream unit(s): " + ", ".join(missing_from_target))
        for unit_name in materialized_units:
            current_driver = current_specifications[unit_name].driver_name
            target_driver = target_specifications[unit_name].driver_name
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
            "fromSpecificationRevision": current_specification_revision,
            "targetDesiredRevision": target_revision,
            "targetObservedRevision": observed_revision,
            "targetSpecificationRevision": target_specification_revision,
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
        for unit_name in materialized_units:
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
            f"From-Specification-Revision: {current_specification_revision}\n"
            f"Target-Desired-Revision: {target_revision}\n"
            f"Target-Observed-Revision: {observed_revision}\n"
            f"Target-Specification-Revision: {target_specification_revision}\n"
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
        )
        if args.dry:
            print(json.dumps(provenance, indent=2, sort_keys=True))
            return
        print(revision)
        write_change_outputs(revision, desired_ref, candidate_ref if outcome else "", outcome)


def command_resolve_desired(args: argparse.Namespace) -> None:
    revision = resolve_ref(args.desired_ref, args.desired_revision)
    print(revision)


def command_status(args: argparse.Namespace) -> None:
    if args.environment is None:
        if args.unit or args.desired_ref or args.desired_revision or args.observed_ref or args.verbose:
            raise OperationError("status options other than --environment are only available for one environment")
        command_list_environments(argparse.Namespace(json=False))
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
        statuses = reconciliation_statuses(sorted(specifications), desired, observed)
        if args.unit is not None:
            if args.unit not in specifications:
                available = ", ".join(sorted(specifications)) or "none"
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


def _environment_names() -> list[str]:
    project = load_project_config(REPOSITORY_ROOT)
    root = REPOSITORY_ROOT.joinpath(*project.environments_path.parts)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def _unit_status_snapshot(environment: str, desired_ref: str, observed_ref: str) -> EnvironmentSnapshot:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        observed = temporary / "observed"
        desired_revision = observed_tree(desired_ref, desired)
        observed_revision = observed_tree(observed_ref, observed)
        specifications = load_environment_specifications(REPOSITORY_ROOT, environment)
        statuses = reconciliation_statuses(sorted(specifications), desired, observed)
        return {
            "desired": {"ref": desired_ref, "revision": desired_revision},
            "observed": {"ref": observed_ref, "revision": observed_revision},
            "statuses": [{"unit": unit, "status": status, "reason": reason} for unit, status, reason in statuses],
        }


def print_inspection_document(document: dict[str, Any], *, force_json: bool = False, force_yaml: bool = False) -> None:
    """Print one inspection document using the project's configured format."""
    selected = (
        DocumentFormat.JSON
        if force_json
        else DocumentFormat.YAML
        if force_yaml
        else load_project_config(REPOSITORY_ROOT).write_format
    )
    if selected is DocumentFormat.JSON:
        print(json.dumps(document, indent=2, sort_keys=True))
        return
    yaml_document = dict(document)
    schema_hint = yaml_document.pop("$schema", None)
    text = yaml.safe_dump(yaml_document, sort_keys=False, default_flow_style=False, allow_unicode=False)
    if isinstance(schema_hint, str):
        text = f"# yaml-language-server: $schema={schema_hint}\n{text}"
    print(text, end="")


def command_list_environments(args: argparse.Namespace) -> None:
    rows: list[EnvironmentRow] = []
    for environment in _environment_names():
        desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, environment)
        snapshot = _unit_status_snapshot(environment, desired_ref, observed_ref)
        statuses = snapshot["statuses"]
        counts = {
            status: sum(item["status"] == status for item in statuses)
            for status in ("CLEAN", "READY", "WAIT", "MATERIALIZED")
        }
        rows.append({"environment": environment, **snapshot, "counts": counts})

    if args.json:
        print(json.dumps({"schema": 1, "environments": rows}, indent=2, sort_keys=True))
        return
    log_heading("Environments")
    for index, row in enumerate(rows):
        if index:
            print(file=sys.stderr)
        desired = describe_revision(row["desired"]["revision"], sys.stderr) if row["desired"]["revision"] else "none"
        observed = describe_revision(row["observed"]["revision"], sys.stderr) if row["observed"]["revision"] else "none"
        counts = ", ".join(f"{name.lower()}={count}" for name, count in row["counts"].items() if count)
        log_status("ENV", style_environment(row["environment"]))
        log_status("DESIRED", f"{style_branch(row['desired']['ref'])} at {desired}")
        log_status("OBSERVED", f"{style_branch(row['observed']['ref'])} at {observed}")
        log_status("UNITS", counts or "no units")


def command_list_units(args: argparse.Namespace) -> None:
    desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment)
    snapshot = _unit_status_snapshot(args.environment, desired_ref, observed_ref)
    result = {"schema": 1, "environment": args.environment, **snapshot}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    log_heading(f"Units for {style_environment(args.environment)}")
    for item in snapshot["statuses"]:
        log_status(item["status"], f"{style_unit(item['unit'])}: {item['reason']}")


def command_show_desired(args: argparse.Namespace) -> None:
    desired_ref, _ = deployment_refs(REPOSITORY_ROOT, args.environment, args.desired_ref)
    with tempfile.TemporaryDirectory() as temporary_directory:
        desired = Path(temporary_directory) / "desired"
        revision = resolve_ref(desired_ref, args.desired_revision)
        materialize_revision(revision, desired)
        path = unit_document_path(desired, args.unit)
        if not path.is_file():
            raise OperationError(f"{desired_ref} has no desired unit {args.unit}")
        document = load_json(path)
        unit = load_desired_unit(path, args.unit)
        validate_unit_materialization(desired, args.unit, unit)
    print_inspection_document(document, force_json=args.json, force_yaml=args.yaml)


def command_show_receipt(args: argparse.Namespace) -> None:
    _, observed_ref = deployment_refs(REPOSITORY_ROOT, args.environment, observed_override=args.observed_ref)
    with tempfile.TemporaryDirectory() as temporary_directory:
        observed = Path(temporary_directory) / "observed"
        observed_tree(observed_ref, observed)
        path = unit_document_path(observed, args.unit)
        if not path.is_file():
            raise OperationError(f"{observed_ref} has no receipt for {args.unit}")
        document = load_json(path)
        receipt = load_receipt(path, args.unit)
        artifacts: dict[str, Any] = {}
        for name, descriptor in (receipt.status.artifacts or {}).items():
            if not isinstance(descriptor.path, str):
                raise OperationError(f"receipt for {args.unit} has an invalid artifact descriptor {name!r}")
            artifact_relative = PurePosixPath(descriptor.path)
            if artifact_relative.is_absolute() or ".." in artifact_relative.parts:
                raise OperationError(f"receipt artifact {name!r} has an unsafe path")
            artifact_path = observed.joinpath(*artifact_relative.parts)
            if not artifact_path.is_file():
                raise OperationError(f"receipt artifact {name!r} is missing at {descriptor.path}")
            artifacts[name] = load_json(artifact_path)
    if args.artifact is not None:
        if args.artifact not in artifacts:
            available = ", ".join(sorted(artifacts)) or "none"
            raise OperationError(
                f"{observed_ref} receipt for {args.unit} has no artifact {args.artifact!r}; available: {available}"
            )
        print_inspection_document(artifacts[args.artifact], force_json=args.json, force_yaml=args.yaml)
    elif args.artifacts:
        print_inspection_document(artifacts, force_json=args.json, force_yaml=args.yaml)
    else:
        print_inspection_document(document, force_json=args.json, force_yaml=args.yaml)


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

    if isinstance(document, dict) and isinstance(document.get("spec"), dict):
        return contains_reference(document["spec"])
    return contains_reference(document)


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
) -> str:
    for attempt in range(5):
        if attempt:
            log_status("RETRY", f"observation publish attempt {attempt + 1}/5")
        with tempfile.TemporaryDirectory() as temporary_directory:
            observed = Path(temporary_directory) / "observed"
            observed_revision = observed_tree(observed_ref, observed)
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


def command_reconcile(args: argparse.Namespace) -> bool:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.unit):
        raise OperationError(f"invalid unit name: {args.unit!r}")
    configured_environment = load_environment(REPOSITORY_ROOT, args.environment)
    promoted_environment = configured_environment.get("promotion") is not None
    if args.advance:
        source_revision = resolve_advance_source_revision(REPOSITORY_ROOT, args.environment, args.source_revision)
    elif args.source_revision is not None:
        if promoted_environment:
            raise OperationError(f"promotion-tracked environment {args.environment} does not accept --source-revision")
        if not args.plan:
            raise OperationError("--source-revision requires --advance or --plan")
        source_revision = git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
    else:
        source_revision = None
    if args.require_source_ref and source_revision is None:
        raise OperationError("--require-source-ref requires --source-revision")
    log_heading(f"Reconcile {style_unit(args.unit)}")
    log_status("START", f"environment {style_environment(args.environment)}")
    log_status("MODE", "plan" if args.plan else "apply")
    report = Path(args.report).resolve() if args.report else None
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        observed = temporary / "observed"
        if not args.plan and args.require_source_ref:
            required_head = fetch_ref(args.require_source_ref)
            if required_head != source_revision:
                log_status("SKIP", f"source revision is superseded by {args.require_source_ref}")
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
                return False
        warn_if_source_revision_excludes_changes(source_revision)
        candidate_source_root: Path | None = None
        if source_revision and args.plan:
            candidate_source_root = temporary / "candidate-source"
            materialize_revision(source_revision, candidate_source_root)
        ref_source_root = candidate_source_root or REPOSITORY_ROOT
        desired_ref, observed_ref = deployment_refs(
            ref_source_root,
            args.environment,
            args.desired_ref,
            args.observed_ref,
        )
        if args.plan or (args.advance and not args.desired_revision):
            require_environment_unit(ref_source_root, args.environment, args.unit)
        log_status("REFS", f"desired {style_branch(desired_ref)}; observed {style_branch(observed_ref)}")
        pre_advance = not args.plan and args.advance and not args.desired_revision
        pre_advanced_revision = ""
        if pre_advance:
            advanced, changed = advance_desired(
                args.environment,
                source_revision,
                desired_ref,
                observed_ref,
                args.require_source_ref,
            )
            if advanced is None:
                log_status("DONE", f"{style_unit(args.unit)}: source revision is no longer eligible")
                write_reconcile_outputs(False)
                return False
            desired_revision = advanced
            if changed:
                pre_advanced_revision = advanced
            log_status("PIN", f"reconcile advanced desired state at {describe_revision(advanced)}")
        observed_revision = observed_tree(observed_ref, observed)
        if args.plan and candidate_source_root is not None:
            assert source_revision is not None
            current_desired = temporary / "current-desired"
            observed_tree(desired_ref, current_desired)
            candidate_result = build_desired_candidate(
                args.environment,
                candidate_source_root,
                source_revision,
                current_desired,
                observed,
                observed_revision,
                desired,
                dry=True,
                source_revision_operation="plan",
            )
            if args.unit in candidate_result.blocked:
                log_status("WAIT", candidate_result.blocked[args.unit])
                log_status("DONE", f"{style_unit(args.unit)}: no changes")
                write_reconcile_outputs(False)
                return False
            desired_revision = f"dry:{source_revision}"
        elif not pre_advance:
            desired_revision = resolve_ref(desired_ref, args.desired_revision)
        if candidate_source_root is None:
            materialize_revision(desired_revision, desired)
        log_status("DESIRED", f"{style_branch(desired_ref)} at {describe_revision(desired_revision)}")
        log_status(
            "OBSERVED",
            f"{style_branch(observed_ref)} at {describe_revision(observed_revision)}"
            if observed_revision
            else f"{style_branch(observed_ref)} has no receipts yet",
        )
        unit_path = unit_document_path(desired, args.unit)
        if not unit_path.is_file():
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(args.unit)}: no changes")
            write_reconcile_outputs(False)
            return False
        ensure_desired_units_materialized(desired)
        load_desired_resource_graph(desired)
        if raw_unit_contains_reference(load_json(unit_path)):
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{style_unit(args.unit)}: no changes")
            write_reconcile_outputs(False)
            return False
        unit = load_desired_unit(unit_path, args.unit)
        driver_name, source = require_unit(unit, args.unit)
        validate_unit_materialization(desired, args.unit, unit)
        log_status("DRIVER", driver_name)
        if source is not None:
            assert source.revision is not None
            log_status("SOURCE", f"{describe_revision(source.revision)} ({source.path})")
        else:
            log_status("SOURCE", "none (source-less unit)")
        if unit_contains_reference(unit):
            raise OperationError(f"{args.unit} desired state is not fully materialized")
        if not unit_requires_reconciliation(unit):
            log_status("SKIP", "unit is complete after desired-state materialization")
            log_status("DONE", f"{style_unit(args.unit)}: materialized for external delivery")
            write_reconcile_outputs(False, pre_advanced_revision)
            return False

        def advance_if_requested() -> str:
            if not args.advance:
                return ""
            advanced, changed = advance_desired(
                args.environment,
                source_revision,
                desired_ref,
                observed_ref,
                args.require_source_ref,
            )
            if changed and advanced:
                return advanced
            if advanced:
                log_status("KEEP", f"{style_branch(desired_ref)} did not change after observation")
            return ""

        unit_blob = file_blob(unit_path)
        receipt_path = unit_document_path(observed, args.unit)
        previous_receipt = load_receipt(receipt_path, args.unit) if receipt_path.is_file() else None
        if receipt_path.is_file():
            assert previous_receipt is not None
            receipt = previous_receipt
            skip_clean_unit = not args.plan or bool(UNIT_DRIVERS[driver_name].artifact_outputs)
            receipt_is_current = receipt.spec.desired.unitBlob == unit_blob
            if receipt_is_current:
                validate_receipt_artifacts(observed, unit, receipt)
            if not getattr(args, "reapply", False) and skip_clean_unit and receipt_is_current:
                log_status("KEEP", "observation already matches desired state")
                if args.plan:
                    advanced_revision = ""
                elif pre_advance:
                    advanced_revision = pre_advanced_revision
                else:
                    advanced_revision = advance_if_requested()
                log_status("DONE", f"{style_unit(args.unit)}: clean")
                write_reconcile_outputs(False, advanced_revision)
                return False

        log_status("RUN", f"execute {driver_name} {'planning' if args.plan else 'reconciliation'}")
        source_root = temporary / "source" if source is not None else None
        if source is not None:
            assert source.revision is not None
            assert source_root is not None
            materialize_revision(source.revision, source_root)
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
            log_status("PLAN", f"{driver_name} planning succeeded")
            log_status("DONE", f"{style_unit(args.unit)}: no remote changes")
            write_reconcile_outputs(False)
            return False
        try:
            plugin = RECONCILIATION_DRIVERS[driver_name]
        except KeyError as exc:
            raise OperationError(f"{args.unit} uses {driver_name}, which does not support reconciliation") from exc
        output = plugin.reconcile(
            ReconciliationContext(
                **execution,
                previous_receipt=previous_receipt,
            )
        )
        if not isinstance(output, ReconciliationOutput):
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
        revision = publish_observation_cas(
            observed_ref,
            args.unit,
            receipt,
            unit,
            output.artifacts,
            desired_revision,
        )
        log_status(
            "OBSERVE",
            f"receipt published to {style_branch(observed_ref)} at {describe_revision(revision)}",
        )
        advanced_revision = advance_if_requested() or pre_advanced_revision
        write_reconcile_outputs(True, advanced_revision)
        log_status("DONE", f"{style_unit(args.unit)}: reconciled successfully")
        return True


def log_ref_advance(advance: RefAdvance) -> None:
    attribution = f" after {style_unit(advance.unit)}" if advance.unit else ""
    log_status(
        "ADVANCE",
        f"{style_branch(advance.ref)} {describe_revision(advance.before)} -> "
        f"{describe_revision(advance.after)}{attribution}",
    )


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
    advances: list[RefAdvance],
    result: str,
    unselected: list[tuple[str, str, str]] | None = None,
) -> None:
    log_heading(f"Convergence result for {style_environment(environment)}")
    if result == "CLEAN":
        driver_summary = f"drivers ran for {style_units(steps)}" if steps else "no drivers ran"
        ref_summary = f"{len(advances)} ref movement{'s' if len(advances) != 1 else ''}"
        log_status("RESULT", f"CLEAN: {len(scope)}/{len(scope)} units; {driver_summary}; {ref_summary}")
    else:
        log_status("RESULT", result)
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{style_unit(unit_name)}: {status.lower()}; {reason}")


def log_convergence_summary(
    environment: str,
    targets: Sequence[str],
    scope: Sequence[str],
    steps: list[str],
    advances: list[RefAdvance],
    start_heads: tuple[str | None, str | None],
    end_heads: tuple[str | None, str | None],
    result: str,
    unselected: list[tuple[str, str, str]] | None = None,
) -> None:
    log_heading(f"Convergence summary for {style_environment(environment)}")
    log_status("TARGET", style_units(targets))
    log_status("SCOPE", style_units(scope))
    log_status("STEPS", style_units(steps) if steps else "no reconciliation drivers ran")
    if advances:
        for index, advance in enumerate(advances, 1):
            attribution = f" ({style_unit(advance.unit)})" if advance.unit else ""
            log_status(
                "MOVE",
                f"{index}. {advance.kind} {style_branch(advance.ref)} "
                f"{describe_revision(advance.before)} -> "
                f"{describe_revision(advance.after)}{attribution}",
            )
    else:
        log_status("MOVE", "no desired or observed ref advances")
    log_status(
        "DESIRED",
        f"{describe_revision(start_heads[0])} -> {describe_revision(end_heads[0])}",
    )
    log_status(
        "OBSERVED",
        f"{describe_revision(start_heads[1])} -> {describe_revision(end_heads[1])}",
    )
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{style_unit(unit_name)}: {status.lower()}; {reason}")
    log_status("RESULT", result)


def command_dependencies(args: argparse.Namespace) -> None:
    source_revision = git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_root = Path(temporary_directory) / "source"
        materialize_revision(source_revision, source_root)
        specifications = load_environment_specifications(source_root, args.environment)
        selection = convergence_scope(specifications, args.unit, args.depth)
        targets, scope = selection.targets, selection.scope
        graph = dependency_graph(specifications, scope)
        order = convergence_order(specifications, scope)
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


def command_converge(args: argparse.Namespace) -> None:
    if args.max_steps is not None and args.max_steps < 1:
        raise OperationError("--max-steps must be a positive integer")
    source_revision = resolve_advance_source_revision(REPOSITORY_ROOT, args.environment, args.source_revision)
    if args.require_source_ref and source_revision is None:
        raise OperationError("--require-source-ref applies only to source-tracked environments")
    log_heading(f"Converge {style_environment(args.environment)}")
    log_status(
        "SOURCE",
        describe_revision(source_revision) if source_revision else "merged promotion",
    )
    warn_if_source_revision_excludes_changes(source_revision)

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe_source = temporary / "probe-source"
        if source_revision is None:
            desired_ref, observed_ref = deployment_refs(
                REPOSITORY_ROOT,
                args.environment,
                args.desired_ref,
                args.observed_ref,
            )
        else:
            materialize_revision(source_revision, probe_source)
            desired_ref, observed_ref = deployment_refs(
                probe_source,
                args.environment,
                args.desired_ref,
                args.observed_ref,
            )
        current_desired = temporary / "current-desired"
        start_desired = observed_tree(desired_ref, current_desired)
        start_observed = fetch_ref(observed_ref)
        promotion = load_promotion_context(current_desired, temporary)
        if source_revision is None and promotion is None:
            source_root = REPOSITORY_ROOT
            effective_source_revision = None
        elif promotion is not None:
            effective_source_revision = promotion.specification_revision
            source_root = temporary / "reviewed-source"
            materialize_revision(effective_source_revision, source_root)
            if deployment_refs(
                source_root,
                args.environment,
                args.desired_ref,
                args.observed_ref,
            ) != (desired_ref, observed_ref):
                raise OperationError("reviewed specification changes deployment refs")
            log_status("PIN", f"reviewed specification {describe_revision(effective_source_revision)}")
        else:
            assert source_revision is not None
            effective_source_revision = source_revision
            source_root = probe_source
        specifications = load_environment_specifications(source_root, args.environment)
        selection = convergence_scope(specifications, args.unit)
        targets, scope = selection.targets, selection.scope
        order = convergence_order(specifications, scope)
        if args.verbose:
            log_status("REFS", f"desired {style_branch(desired_ref)}; observed {style_branch(observed_ref)}")
            log_status("TARGET", style_units(targets))
            log_status("SCOPE", style_units(scope))
            log_dependency_graph(dependency_graph(specifications, scope).dependencies)
        else:
            log_status("DESIRED", f"{style_branch(desired_ref)} at {describe_revision(start_desired)}")
            log_status("OBSERVED", f"{style_branch(observed_ref)} at {describe_revision(start_observed)}")
            if targets != scope:
                log_status("TARGET", style_units(targets))
                log_status("SCOPE", style_units(scope))

        advances: list[RefAdvance] = []
        steps: list[str] = []
        last_desired = start_desired
        last_observed = start_observed
        max_steps = args.max_steps or max(2, 2 * len(scope))
        iterations = 0
        previous_plan: list[tuple[str, str, str]] | None = None

        promotion_units = sorted(
            unit_name
            for unit_name in scope
            if reference_paths(
                specifications[unit_name].driver.unit_contract.dump(specifications[unit_name].spec),
                "fromPromotion",
                current_unit=unit_name,
            )
        )
        if promotion_units and promotion is None:
            result = "FAILED: review gate requires a merged promotion for " + ", ".join(promotion_units)
            log_status("REVIEW", result.removeprefix("FAILED: "))
            if args.verbose:
                log_convergence_summary(
                    args.environment,
                    targets,
                    scope,
                    steps,
                    advances,
                    (start_desired, start_observed),
                    (last_desired, last_observed),
                    result,
                )
            else:
                log_compact_convergence_summary(args.environment, scope, steps, advances, result)
            raise OperationError(result.removeprefix("FAILED: "))

        try:
            while True:
                iterations += 1
                before_desired = fetch_ref(desired_ref)
                desired_revision, _changed = advance_desired(
                    args.environment,
                    args.source_revision,
                    desired_ref,
                    observed_ref,
                    args.require_source_ref,
                    summarize=False,
                    verbose=args.verbose,
                )
                if desired_revision is None:
                    raise OperationError("source revision is no longer eligible")
                last_desired = desired_revision
                if before_desired != desired_revision:
                    movement = RefAdvance("desired", desired_ref, before_desired, desired_revision)
                    advances.append(movement)
                    if args.verbose:
                        log_ref_advance(movement)
                    else:
                        log_status(
                            "DESIRED",
                            f"{style_branch(desired_ref)} {describe_revision(before_desired)} -> "
                            f"{describe_revision(desired_revision)} (advanced)",
                        )

                state = temporary / f"state-{iterations}"
                desired = state / "desired"
                observed = state / "observed"
                materialize_revision(desired_revision, desired)
                last_observed = observed_tree(observed_ref, observed)
                statuses = reconciliation_statuses(scope, desired, observed)
                status_by_unit = {unit_name: status for unit_name, status, _ in statuses}
                if args.verbose:
                    log_reconciliation_status(
                        args.environment,
                        statuses,
                        desired_revision,
                        desired,
                        observed,
                        args.verbose,
                    )

                if all(status in {"CLEAN", "MATERIALIZED"} for _, status, _ in statuses):
                    all_statuses = reconciliation_statuses(sorted(specifications), desired, observed)
                    unselected = [item for item in all_statuses if item[0] not in set(scope)]
                    materialized_count = sum(status == "MATERIALIZED" for _, status, _ in statuses)
                    result = (
                        "CLEAN"
                        if materialized_count == 0
                        else f"COMPLETE: {len(scope) - materialized_count} clean; {materialized_count} materialized"
                    )
                    if args.verbose:
                        log_convergence_summary(
                            args.environment,
                            targets,
                            scope,
                            steps,
                            advances,
                            (start_desired, start_observed),
                            (last_desired, last_observed),
                            result,
                            unselected,
                        )
                    else:
                        log_compact_convergence_summary(
                            args.environment,
                            scope,
                            steps,
                            advances,
                            result,
                            unselected,
                        )
                    return

                repeated = [
                    unit_name for unit_name in order if status_by_unit.get(unit_name) == "READY" and unit_name in steps
                ]
                if args.fail_on_repeat and repeated:
                    raise OperationError(
                        "convergence heuristic detected repeated ready unit(s): " + ", ".join(repeated)
                    )
                ready = [unit_name for unit_name in order if status_by_unit.get(unit_name) == "READY"]
                if not ready:
                    waiting = [f"{unit_name} ({reason})" for unit_name, status, reason in statuses if status == "WAIT"]
                    raise OperationError("convergence stalled with no ready unit: " + ", ".join(waiting))
                if len(steps) >= max_steps:
                    raise OperationError(f"convergence did not finish within {max_steps} reconciliation steps")

                unit_name = ready[0]
                plan = convergence_plan_rows(statuses, order)
                if not args.verbose:
                    log_convergence_plan(plan, previous_plan)
                    previous_plan = plan
                reason_by_unit = {name: reason for name, _status, reason in statuses}
                log_convergence_action(
                    unit_name,
                    reason_by_unit[unit_name],
                    desired_revision,
                    desired,
                    observed,
                    observed_ref,
                )
                if not args.yes:
                    require_reconciliation_approval(unit_name)
                if args.verbose:
                    log_heading(f"Convergence step {len(steps) + 1} (limit {max_steps}): {style_unit(unit_name)}")
                else:
                    log_status("RUN", style_unit(unit_name))
                before_observed = last_observed
                ran = command_reconcile(
                    argparse.Namespace(
                        unit=unit_name,
                        environment=args.environment,
                        desired_ref=desired_ref,
                        desired_revision=desired_revision,
                        observed_ref=observed_ref,
                        plan=False,
                        report=None,
                        source_revision=None,
                        advance=False,
                        require_source_ref=None,
                        reapply=False,
                    )
                )
                after_observed = fetch_ref(observed_ref)
                last_observed = after_observed
                if ran:
                    steps.append(unit_name)
                if before_observed != after_observed and after_observed is not None:
                    movement = RefAdvance(
                        "observed",
                        observed_ref,
                        before_observed,
                        after_observed,
                        unit_name,
                    )
                    advances.append(movement)
                    if args.verbose:
                        log_ref_advance(movement)
                    else:
                        log_status(
                            "OBSERVED",
                            f"{style_branch(observed_ref)} {describe_revision(before_observed)} -> "
                            f"{describe_revision(after_observed)} ({style_unit(unit_name)})",
                        )
        except (DriverError, OperationError, subprocess.CalledProcessError) as exc:
            detail = (
                (exc.stderr or "").strip() or str(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            )
            result = f"FAILED: {detail}"
            if args.verbose:
                log_convergence_summary(
                    args.environment,
                    targets,
                    scope,
                    steps,
                    advances,
                    (start_desired, start_observed),
                    (last_desired, last_observed),
                    result,
                )
            else:
                log_compact_convergence_summary(args.environment, scope, steps, advances, result)
            raise


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
        force=args.force,
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
        ("Project", ("create", "validate")),
        ("Schemas", ("schemas",)),
        ("Deployment", ("advance-desired", "promote", "rollback", "resolve-desired")),
        ("Inspection", ("status", "list", "show", "verify", "dependencies")),
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

    create = commands.add_parser("create", help="create a Project, Environment, or Unit resource")
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
    create_unit.add_argument("--name", required=True)
    create_unit.add_argument("--driver", required=True, choices=tuple(sorted(UNIT_DRIVERS)))
    create_unit.add_argument(
        "--source-path",
        default=".",
        help="path relative to the root of the selected source revision",
    )
    create_unit.add_argument("--force", action="store_true", help="replace an existing Unit resource")
    create_unit.set_defaults(handler=command_create_unit)

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

    advance = commands.add_parser(
        "advance-desired",
        help="advance desired state with ready units",
    )
    advance.add_argument("--environment", required=True)
    advance.add_argument("--source-revision")
    advance.add_argument("--desired-ref", help="override the environment's desired ref")
    advance.add_argument("--observed-ref", help="override the environment's observed ref")
    advance.add_argument("--require-source-ref")
    advance.add_argument("--dry", action="store_true")
    advance.set_defaults(handler=command_advance_desired)

    promote = commands.add_parser(
        "promote",
        help="promote reviewed desired state",
    )
    promote.add_argument("--from-environment", required=True)
    promote.add_argument("--to-environment", required=True)
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

    list_command = commands.add_parser("list", help="list environments or units")
    list_commands = list_command.add_subparsers(dest="list_command", required=True)
    list_environments = list_commands.add_parser("environments", help="list environments and their deployment summary")
    list_environments.add_argument("--json", action="store_true", help="emit one machine-readable document")
    list_environments.set_defaults(handler=command_list_environments)
    list_units = list_commands.add_parser("units", help="list units and their reconciliation status")
    list_units.add_argument("--environment", required=True)
    list_units.add_argument("--json", action="store_true", help="emit one machine-readable document")
    list_units.set_defaults(handler=command_list_units)

    show = commands.add_parser("show", help="show desired state or a receipt")
    show_commands = show.add_subparsers(dest="show_command", required=True)
    desired = show_commands.add_parser("desired", aliases=("desired-unit",), help="show one desired unit")
    desired.add_argument("--environment", required=True)
    desired.add_argument("unit")
    desired.add_argument("--desired-ref", help="override the environment's desired ref")
    desired.add_argument("--desired-revision", help="exact desired commit; defaults to the current desired ref head")
    desired_format = desired.add_mutually_exclusive_group()
    desired_format.add_argument("--json", action="store_true", help="force JSON instead of the project format")
    desired_format.add_argument("--yaml", action="store_true", help="force YAML instead of the project format")
    desired.set_defaults(handler=command_show_desired)
    receipt = show_commands.add_parser("receipt", help="show one observation receipt and its artifacts")
    receipt.add_argument("--environment", required=True)
    receipt.add_argument("unit")
    receipt.add_argument("--observed-ref", help="override the environment's observed ref")
    receipt_format = receipt.add_mutually_exclusive_group()
    receipt_format.add_argument("--json", action="store_true", help="force JSON instead of the project format")
    receipt_format.add_argument("--yaml", action="store_true", help="force YAML instead of the project format")
    receipt_artifacts = receipt.add_mutually_exclusive_group()
    receipt_artifacts.add_argument("--artifact", help="show one named artifact instead of the receipt")
    receipt_artifacts.add_argument(
        "--artifacts",
        action="store_true",
        help="show all artifacts as an object keyed by artifact name",
    )
    receipt.set_defaults(handler=command_show_receipt)

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
    reconcile.add_argument(
        "--source-revision",
        help="source commit used for plan resolution or post-reconcile advancement",
    )
    reconcile.add_argument("--environment", required=True)
    reconcile.add_argument("--advance", action="store_true")
    reconcile.add_argument("--require-source-ref")
    reconcile.add_argument(
        "--reapply",
        action="store_true",
        help="run the driver even when a clean receipt already exists",
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
    converge.add_argument(
        "--unit",
        action="append",
        help="target unit to converge; repeat for multiple targets (defaults to all units)",
    )
    converge.add_argument("--desired-ref", help="override the environment's desired ref")
    converge.add_argument("--observed-ref", help="override the environment's observed ref")
    converge.add_argument("--require-source-ref")
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
        if exc.operation == "plan":
            print(
                "      Run advance-desired from a durable source revision before planning.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                "      Run advance-desired from a durable source revision before retrying.",
                file=sys.stderr,
                flush=True,
            )
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
