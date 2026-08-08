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
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any

from gitopsctr.contracts import CORE_CONTRACTS, with_schema
from gitopsctr.document import ContractError, DocumentContract
from gitopsctr.driver import (
    DRIVER_GVKS,
    DRIVER_NAMES_BY_GVK,
    DRIVER_VERSIONS,
    MATERIALIZATION_DRIVERS,
    PLANNING_DRIVERS,
    RECONCILIATION_DRIVERS,
    UNIT_DRIVERS,
    VERIFICATION_DRIVERS,
    DriverError,
    MaterializationContext,
    MaterializationResult,
    PlanningContext,
    ReconciliationCapability,
    ReconciliationContext,
    VerificationContext,
    VerificationStatus,
    semantic_reconciliation_result,
)
from gitopsctr.execution import DriverExecution
from gitopsctr.forges import (
    ChangeRequestResult,
    ChangeRequestSpec,
    ManualChangeRequest,
    ensure_change_request,
)
from gitopsctr.formats import (
    DocumentFormatError,
    document_candidates,
    load_document,
    load_project_config,
    write_document,
)
from gitopsctr.schemas import driver_schema, encoded_schema, export_schemas, resource_schema_url, show_schema

GIT_AUTHOR_NAME = os.environ.get("GITOPSCTR_GIT_AUTHOR_NAME", "gitopsctr")
GIT_AUTHOR_EMAIL = os.environ.get(
    "GITOPSCTR_GIT_AUTHOR_EMAIL",
    "gitopsctr@users.noreply.github.com",
)
REPOSITORY_ROOT = Path.cwd().resolve()


class OperationError(RuntimeError):
    pass


class ReferenceUnavailable(OperationError):
    pass


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
                "schema": 1,
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


def log_heading(message: str) -> None:
    """Write a visually distinct phase heading without polluting command result stdout."""
    print(f"\n==> {message}", file=sys.stderr, flush=True)


def log_status(status: str, message: str) -> None:
    """Write one consistently aligned deployment progress line."""
    print(f"    {status:<8} {message}", file=sys.stderr, flush=True)


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


def git(*args: str, check: bool = True, input_text: str | None = None, env=None):
    return run(
        "git",
        *args,
        check=check,
        input_text=input_text,
        env=env,
        cwd=REPOSITORY_ROOT,
    )


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
    remote_ref = f"refs/heads/{ref}"
    result = git("ls-remote", "--exit-code", "--heads", "origin", remote_ref, check=False)
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        raise OperationError(result.stderr.strip() or f"could not inspect {ref}")

    head = result.stdout.split()[0]
    git("fetch", "origin", f"{remote_ref}:refs/remotes/origin/{ref}")
    return head


def resolve_ref(ref: str, revision: str | None = None) -> str:
    head = fetch_ref(ref)
    if head is None:
        raise OperationError(f"ref {ref!r} does not exist")

    resolved = head if revision is None else git("rev-parse", f"{revision}^{{commit}}").stdout.strip()
    if revision is not None:
        result = git("merge-base", "--is-ancestor", resolved, head, check=False)
        if result.returncode != 0:
            raise OperationError(f"requested revision is not part of {ref} history")
    return resolved


def materialize_revision(revision: str, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise OperationError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tempfile.TemporaryFile() as stream:
        stream.write(archive)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as tar:
            tar.extractall(output, filter="data")


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
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        raise OperationError(f"tree is empty: {directory}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        index = str(Path(temporary_directory) / "index")
        identity = os.environ | {"GIT_INDEX_FILE": index}
        git("read-tree", "--empty", env=identity)
        for path in files:
            if path.is_symlink():
                raise OperationError(f"tree contains a symbolic link: {path}")
            relative = path.relative_to(directory).as_posix()
            blob = git("hash-object", "-w", str(path)).stdout.strip()
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", env=identity)
        tree = git("write-tree", env=identity).stdout.strip()

    commit_args = ["commit-tree", tree]
    if parent:
        commit_args.extend(["-p", parent])

    identity = os.environ | {
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }
    commit = git(
        *commit_args,
        input_text=f"{message}\n",
        env=identity,
    ).stdout.strip()
    git("push", "origin", f"{commit}:refs/heads/{ref}")
    return commit


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


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
        alternatives = document_candidates(path.parent, path.stem)
        if alternatives:
            path = alternatives[0]
    try:
        value = load_document(path)
    except (OSError, DocumentFormatError) as exc:
        raise OperationError(f"could not read {path}: {exc}") from exc
    return value


CORE_API_VERSION = "gitopsctr.io/v1"
UNIT_API_VERSION = "unit.gitopsctr.io/v1"


def normalize_environment_document(document: dict[str, Any], expected_name: str | None = None) -> dict[str, Any]:
    """Convert a resource envelope to the controller's internal environment shape."""
    if document.get("apiVersion") is None:
        return document
    if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Environment":
        raise OperationError("environment must use apiVersion gitopsctr.io/v1 and kind Environment")
    metadata = document.get("metadata")
    specification = document.get("spec")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not isinstance(specification, dict):
        raise OperationError("environment envelope requires metadata.name and a spec mapping")
    name = metadata["name"]
    if expected_name is not None and name != expected_name:
        raise OperationError(f"environment metadata.name must be {expected_name!r}")
    return {"schema": 1, "name": name, **specification}


def normalize_promotion_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("apiVersion") is None:
        return document
    if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Promotion":
        raise OperationError("promotion must use apiVersion gitopsctr.io/v1 and kind Promotion")
    specification = document.get("spec")
    if not isinstance(specification, dict):
        raise OperationError("promotion envelope requires a spec mapping")
    return {"schema": 1, **specification}


def normalize_unit_document(document: dict[str, Any], expected_name: str | None = None) -> dict[str, Any]:
    """Convert a unit resource envelope to the controller's internal shape."""
    if document.get("apiVersion") is None:
        return document
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    metadata = document.get("metadata")
    specification = document.get("spec")
    if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(metadata, dict):
        raise OperationError("unit envelope requires apiVersion, kind, and metadata")
    if api_version != UNIT_API_VERSION:
        raise OperationError(f"unsupported unit API version: {api_version!r}")
    driver = DRIVER_NAMES_BY_GVK.get(f"{api_version}/{kind}")
    if driver is None:
        raise OperationError(f"no installed unit driver handles {api_version}/{kind}")
    name = metadata.get("name")
    if not isinstance(name, str) or (expected_name is not None and name != expected_name):
        raise OperationError(f"unit metadata.name must be {expected_name or 'a non-empty name'!r}")
    if not isinstance(specification, dict):
        raise OperationError(f"unit {name} requires a spec mapping")
    return {"schema": 1, "name": name, "driver": driver, **specification}


def serialize_environment_document(environment: dict[str, Any]) -> dict[str, Any]:
    name = environment.get("name")
    if not isinstance(name, str):
        raise OperationError("environment is missing its name")
    specification = {key: value for key, value in environment.items() if key not in {"schema", "name", "$schema"}}
    return {
        "$schema": resource_schema_url(CORE_API_VERSION, "Environment"),
        "apiVersion": CORE_API_VERSION,
        "kind": "Environment",
        "metadata": {"name": name},
        "spec": specification,
    }


def serialize_promotion_document(promotion: dict[str, Any]) -> dict[str, Any]:
    specification = {key: value for key, value in promotion.items() if key not in {"schema", "$schema"}}
    return {
        "$schema": resource_schema_url(CORE_API_VERSION, "Promotion"),
        "apiVersion": CORE_API_VERSION,
        "kind": "Promotion",
        "metadata": {"name": str(specification.get("source", {}).get("environment", "promotion"))},
        "spec": specification,
    }


def serialize_unit_document(
    unit: dict[str, Any], driver_name: str | None = None, *, profile: str = "desired"
) -> dict[str, Any]:
    driver = driver_name or unit.get("driver")
    if not isinstance(driver, str) or driver not in UNIT_DRIVERS:
        raise OperationError(f"unit uses an unknown driver: {driver!r}")
    name = unit.get("name")
    if not isinstance(name, str):
        raise OperationError("unit is missing its name")
    specification = {key: value for key, value in unit.items() if key not in {"schema", "name", "driver", "$schema"}}
    return {
        "$schema": resource_schema_url(
            DRIVER_GVKS[driver].rsplit("/", 1)[0],
            DRIVER_GVKS[driver].rsplit("/", 1)[1],
            "authored" if profile == "authored" else "desired",
        ),
        "apiVersion": DRIVER_GVKS[driver].rsplit("/", 1)[0],
        "kind": DRIVER_GVKS[driver].rsplit("/", 1)[1],
        "metadata": {"name": name},
        "spec": specification,
    }


def normalize_receipt_document(document: dict[str, Any], expected_unit: str | None = None) -> dict[str, Any]:
    if document.get("apiVersion") is None:
        return document
    if document.get("apiVersion") != CORE_API_VERSION or document.get("kind") != "Receipt":
        raise OperationError("receipt must use apiVersion gitopsctr.io/v1 and kind Receipt")
    metadata = document.get("metadata")
    specification = document.get("spec")
    status = document.get("status")
    if not isinstance(metadata, dict) or not isinstance(specification, dict) or not isinstance(status, dict):
        raise OperationError("receipt envelope requires metadata, spec, and status mappings")
    name = metadata.get("name")
    subject = specification.get("subject")
    if not isinstance(name, str) or (expected_unit is not None and name != expected_unit):
        raise OperationError(f"receipt metadata.name must be {expected_unit or 'a unit name'}")
    if not isinstance(subject, dict):
        raise OperationError("receipt spec.subject is required")
    api_version = subject.get("apiVersion")
    kind = subject.get("kind")
    driver = DRIVER_NAMES_BY_GVK.get(f"{api_version}/{kind}")
    if driver is None:
        raise OperationError("receipt subject does not identify an installed unit driver")
    result = status.get("result", {})
    if not isinstance(result, dict):
        raise OperationError("receipt status.result must be a mapping")
    return {
        "schema": 1,
        "unit": name,
        "driver": driver,
        "desired": specification.get("desired", {}),
        "resolvedInputs": specification.get("resolvedInputs", {}),
        "controller": status.get("controller", {}),
        **result,
    }


def serialize_receipt_document(receipt: dict[str, Any]) -> dict[str, Any]:
    driver = receipt.get("driver")
    unit = receipt.get("unit")
    if not isinstance(driver, str) or driver not in UNIT_DRIVERS or not isinstance(unit, str):
        raise OperationError("receipt is missing a known driver or unit")
    reserved = {"schema", "unit", "driver", "desired", "resolvedInputs", "controller", "$schema"}
    return {
        "$schema": resource_schema_url(DRIVER_GVKS[driver].rsplit("/", 1)[0], DRIVER_GVKS[driver].rsplit("/", 1)[1], "receipt"),
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": unit},
        "spec": {
            "subject": {
                "apiVersion": DRIVER_GVKS[driver].rsplit("/", 1)[0],
                "kind": DRIVER_GVKS[driver].rsplit("/", 1)[1],
                "name": unit,
            },
            "desired": receipt.get("desired", {}),
            "resolvedInputs": receipt.get("resolvedInputs", {}),
        },
        "status": {
            "controller": receipt.get("controller", {}),
            "result": {key: value for key, value in receipt.items() if key not in reserved},
        },
    }


def load_receipt(path: Path, expected_unit: str | None = None) -> dict[str, Any]:
    document = load_json(path)
    if strict_resource_documents(path) and document.get("apiVersion") is None:
        raise OperationError(f"legacy receipt document is not valid in a migrated project: {path}")
    return normalize_receipt_document(document, expected_unit or path.stem)


def resource_documents_enabled(root: Path) -> bool:
    """Use envelopes once a project has opted into the new document layout."""
    if any((root / name).is_file() for name in ("gitopsctr.yaml", "gitopsctr.yml", ".gitopsctr.yaml", ".gitopsctr.yml")):
        return True
    for environment_root in (root / "deployment" / "environments").glob("*"):
        for name in ("environment.yaml", "environment.yml", "environment.json"):
            path = environment_root / name
            if path.is_file() and load_json(path).get("apiVersion") is not None:
                return True
    return False


def unit_document_path(root: Path, unit_name: str, project_root: Path | None = None) -> Path:
    directory = root / "units"
    candidates = document_candidates(directory, unit_name)
    if len(candidates) > 1:
        raise OperationError(f"multiple document formats exist for unit {unit_name}: {', '.join(map(str, candidates))}")
    if candidates:
        return candidates[0]
    if project_root is not None and resource_documents_enabled(project_root):
        return directory / f"{unit_name}{load_project_config(project_root).write_format.suffix}"
    return directory / f"{unit_name}.json"


def strict_resource_documents(path: Path) -> bool:
    for parent in (path.parent, *path.parents):
        if any(
            (parent / name).is_file()
            for name in ("gitopsctr.yaml", "gitopsctr.yml", ".gitopsctr.yaml", ".gitopsctr.yml")
        ):
            return True
    return False


def load_unit(path: Path, expected_name: str | None = None) -> dict[str, Any]:
    document = load_json(path)
    if strict_resource_documents(path) and document.get("apiVersion") is None:
        raise OperationError(f"legacy unit document is not valid in a migrated project: {path}")
    return normalize_unit_document(document, expected_name or path.stem)


def reference_document_path(root: Path, reference: str) -> Path:
    exact = root / reference
    if exact.is_file():
        return exact
    path = PurePosixPath(reference)
    if len(path.parts) == 2 and path.parts[0] == "units":
        return unit_document_path(root, path.stem)
    return exact


def write_unit(path: Path, unit: dict[str, Any], project_root: Path) -> Path:
    if resource_documents_enabled(project_root):
        selected = load_project_config(project_root).write_format
        return write_document(path.with_suffix(selected.suffix), serialize_unit_document(unit), format=selected)
    write_json(path.with_suffix(".json"), unit)
    return path.with_suffix(".json")


def validate_document(contract: DocumentContract, document: object, description: str) -> dict[str, Any]:
    try:
        return contract.validate(document)
    except ContractError as exc:
        raise OperationError(f"invalid {description}: {exc}") from exc


def validate_receipt_document(document: object, description: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise OperationError(f"invalid {description}: expected a JSON object")
    document = normalize_receipt_document(document)
    driver = document.get("driver")
    if driver is None and "$schema" not in document:
        # Pre-contract receipts remain readable; every newly written receipt carries a driver and $schema.
        return document
    if not isinstance(driver, str) or driver not in UNIT_DRIVERS:
        raise OperationError(f"invalid {description}: unknown driver {driver!r}")
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    candidate = {**document, "$schema": None}
    try:
        Draft202012Validator(driver_schema(driver, "receipt")).validate(candidate)
    except ValidationError as exc:
        raise OperationError(f"invalid {description}: {exc.message}") from exc
    return document


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_preferred_document(path: Path, value: dict[str, Any], project_root: Path) -> Path:
    """Write a generated document using the project's configured format."""
    if not resource_documents_enabled(project_root):
        write_json(path.with_suffix(".json"), value)
        return path.with_suffix(".json")
    try:
        selected = load_project_config(project_root).write_format
        if value.get("source") is not None and value.get("specificationRevision") is not None:
            value = serialize_promotion_document(value)
        elif value.get("unit") is not None and value.get("driver") is not None:
            value = serialize_receipt_document(value)
        return write_document(path.with_suffix(selected.suffix), value, format=selected)
    except DocumentFormatError as exc:
        raise OperationError(str(exc)) from exc


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


def unit_requires_reconciliation(unit: dict[str, Any]) -> bool:
    plugin_name = unit.get("driver")
    plugin = UNIT_DRIVERS.get(plugin_name) if isinstance(plugin_name, str) else None
    if plugin is None:
        raise OperationError(f"unit uses an unknown plugin: {plugin_name!r}")
    if not isinstance(plugin, ReconciliationCapability):
        return False
    try:
        return plugin.reconciliation_required(unit)
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


def validate_unit_materialization(desired_root: Path, unit_name: str, unit: dict[str, Any]) -> None:
    plugin_name = unit.get("driver")
    if isinstance(plugin_name, str) and plugin_name in UNIT_DRIVERS:
        validate_document(
            UNIT_DRIVERS[plugin_name].desired_unit_contract,
            unit,
            f"persisted desired {plugin_name} unit {unit_name}",
        )
    expects_materialization = isinstance(plugin_name, str) and plugin_name in MATERIALIZATION_DRIVERS
    descriptor = unit.get("materialization")
    if not expects_materialization:
        if descriptor is not None:
            raise OperationError(f"{unit_name} records materialization for a plugin without that capability")
        return
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "digest", "mediaType", "metadata"}:
        raise OperationError(f"{unit_name} has an invalid materialization descriptor")
    expected_path = f"manifests/{unit_name}"
    if descriptor.get("path") != expected_path:
        raise OperationError(f"{unit_name} materialization path must be {expected_path}")
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise OperationError(f"{unit_name} has an invalid materialization digest")
    if not isinstance(descriptor.get("mediaType"), str) or not descriptor["mediaType"]:
        raise OperationError(f"{unit_name} has an invalid materialization media type")
    if not isinstance(descriptor.get("metadata"), dict):
        raise OperationError(f"{unit_name} has invalid materialization metadata")
    actual = materialization_tree_digest(desired_root / expected_path)
    if actual != digest:
        raise OperationError(f"{unit_name} materialized payload does not match its digest")


def copy_unit_materialization(source: Path, destination: Path, unit_name: str, unit: dict[str, Any]) -> None:
    validate_unit_materialization(source, unit_name, unit)
    target = destination / "manifests" / unit_name
    if target.exists():
        shutil.rmtree(target)
    descriptor = unit.get("materialization")
    if descriptor is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / "manifests" / unit_name, target)


def require_unit_specification(
    specification: dict[str, Any], expected_name: str | None = None
) -> tuple[str, dict[str, Any]]:
    specification = normalize_unit_document(specification, expected_name)
    name = specification.get("name")
    driver = specification.get("driver")
    source = specification.get("source")
    if (
        specification.get("schema") != 1
        or not isinstance(name, str)
        or (expected_name is not None and name != expected_name)
    ):
        raise OperationError(f"invalid unit specification: {expected_name or name!r}")
    if driver not in UNIT_DRIVERS:
        raise OperationError(f"{name} uses an unknown driver: {driver!r}")
    validate_document(UNIT_DRIVERS[driver].unit_contract, specification, f"{driver} unit {expected_name or name}")
    if not isinstance(source, dict):
        raise OperationError(f"{name} requires a source object")
    safe_source_path(source.get("path"), f"{name} source path")
    inputs = source.get("inputs")
    if inputs is not None and (not isinstance(inputs, list) or not all(isinstance(value, str) for value in inputs)):
        raise OperationError(f"{name} source inputs must be a list of paths or glob patterns")
    artifacts = specification.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(re.fullmatch(r"[a-z0-9-]+\.json", str(value)) for value in artifacts):
        raise OperationError(f"{name} artifacts must be JSON filenames")
    return driver, source


def unit_input_hash(specification: dict[str, Any], source_root: Path) -> str:
    driver, source = require_unit_specification(specification)
    inputs = source.get("inputs")
    if inputs is None:
        source_path = "."
        inputs = [source["path"]]
    else:
        source_path = source["path"]
    return hash_source_inputs(
        source_root,
        source_path,
        inputs,
        {
            "kind": "unit",
            "driver": driver,
            "driverVersion": DRIVER_VERSIONS[driver],
            "specification": specification,
        },
    )


def prior_unit_source(
    unit_name: str,
    current_desired: Path,
    legacy: dict[str, Any] | None,
) -> tuple[str, str] | None:
    current_path = unit_document_path(current_desired, unit_name)
    if current_path.is_file():
        source = load_unit(current_path).get("source", {})
        revision = source.get("revision")
        input_hash = source.get("inputHash")
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


def current_receipt(observed: Path, candidate_units: Path, unit_name: str) -> dict[str, Any] | None:
    receipt_path = unit_document_path(observed, unit_name)
    unit_path = unit_document_path(candidate_units.parent, unit_name)
    if not receipt_path.is_file() or not unit_path.is_file():
        return None
    receipt = load_receipt(receipt_path, unit_name)
    validate_receipt_document(receipt, f"persisted receipt for {unit_name}")
    if receipt.get("desired", {}).get("unitBlob") != file_blob(unit_path):
        return None
    return receipt


def resolve_template(
    value: Any,
    candidate: Path,
    observed: Path,
    observed_revision: str | None,
    dry: bool = False,
    promotion: Path | None = None,
) -> tuple[Any, dict[str, str], dict[str, str]]:
    promotion_inputs: dict[str, str] = {}
    observed_inputs: dict[str, str] = {}

    def resolve(candidate_value: Any) -> Any:
        if isinstance(candidate_value, list):
            return [resolve(item) for item in candidate_value]
        if not isinstance(candidate_value, dict):
            return candidate_value
        reference_keys = {"fromObservation", "fromPromotion"}.intersection(candidate_value)
        if not reference_keys:
            return {name: resolve(item) for name, item in candidate_value.items()}
        if len(reference_keys) != 1 or set(candidate_value) - {
            *reference_keys,
            "pointer",
            "dryFallback",
        }:
            raise OperationError("invalid observation or promotion reference")
        reference_type = reference_keys.pop()
        reference = candidate_value.get(reference_type)
        pointer = candidate_value.get("pointer", "")
        pattern = r"units/[a-z0-9-]+\.(?:json|ya?ml)"
        if not isinstance(reference, str) or not re.fullmatch(pattern, reference):
            raise OperationError(f"invalid {reference_type} path: {reference!r}")
        if not isinstance(pointer, str):
            raise OperationError(f"invalid JSON pointer for {reference!r}")
        try:
            if reference_type == "fromPromotion":
                if promotion is None:
                    raise ReferenceUnavailable(f"promotion does not exist: {reference}")
                path = reference_document_path(promotion, reference)
                fingerprints = promotion_inputs
            else:
                if observed_revision is None:
                    raise ReferenceUnavailable(f"observation does not exist: {reference}")
                unit_name = Path(reference).stem
                if current_receipt(observed, candidate / "units", unit_name) is None:
                    raise ReferenceUnavailable(f"observation is stale: {reference}")
                path = reference_document_path(observed, reference)
                fingerprints = observed_inputs
            if not path.is_file():
                raise ReferenceUnavailable(f"referenced file does not exist: {reference}")
            fingerprints[reference] = file_blob(path)
            return json_pointer(load_json(path), pointer)
        except ReferenceUnavailable:
            if dry and "dryFallback" in candidate_value:
                return resolve(candidate_value["dryFallback"])
            raise

    return resolve(value), promotion_inputs, observed_inputs


def resolved_unit_source(
    specification: dict[str, Any],
    source_root: Path,
    source_revision: str,
    current_desired: Path,
    legacy: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    driver, source = require_unit_specification(specification)
    input_hash = unit_input_hash(specification, source_root)
    revision = source_revision
    prior = prior_unit_source(specification["name"], current_desired, legacy)
    inputs_changed = prior is None
    if prior is not None:
        prior_revision, prior_hash = prior
        previous_unit_path = unit_document_path(current_desired, specification["name"])
        if (
            specification.get("driver") == "oci-images"
            and "environment" not in specification
            and previous_unit_path.is_file()
        ):
            previous_environment = load_unit(previous_unit_path).get("environment")
            if isinstance(previous_environment, str):
                legacy_specification = json.loads(json.dumps(specification))
                legacy_specification["environment"] = previous_environment
                if unit_input_hash(legacy_specification, source_root) == prior_hash:
                    input_hash = prior_hash
        if not prior_hash:
            with tempfile.TemporaryDirectory() as prior_directory:
                prior_root = Path(prior_directory) / "source"
                materialize_revision(prior_revision, prior_root)
                prior_hash = unit_input_hash(specification, prior_root)
        if prior_hash == input_hash:
            revision = prior_revision
        else:
            inputs_changed = True
    return (
        {
            **source,
            "revision": revision,
            "inputHash": input_hash,
            "driverVersion": DRIVER_VERSIONS[driver],
        },
        inputs_changed,
    )


def load_environment(source_root: Path, environment_name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", environment_name):
        raise OperationError(f"invalid environment name: {environment_name!r}")
    environment_root = source_root / "deployment" / "environments" / environment_name
    environment_paths = document_candidates(environment_root, "environment")
    if len(environment_paths) != 1:
        raise OperationError(f"expected exactly one environment document for {environment_name}")
    environment_document = load_json(environment_paths[0])
    if resource_documents_enabled(source_root) and environment_document.get("apiVersion") is None:
        raise OperationError(f"legacy environment document is not valid in a migrated project: {environment_paths[0]}")
    environment = normalize_environment_document(environment_document, environment_name)
    if environment.get("schema") != 1 or environment.get("name") != environment_name:
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
    if not isinstance(configured, dict) or set(configured) - {"desired", "observed"}:
        raise OperationError(f"{environment_name} refs must contain desired and observed only")
    desired_ref = desired_override or configured.get("desired") or f"deploy/{environment_name}"
    observed_ref = observed_override or configured.get("observed") or f"observed/{environment_name}"
    if not all(isinstance(ref, str) and ref for ref in (desired_ref, observed_ref)):
        raise OperationError(f"{environment_name} desired and observed refs must be strings")
    if desired_ref == observed_ref:
        raise OperationError(f"{environment_name} desired and observed refs must differ")
    return desired_ref, observed_ref


def load_environment_specifications(source_root: Path, environment_name: str) -> dict[str, dict[str, Any]]:
    load_environment(source_root, environment_name)
    environment_root = source_root / "deployment" / "environments" / environment_name
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
    specifications = {path.stem: normalize_unit_document(load_json(path), path.stem) for path in unit_paths}
    for unit_name, specification in specifications.items():
        require_unit_specification(specification, unit_name)
    for consumer, specification in specifications.items():
        for reference in reference_paths(specification, "fromObservation"):
            producer = Path(reference).stem
            if producer in specifications and not unit_requires_reconciliation(specifications[producer]):
                raise OperationError(f"{consumer} cannot observe materialization-only unit {producer!r}")
    return specifications


def require_environment_unit(source_root: Path, environment_name: str, unit_name: str) -> None:
    specifications = load_environment_specifications(source_root, environment_name)
    if unit_name not in specifications:
        available = ", ".join(sorted(specifications))
        raise OperationError(
            f"unknown unit {unit_name!r} for environment {environment_name!r}; available units: {available}"
        )


def reconciliation_statuses(unit_names: list[str], desired: Path, observed: Path) -> list[tuple[str, str, str]]:
    statuses = []
    for unit_name in unit_names:
        unit_path = unit_document_path(desired, unit_name)
        receipt_path = unit_document_path(observed, unit_name)
        if not unit_path.is_file():
            statuses.append((unit_name, "WAIT", "desired inputs are not materialized"))
            continue
        unit = load_unit(unit_path, unit_name)
        validate_unit_materialization(desired, unit_name, unit)
        if not unit_requires_reconciliation(unit):
            statuses.append((unit_name, "MATERIALIZED", "desired payload is published for external delivery"))
            continue
        if not receipt_path.is_file():
            statuses.append((unit_name, "READY", "no observation receipt"))
            continue
        receipt = load_receipt(receipt_path, unit_name)
        validate_receipt_document(receipt, f"persisted receipt for {unit_name}")
        if receipt.get("desired", {}).get("unitBlob") == file_blob(unit_path):
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
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_desired_revision: str,
) -> UnitChangeExplanation:
    previous_source = previous.get("source", {})
    current_source = current.get("source", {})
    if not isinstance(previous_source, dict):
        previous_source = {}
    if not isinstance(current_source, dict):
        current_source = {}
    causes = []
    if previous.get("driver") != current.get("driver") or previous_source.get("driverVersion") != current_source.get(
        "driverVersion"
    ):
        causes.append("reconciliation driver changed")
    source_fingerprint_changed = previous_source.get("inputHash") != current_source.get("inputHash")
    commits, files = source_change_evidence(previous_source, current_source) if source_fingerprint_changed else ((), ())
    if files:
        causes.append("source inputs changed")
    previous_inputs = previous.get("resolvedInputs", {})
    current_inputs = current.get("resolvedInputs", {})
    if not isinstance(previous_inputs, dict):
        previous_inputs = {}
    if not isinstance(current_inputs, dict):
        current_inputs = {}
    previous_observed = previous_inputs.get("observed", {})
    current_observed = current_inputs.get("observed", {})
    if not isinstance(previous_observed, dict):
        previous_observed = {}
    if not isinstance(current_observed, dict):
        current_observed = {}
    if previous_observed != current_observed:
        changed = sorted(set(previous_observed) | set(current_observed))
        causes.append("upstream observations changed: " + ", ".join(Path(path).stem for path in changed))
    previous_promotion = previous_inputs.get("promotion", {})
    current_promotion = current_inputs.get("promotion", {})
    if not isinstance(previous_promotion, dict):
        previous_promotion = {}
    if not isinstance(current_promotion, dict):
        current_promotion = {}
    if previous_promotion != current_promotion:
        changed = sorted(set(previous_promotion) | set(current_promotion))
        causes.append("reviewed promotion inputs changed: " + ", ".join(changed))
    ignored = {"driver", "source", "resolvedInputs"}
    previous_specification = {key: value for key, value in previous.items() if key not in ignored}
    current_specification = {key: value for key, value in current.items() if key not in ignored}
    specification_paths = tuple(changed_json_paths(previous_specification, current_specification))
    if specification_paths:
        causes.append("unit specification changed")
    if source_fingerprint_changed and not files and not causes:
        causes.append("source input fingerprint changed")
    if not causes:
        causes.append("desired unit content changed")
    return UnitChangeExplanation(
        previous_desired_revision=previous_desired_revision,
        previous_source_revision=(
            previous_source.get("revision") if isinstance(previous_source.get("revision"), str) else None
        ),
        current_source_revision=(
            current_source.get("revision") if isinstance(current_source.get("revision"), str) else None
        ),
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
    validate_receipt_document(receipt, f"persisted receipt for {unit_name}")
    previous_revision = receipt.get("desired", {}).get("revision")
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
    return classify_unit_change(previous, load_unit(current_path, unit_name), previous_revision)


def log_bounded_items(status: str, values: tuple[str, ...], verbose: bool) -> None:
    limit = len(values) if verbose else 5
    for value in values[:limit]:
        log_status(status, value)
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
        f"desired {short_revision(explanation.previous_desired_revision)}; "
        f"source {short_revision(explanation.previous_source_revision)}",
    )
    log_status(
        "CURRENT",
        f"desired {short_revision(desired_revision)}; source {short_revision(explanation.current_source_revision)}",
    )
    for cause in explanation.causes:
        log_status("CAUSE", cause)
    log_bounded_items("COMMIT", explanation.commits, verbose)
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
    log_heading(f"Reconciliation status for {environment_name}")
    for unit_name, status, reason in statuses:
        log_status(status, f"{unit_name}: {reason}")
        if status == "READY" and desired_revision is not None and desired is not None and observed is not None:
            log_unit_change_explanation(unit_name, desired_revision, desired, observed, verbose)
    ready = [unit_name for unit_name, status, _ in statuses if status == "READY"]
    if ready:
        log_status("NEXT", ", ".join(ready))
    elif any(status == "WAIT" for _, status, _ in statuses):
        log_status("NEXT", "none ready; waiting for upstream observations")
    elif any(status == "MATERIALIZED" for _, status, _ in statuses):
        log_status("NEXT", "none; all units are complete")
    else:
        log_status("NEXT", "none; all units are clean")


def convergence_plan_rows(
    statuses: list[tuple[str, str, str]],
    order: list[str],
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
        message = unit_name if status in {"CLEAN", "MATERIALIZED"} else f"{unit_name}: {reason}"
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
    unit = load_unit(unit_document_path(desired, unit_name), unit_name)
    driver_value = unit.get("driver")
    driver = driver_value if isinstance(driver_value, str) else "unknown"
    explanation = unit_change_explanation(unit_name, desired, observed)
    log_heading(f"Next action: {unit_name}")
    log_status("DRIVER", driver)
    if explanation is None:
        log_status("CAUSE", reason)
    else:
        if explanation.previous_source_revision or explanation.current_source_revision:
            log_status(
                "SOURCE",
                f"{short_revision(explanation.previous_source_revision)} -> "
                f"{short_revision(explanation.current_source_revision)}",
            )
        for cause in explanation.causes:
            log_status("CAUSE", cause)
        if commit := bounded_evidence(explanation.commits):
            log_status("COMMIT", commit)
        if file := bounded_evidence(explanation.files):
            log_status("FILE", file)
        if field := bounded_evidence(explanation.specification_paths):
            log_status("FIELD", field)
    log_status("WRITES", f"driver effects; receipt to {observed_ref} on success")


def materialize_resolved_unit(
    environment_name: str,
    unit_name: str,
    resolved: dict[str, Any],
    source_root: Path,
    source_revision: str,
    current_desired: Path,
    candidate: Path,
) -> dict[str, Any]:
    plugin_name = resolved.get("driver")
    if not isinstance(plugin_name, str) or plugin_name not in UNIT_DRIVERS:
        raise OperationError(f"{unit_name} uses an unknown unit plugin: {plugin_name!r}")
    unit_plugin = UNIT_DRIVERS[plugin_name]
    desired_schema_id = str(driver_schema(plugin_name, "desired-unit")["$id"])
    resolved = with_schema({key: value for key, value in resolved.items() if key != "$schema"}, desired_schema_id)
    plugin = MATERIALIZATION_DRIVERS.get(plugin_name) if isinstance(plugin_name, str) else None
    if plugin is None:
        validate_document(unit_plugin.desired_unit_contract, resolved, f"materialized {plugin_name} unit {unit_name}")
        return resolved

    previous_path = unit_document_path(current_desired, unit_name)
    if previous_path.is_file():
        previous = load_unit(previous_path, unit_name)
        previous_without_materialization = {
            name: value for name, value in previous.items() if name != "materialization"
        }
        if previous_without_materialization == resolved:
            validate_unit_materialization(current_desired, unit_name, previous)
            copy_unit_materialization(current_desired, candidate, unit_name, previous)
            reused = {**resolved, "materialization": previous["materialization"]}
            validate_document(unit_plugin.desired_unit_contract, reused, f"materialized {plugin_name} unit {unit_name}")
            return reused

    output_root = candidate / "manifests" / unit_name
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    source = resolved.get("source")
    if not isinstance(source, dict):
        raise OperationError(f"{unit_name} has no resolved source for materialization")
    selected_revision = source.get("revision")
    source_path = source.get("path")
    if not isinstance(selected_revision, str) or not isinstance(source_path, str):
        raise OperationError(f"{unit_name} has an invalid materialization source")

    def run_materializer(selected_source_root: Path) -> MaterializationResult:
        result = plugin.materialize(
            MaterializationContext(
                environment=environment_name,
                source_root=selected_source_root,
                source_revision=selected_revision,
                source_path=source_path,
                unit=resolved,
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
    resolved = {
        **resolved,
        "materialization": {
            "path": f"manifests/{unit_name}",
            "digest": materialization_tree_digest(output_root),
            "mediaType": result.media_type,
            "metadata": result.metadata,
        },
    }
    validate_document(unit_plugin.desired_unit_contract, resolved, f"materialized {plugin_name} unit {unit_name}")
    validate_unit_materialization(candidate, unit_name, resolved)
    return resolved


def build_desired_candidate(
    environment_name: str,
    source_root: Path,
    source_revision: str,
    current_desired: Path,
    observed: Path,
    observed_revision: str | None,
    candidate: Path,
    dry: bool = False,
    promotion: PromotionContext | None = None,
    verbose: bool = True,
) -> None:
    if verbose:
        log_heading(f"Resolve desired state for {environment_name}")
        log_status("SOURCE", f"candidate revision {short_revision(source_revision)}")
        log_status("DESIRED", "no current state" if not any(current_desired.iterdir()) else "loaded")
        log_status(
            "OBSERVED",
            f"revision {short_revision(observed_revision)}" if observed_revision else "no observations yet",
        )
    legacy_path = current_desired / "release.json"
    legacy = load_json(legacy_path) if legacy_path.is_file() else None
    specifications = load_environment_specifications(source_root, environment_name)
    candidate_units = candidate / "units"
    candidate_units.mkdir(parents=True)
    if promotion is not None:
        write_preferred_document(candidate / "promotion.json", promotion.document(), source_root)

    prepared: dict[str, dict[str, Any]] = {}
    for unit_name, specification in specifications.items():
        resolved_source, source_changed = resolved_unit_source(
            specification, source_root, source_revision, current_desired, legacy
        )
        prepared[unit_name] = {
            **json.loads(json.dumps(specification)),
            "source": resolved_source,
        }
        previous_unit = unit_document_path(current_desired, unit_name)
        if not previous_unit.is_file():
            source_resolution = "new unit; use candidate revision"
        elif source_changed:
            source_resolution = "inputs changed; use candidate revision"
        else:
            source_resolution = f"inputs unchanged; retain {short_revision(resolved_source['revision'])}"
        if verbose:
            log_status("CHECK", f"{unit_name}: {source_resolution}")

    unresolved = set(prepared)
    unavailable: dict[str, str] = {}
    while unresolved:
        progressed = False
        for unit_name in sorted(unresolved):
            try:
                resolved, promotion_inputs, observed_inputs = resolve_template(
                    prepared[unit_name],
                    candidate,
                    observed,
                    observed_revision,
                    dry,
                    promotion.desired_root if promotion is not None else None,
                )
            except ReferenceUnavailable as exc:
                unavailable[unit_name] = str(exc)
                continue
            if promotion_inputs or observed_inputs:
                resolved["resolvedInputs"] = {}
                if promotion_inputs:
                    resolved["resolvedInputs"]["promotion"] = promotion_inputs
                if observed_inputs:
                    resolved["resolvedInputs"]["observed"] = observed_inputs
            resolved = materialize_resolved_unit(
                environment_name,
                unit_name,
                resolved,
                source_root,
                source_revision,
                current_desired,
                candidate,
            )
            candidate_unit = write_unit(candidate_units / f"{unit_name}.json", resolved, source_root)
            previous_unit = unit_document_path(current_desired, unit_name)
            previous = load_unit(previous_unit, unit_name) if previous_unit.is_file() else None
            previous_observations = (
                previous.get("resolvedInputs", {}).get("observed", {}) if previous is not None else {}
            )
            previous_promotions = (
                previous.get("resolvedInputs", {}).get("promotion", {}) if previous is not None else {}
            )
            if promotion_inputs:
                promotion_resolution = (
                    "new promotion changes resolved inputs"
                    if previous_promotions != promotion_inputs
                    else "promotion already matches resolved inputs"
                )
                if verbose:
                    log_status("PROMOTE", f"{unit_name}: {promotion_resolution}")
            if observed_inputs:
                observation_resolution = (
                    "new observation changes resolved inputs"
                    if previous_observations != observed_inputs
                    else "observations already match resolved inputs"
                )
                if verbose:
                    log_status("OBSERVE", f"{unit_name}: {observation_resolution}")
            changed = not previous_unit.is_file() or previous_unit.read_bytes() != candidate_unit.read_bytes()
            if verbose:
                log_status(
                    "UPDATE" if changed else "KEEP",
                    f"{unit_name}: {'desired state changed' if changed else 'already resolved'}",
                )
            unresolved.remove(unit_name)
            unavailable.pop(unit_name, None)
            progressed = True
        if not progressed:
            break

    for unit_name in sorted(unresolved):
        previous = unit_document_path(current_desired, unit_name)
        previous_driver = load_unit(previous, unit_name).get("driver") if previous.is_file() else None
        next_driver = prepared[unit_name]["driver"]
        if previous_driver == next_driver:
            shutil.copy2(previous, candidate_units / previous.name)
            copy_unit_materialization(current_desired, candidate, unit_name, load_unit(previous, unit_name))
            resolution = "retain previous desired state"
        elif previous_driver is not None:
            resolution = f"omit previous {previous_driver} desired state while transitioning to {next_driver}"
        else:
            resolution = "omit until its inputs are available"
        if verbose:
            log_status("WAIT", f"{unit_name}: {unavailable[unit_name]}; {resolution}")


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
    unit = load_unit(unit_path, unit_name)
    try:
        validate_unit_materialization(desired, unit_name, unit)
        if not unit_requires_reconciliation(unit):
            return True
    except (DriverError, OperationError):
        return False
    if not receipt_path.is_file():
        return False
    receipt = load_receipt(receipt_path, unit_name)
    try:
        validate_receipt_document(receipt, f"historical receipt for {unit_name}")
    except OperationError:
        return False
    driver = unit.get("driver")
    desired_evidence = receipt.get("desired")
    if (
        receipt.get("schema") != 1
        or receipt.get("unit") != unit_name
        or not isinstance(driver, str)
        or receipt.get("driver") != driver
        or not isinstance(desired_evidence, dict)
        or not re.fullmatch(r"[0-9a-f]{40}", str(desired_evidence.get("revision", "")))
        or desired_evidence.get("unitBlob") != file_blob(unit_path)
    ):
        return False
    try:
        semantic_reconciliation_result(driver, receipt)
    except DriverError:
        return False
    return True


def require_clean_source(desired: Path, observed: Path, minimum_evidence: str = "reconciled") -> None:
    unit_names = sorted({path.stem for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}})
    if not unit_names:
        raise OperationError("promotion source desired state has no units")
    unresolved = [
        unit_name for unit_name in unit_names if contains_reference(load_unit(unit_document_path(desired, unit_name), unit_name))
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
        f"desired revision {short_revision(desired_revision)} does not record its specification revision"
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
        if unit_requires_reconciliation(load_unit(unit_document_path(desired, unit_name), unit_name))
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


def downstream_unit_closure(specifications: dict[str, dict[str, Any]], selected: list[str]) -> list[str]:
    consumers: dict[str, set[str]] = {unit: set() for unit in specifications}
    for consumer, specification in specifications.items():
        for reference in reference_paths(specification, "fromObservation"):
            producer = Path(reference).stem
            if producer not in specifications:
                raise OperationError(f"{consumer} references unknown observation unit {producer!r}")
            consumers[producer].add(consumer)
    closure: set[str] = set()
    pending = list(selected)
    while pending:
        producer = pending.pop()
        for consumer in consumers[producer]:
            if consumer not in closure and consumer not in selected:
                closure.add(consumer)
                pending.append(consumer)
    return sorted(closure)


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
) -> tuple[str | None, bool]:
    desired_override = desired_ref
    observed_override = observed_ref
    requested_source_revision = resolve_advance_source_revision(REPOSITORY_ROOT, environment, source_revision)
    if require_source_ref and requested_source_revision is None:
        raise OperationError("--require-source-ref applies only to source-tracked environments")
    if verbose:
        log_heading(f"Advance desired state for {environment}")
        log_status(
            "START",
            (
                f"environment {environment} from {short_revision(requested_source_revision)}"
                if requested_source_revision is not None
                else f"environment {environment} from its merged promotion"
            ),
        )
    if requested_source_revision is None:
        desired_ref, observed_ref = deployment_refs(REPOSITORY_ROOT, environment, desired_ref, observed_ref)
    else:
        with tempfile.TemporaryDirectory() as probe_directory:
            probe_root = Path(probe_directory) / "source"
            materialize_revision(requested_source_revision, probe_root)
            desired_ref, observed_ref = deployment_refs(probe_root, environment, desired_ref, observed_ref)
    if verbose:
        log_status("REFS", f"desired {desired_ref}; observed {observed_ref}")
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
                    f"use reviewed specification {short_revision(effective_source_revision)} from promotion",
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
                verbose=verbose,
            )
            if current_revision and directory_files(current_desired) == directory_files(candidate):
                if verbose:
                    log_status(
                        "KEEP",
                        f"{desired_ref} already resolved at {short_revision(current_revision)}",
                    )
                if summarize:
                    log_reconciliation_summary(environment, source_root, candidate, observed)
                return current_revision, False
            if dry:
                if verbose:
                    log_status("DRY", f"{desired_ref} would be updated")
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
                    log_status("UPDATE", f"{desired_ref} advanced to {short_revision(revision)}")
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
    if git("check-ref-format", "--branch", candidate_ref, check=False).returncode != 0:
        raise OperationError(f"invalid change candidate ref: {candidate_ref!r}")
    if candidate_ref == target_ref:
        raise OperationError("change candidate ref conflicts with target desired state")
    existing_candidate = fetch_ref(candidate_ref)
    if existing_candidate is not None:
        with tempfile.TemporaryDirectory() as existing_directory:
            existing_root = Path(existing_directory) / "candidate"
            materialize_revision(existing_candidate, existing_root)
            if directory_files(existing_root) != directory_files(candidate):
                raise OperationError(f"change candidate ref exists with different state: {candidate_ref}")
        candidate_revision = existing_candidate
        log_status("KEEP", f"reuse existing candidate {candidate_ref}")
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
    log_heading(f"Promote {args.from_environment} to {args.to_environment}")
    log_status("SPEC", f"reviewed source {short_revision(specification_revision)}")
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
            f"{source_desired_ref} {short_revision(source_desired_revision)} is {evidence_label} at "
            f"{short_revision(source_observed_revision)}",
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
                "schema": 1,
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
                f"created inert {target_desired_ref} at {short_revision(target_revision)}",
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

        commit_message = f"Promote {args.from_environment} to {args.to_environment} from {source_desired_revision}"
        title = f"Promote {args.from_environment} to {args.to_environment}"
        body = (
            f"Promotes reconciled desired state from `{source_desired_revision}`. "
            f"After merge, reconcile `{args.to_environment}`."
        )
        outcome: ChangeRequestResult | ManualChangeRequest | None = None
        if gate == "pullRequest":
            candidate_ref = args.candidate_ref or (
                f"promotion/{args.to_environment}/{source_desired_revision[:12]}-{specification_revision[:12]}"
            )
            if candidate_ref in {source_desired_ref, target_observed_ref}:
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
                f"{candidate_ref} at {short_revision(change_revision)} targets {target_desired_ref}",
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
                f"{target_desired_ref} advanced to {short_revision(change_revision)}",
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
    gate = change_gate(REPOSITORY_ROOT, environment)
    if dry:
        log_status("DRY", f"{target_ref} would receive {title.lower()}")
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
            f"{candidate_ref} at {short_revision(revision)} targets {target_ref}",
        )
        return revision, outcome
    revision = publish_tree(target_ref, candidate, target_revision, commit_message)
    log_status("UPDATE", f"{target_ref} advanced to {short_revision(revision)}")
    return revision, None


def validate_materialized_desired(
    environment: str,
    desired_revision: str,
    desired: Path,
    source: Path,
    description: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    specifications = load_environment_specifications(source, environment)
    expected_units = sorted(specifications)
    desired_units = sorted(
        {path.stem for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}}
    )
    if desired_units != expected_units:
        raise OperationError(f"{description} {short_revision(desired_revision)} is not fully materialized")
    for unit_name in desired_units:
        unit = load_unit(unit_document_path(desired, unit_name), unit_name)
        if contains_reference(unit):
            raise OperationError(f"{description} unit {unit_name} contains unresolved inputs")
        driver, _source = require_unit(unit, unit_name)
        validate_unit_materialization(desired, unit_name, unit)
        if driver != specifications[unit_name].get("driver"):
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
    log_heading(f"Roll back {args.environment}")
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
            current_driver = current_specifications[unit_name].get("driver")
            target_driver = target_specifications[unit_name].get("driver")
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
                historical_unit = load_unit(historical_path, unit_name)
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
                load_unit(unit_document_path(candidate, unit_name), unit_name),
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
        candidate_hash = hashlib.sha256(canonical_json(provenance)).hexdigest()[:12]
        candidate_ref = f"rollback/{args.environment}/{target_revision[:12]}-{candidate_hash}"
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
        log_heading(f"Deployment status for {args.environment}")
        log_status(
            "DESIRED",
            f"{desired_ref} at {short_revision(desired_revision)}"
            if desired_revision
            else f"{desired_ref} does not exist",
        )
        log_status(
            "OBSERVED",
            f"{observed_ref} at {short_revision(observed_revision)}"
            if observed_revision
            else f"{observed_ref} has no receipts yet",
        )
        specifications = load_environment_specifications(REPOSITORY_ROOT, args.environment)
        statuses = reconciliation_statuses(sorted(specifications), desired, observed)
        log_reconciliation_status(
            args.environment,
            statuses,
            desired_revision,
            desired,
            observed,
            args.verbose,
        )


def command_facts(args: argparse.Namespace) -> None:
    _, observed_ref = deployment_refs(
        REPOSITORY_ROOT,
        args.environment,
        None,
        args.observed_ref,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        observed = Path(temporary_directory) / "observed"
        observed_revision = observed_tree(observed_ref, observed)
        receipts = sorted(
            path for path in (observed / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}
        )
        if args.unit:
            receipt = unit_document_path(observed, args.unit)
            if not receipt.is_file():
                raise OperationError(f"{observed_ref} has no receipt for {args.unit}")
            receipts = [receipt]

        metadata = {"$schema", "schema", "unit", "driver", "desired", "resolvedInputs", "controller"}
        units = {}
        for path in receipts:
            receipt = load_receipt(path, path.stem)
            unit_name = path.stem
            validate_receipt_document(receipt, f"observation receipt units/{path.name}")
            if receipt.get("schema") != 1 or receipt.get("unit") != unit_name:
                raise OperationError(f"invalid observation receipt: units/{path.name}")
            units[unit_name] = {key: value for key, value in receipt.items() if key not in metadata}

        result = {
            "schema": 1,
            "environment": args.environment,
            "observed": {"ref": observed_ref, "revision": observed_revision},
            "units": units,
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return

        revision = short_revision(observed_revision) if observed_revision else "no receipts"
        print(f"Observed facts for {args.environment} ({observed_ref} at {revision})")
        for unit_name, facts in units.items():
            print(f"\n{unit_name}")
            print(json.dumps(facts, indent=2, sort_keys=True))


def command_verify(args: argparse.Namespace) -> None:
    desired_ref, _ = deployment_refs(REPOSITORY_ROOT, args.environment)
    log_heading(f"Verify {args.environment}")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        desired = temporary / "desired"
        desired_revision = resolve_ref(desired_ref)
        materialize_revision(desired_revision, desired)
        log_status("DESIRED", f"{desired_ref} at {short_revision(desired_revision)}")

        unit_paths = sorted(
            path for path in (desired / "units").glob("*") if path.suffix in {".json", ".yaml", ".yml"}
        )
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

        prepared: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        for unit_name in selected:
            unit = load_json(available[unit_name])
            driver_name, source = require_unit(unit, unit_name)
            validate_unit_materialization(desired, unit_name, unit)
            if contains_reference(unit):
                raise OperationError(f"{unit_name} desired state is not fully materialized")
            if driver_name not in VERIFICATION_DRIVERS:
                raise OperationError(f"{unit_name} uses {driver_name}, which does not support verification")
            prepared.append((unit_name, driver_name, unit, source))

        drifted: list[str] = []
        for unit_name, driver_name, unit, source in prepared:
            log_status("VERIFY", f"{unit_name} ({driver_name})")
            source_root = temporary / "sources" / unit_name
            materialize_revision(source["revision"], source_root)
            result = VERIFICATION_DRIVERS[driver_name].verify(
                VerificationContext(
                    environment=args.environment,
                    desired_root=desired,
                    desired_revision=desired_revision,
                    source_root=source_root,
                    source_revision=source["revision"],
                    source_path=source["path"],
                    unit=unit,
                    inputs=unit.get("inputs", {}),
                    execution=DriverExecution.console(),
                )
            )
            if result.status is VerificationStatus.CLEAN:
                log_status("CLEAN", unit_name)
            elif result.status is VerificationStatus.DRIFT:
                drifted.append(unit_name)
                log_status("DRIFT", unit_name)
            else:
                raise DriverError(f"{driver_name} returned an invalid verification status: {result.status!r}")

    if drifted:
        log_status("RESULT", f"DRIFT: {', '.join(drifted)}")
        raise OperationError(f"verification detected drift in: {', '.join(drifted)}")
    log_status("RESULT", "CLEAN")


def require_unit(unit: dict[str, Any], unit_name: str) -> tuple[str, dict[str, str]]:
    if unit.get("schema") != 1 or unit.get("name") != unit_name:
        raise OperationError(f"invalid desired unit: {unit_name}")
    driver = unit.get("driver")
    if driver not in UNIT_DRIVERS:
        raise OperationError(f"{unit_name} uses an unknown unit plugin: {driver!r}")
    validate_document(UNIT_DRIVERS[driver].desired_unit_contract, unit, f"desired {driver} unit {unit_name}")
    source = unit.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        raise OperationError(f"{unit_name} has an invalid source")
    safe_source_path(source["path"], f"{unit_name} source path")
    if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))):
        raise OperationError(f"{unit_name} has an invalid source revision")
    recorded_version = source.get("driverVersion")
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


def contains_reference(value: Any) -> bool:
    if isinstance(value, list):
        return any(contains_reference(item) for item in value)
    if isinstance(value, dict):
        return bool({"fromObservation", "fromPromotion"}.intersection(value)) or any(
            contains_reference(item) for item in value.values()
        )
    return False


def reference_paths(value: Any, reference_type: str) -> set[str]:
    """Collect validated unit-reference paths of one type from a specification."""
    paths: set[str] = set()
    if isinstance(value, list):
        for item in value:
            paths.update(reference_paths(item, reference_type))
    elif isinstance(value, dict):
        if reference_type in value:
            reference = value[reference_type]
            if not isinstance(reference, str) or not re.fullmatch(r"units/[a-z0-9-]+\.(?:json|ya?ml)", reference):
                raise OperationError(f"invalid {reference_type} path: {reference!r}")
            paths.add(reference)
        for item in value.values():
            paths.update(reference_paths(item, reference_type))
    return paths


def convergence_scope(
    specifications: dict[str, dict[str, Any]],
    targets: list[str] | None,
    max_depth: int | None = None,
) -> tuple[list[str], list[str]]:
    """Return selected targets and their transitive observation dependencies."""
    if max_depth is not None and max_depth < 0:
        raise OperationError("--depth must be zero or a positive integer")
    selected = sorted(set(targets or specifications))
    unknown = sorted(set(selected) - specifications.keys())
    if unknown:
        available = ", ".join(sorted(specifications))
        raise OperationError(f"unknown unit(s) {', '.join(unknown)}; available units: {available}")
    depths = {unit_name: 0 for unit_name in selected}
    pending = list(selected)
    while pending:
        unit_name = pending.pop()
        depth = depths[unit_name]
        dependencies = {
            Path(reference).stem for reference in reference_paths(specifications[unit_name], "fromObservation")
        }
        missing = sorted(dependencies - specifications.keys())
        if missing:
            raise OperationError(f"{unit_name} references unknown observation unit(s): {', '.join(missing)}")
        if max_depth is not None and depth >= max_depth:
            continue
        for dependency in sorted(dependencies):
            dependency_depth = depth + 1
            if dependency_depth < depths.get(dependency, dependency_depth + 1):
                depths[dependency] = dependency_depth
                pending.append(dependency)
    return selected, sorted(depths)


def convergence_order(specifications: dict[str, dict[str, Any]], scope: list[str]) -> list[str]:
    """Order prerequisites before consumers while tolerating self/cyclic observations."""
    included = set(scope)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(unit_name: str) -> None:
        if unit_name in visited:
            return
        if unit_name in visiting:
            return
        visiting.add(unit_name)
        dependencies = sorted(
            Path(reference).stem
            for reference in reference_paths(specifications[unit_name], "fromObservation")
            if Path(reference).stem in included
        )
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(unit_name)
        visited.add(unit_name)
        ordered.append(unit_name)

    for unit_name in sorted(scope):
        visit(unit_name)
    return ordered


def observation_dependency_graph(specifications: dict[str, dict[str, Any]], scope: list[str]) -> dict[str, list[str]]:
    included = set(scope)
    return {
        unit_name: sorted(
            Path(reference).stem
            for reference in reference_paths(specifications[unit_name], "fromObservation")
            if Path(reference).stem in included
        )
        for unit_name in sorted(scope)
    }


def log_dependency_graph(graph: dict[str, list[str]]) -> None:
    for unit_name, dependencies in graph.items():
        log_status("DEPEND", f"{unit_name}: {', '.join(dependencies) or 'none'}")


def render_dependency_tree(target: str, graph: dict[str, list[str]]) -> list[str]:
    lines = [target]

    def render(unit_name: str, prefix: str, ancestors: set[str]) -> None:
        dependencies = graph[unit_name]
        for index, dependency in enumerate(dependencies):
            last = index == len(dependencies) - 1
            connector = "└── " if last else "├── "
            cycle = dependency in ancestors
            lines.append(f"{prefix}{connector}{dependency}{' [cycle]' if cycle else ''}")
            if not cycle:
                render(
                    dependency,
                    prefix + ("    " if last else "│   "),
                    ancestors | {dependency},
                )

    render(target, "", {target})
    return lines


def nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for candidate in value for item in nested_strings(candidate)]
    if isinstance(value, dict):
        return [item for candidate in value.values() for item in nested_strings(candidate)]
    return []


def publish_receipt_cas(
    observed_ref: str,
    unit_name: str,
    receipt: dict[str, Any],
    desired_revision: str,
) -> str:
    validate_receipt_document(receipt, f"candidate receipt for {unit_name}")
    for attempt in range(5):
        if attempt:
            log_status("RETRY", f"observation publish attempt {attempt + 1}/5")
        with tempfile.TemporaryDirectory() as temporary_directory:
            observed = Path(temporary_directory) / "observed"
            observed_revision = observed_tree(observed_ref, observed)
            receipt_path = unit_document_path(observed, unit_name)
            existing_receipt = load_receipt(receipt_path, unit_name) if receipt_path.is_file() else None
            if existing_receipt is not None:
                validate_receipt_document(existing_receipt, f"persisted receipt for {unit_name}")
            if existing_receipt is not None and existing_receipt.get("desired", {}).get("unitBlob") == receipt.get(
                "desired", {}
            ).get("unitBlob"):
                if observed_revision is None:
                    raise OperationError(f"{observed_ref} receipt has no revision")
                driver = receipt.get("driver")
                if existing_receipt.get("driver") != driver or not isinstance(driver, str):
                    raise OperationError(f"duplicate {unit_name} receipt changed its reconciliation driver")
                existing_result = semantic_reconciliation_result(driver, existing_receipt)
                candidate_result = semantic_reconciliation_result(driver, receipt)
                if existing_result != candidate_result:
                    raise OperationError(
                        f"duplicate {unit_name} receipt for the same desired unit has a different semantic result"
                    )
                return observed_revision
            write_preferred_document(receipt_path, receipt, REPOSITORY_ROOT)
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
    log_heading(f"Reconcile {args.unit}")
    log_status("START", f"environment {args.environment}")
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
                log_status("DONE", f"{args.unit}: no changes")
                write_reconcile_outputs(False)
                return False
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
        log_status("REFS", f"desired {desired_ref}; observed {observed_ref}")
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
                log_status("DONE", f"{args.unit}: source revision is no longer eligible")
                write_reconcile_outputs(False)
                return False
            desired_revision = advanced
            if changed:
                pre_advanced_revision = advanced
            log_status("PIN", f"reconcile advanced desired state at {short_revision(advanced)}")
        observed_revision = observed_tree(observed_ref, observed)
        if args.plan and candidate_source_root is not None:
            assert source_revision is not None
            current_desired = temporary / "current-desired"
            observed_tree(desired_ref, current_desired)
            build_desired_candidate(
                args.environment,
                candidate_source_root,
                source_revision,
                current_desired,
                observed,
                observed_revision,
                desired,
                dry=True,
            )
            desired_revision = f"dry:{source_revision}"
        elif not pre_advance:
            desired_revision = resolve_ref(desired_ref, args.desired_revision)
        if candidate_source_root is None:
            materialize_revision(desired_revision, desired)
        log_status("DESIRED", f"{desired_ref} at {short_revision(desired_revision)}")
        log_status(
            "OBSERVED",
            f"{observed_ref} at {short_revision(observed_revision)}"
            if observed_revision
            else f"{observed_ref} has no receipts yet",
        )
        unit_path = unit_document_path(desired, args.unit)
        if not unit_path.is_file():
            log_status("WAIT", "desired inputs are not materialized")
            log_status("DONE", f"{args.unit}: no changes")
            write_reconcile_outputs(False)
            return False
        unit = load_unit(unit_path, args.unit)
        driver_name, source = require_unit(unit, args.unit)
        validate_unit_materialization(desired, args.unit, unit)
        log_status("DRIVER", driver_name)
        log_status("SOURCE", f"{short_revision(source['revision'])} ({source['path']})")
        if contains_reference(unit):
            raise OperationError(f"{args.unit} desired state is not fully materialized")
        if not unit_requires_reconciliation(unit):
            log_status("SKIP", "unit is complete after desired-state materialization")
            log_status("DONE", f"{args.unit}: materialized for external delivery")
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
                log_status("KEEP", f"{desired_ref} did not change after observation")
            return ""

        unit_blob = file_blob(unit_path)
        receipt_path = unit_document_path(observed, args.unit)
        previous_receipt = load_receipt(receipt_path, args.unit) if receipt_path.is_file() else None
        if previous_receipt is not None:
            validate_receipt_document(previous_receipt, f"persisted receipt for {args.unit}")
        if receipt_path.is_file():
            assert previous_receipt is not None
            receipt = previous_receipt
            skip_clean_unit = not args.plan or bool(unit.get("artifacts"))
            if (
                not getattr(args, "reapply", False)
                and skip_clean_unit
                and receipt.get("desired", {}).get("unitBlob") == unit_blob
            ):
                log_status("KEEP", "observation already matches desired state")
                if args.plan:
                    advanced_revision = ""
                elif pre_advance:
                    advanced_revision = pre_advanced_revision
                else:
                    advanced_revision = advance_if_requested()
                log_status("DONE", f"{args.unit}: clean")
                write_reconcile_outputs(False, advanced_revision)
                return False

        log_status("RUN", f"execute {driver_name} {'planning' if args.plan else 'reconciliation'}")
        source_root = temporary / "source"
        materialize_revision(source["revision"], source_root)
        execution: dict[str, Any] = {
            "environment": args.environment,
            "desired_root": desired,
            "desired_revision": desired_revision,
            "source_root": source_root,
            "source_revision": source["revision"],
            "source_path": source["path"],
            "unit": unit,
            "inputs": unit.get("inputs", {}),
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
            log_status("DONE", f"{args.unit}: no remote changes")
            write_reconcile_outputs(False)
            return False
        try:
            plugin = RECONCILIATION_DRIVERS[driver_name]
        except KeyError as exc:
            raise OperationError(f"{args.unit} uses {driver_name}, which does not support reconciliation") from exc
        result = plugin.reconcile(
            ReconciliationContext(
                **execution,
                previous_receipt=previous_receipt,
            )
        )
        validate_document(UNIT_DRIVERS[driver_name].result_contract, result, f"{driver_name} reconciliation result")
        reserved = {
            "schema",
            "unit",
            "driver",
            "desired",
            "controller",
        }
        overlap = reserved.intersection(result)
        if overlap:
            raise OperationError(f"driver returned reserved observation fields: {sorted(overlap)}")
        receipt = with_schema(
            {
                "schema": 1,
                "unit": args.unit,
                "driver": driver_name,
                "desired": {"revision": desired_revision, "unitBlob": unit_blob},
                "resolvedInputs": unit.get("resolvedInputs", {}),
                "controller": controller_evidence(),
                **result,
            },
            str(driver_schema(driver_name, "receipt")["$id"]),
        )
        validate_receipt_document(receipt, f"{driver_name} receipt")
        revision = publish_receipt_cas(
            observed_ref,
            args.unit,
            receipt,
            desired_revision,
        )
        log_status(
            "OBSERVE",
            f"receipt published to {observed_ref} at {short_revision(revision)}",
        )
        advanced_revision = advance_if_requested() or pre_advanced_revision
        write_reconcile_outputs(True, advanced_revision)
        log_status("DONE", f"{args.unit}: reconciled successfully")
        return True


def log_ref_advance(advance: RefAdvance) -> None:
    attribution = f" after {advance.unit}" if advance.unit else ""
    log_status(
        "ADVANCE",
        f"{advance.ref} {short_revision(advance.before)} -> {short_revision(advance.after)}{attribution}",
    )


def require_reconciliation_approval(unit_name: str) -> None:
    print(
        f"    {'APPROVE':<8} Continue with {unit_name}? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    if answer not in {"y", "yes"}:
        raise OperationError(f"reconciliation of {unit_name} was not approved")


def log_compact_convergence_summary(
    environment: str,
    scope: list[str],
    steps: list[str],
    advances: list[RefAdvance],
    result: str,
    unselected: list[tuple[str, str, str]] | None = None,
) -> None:
    log_heading(f"Convergence result for {environment}")
    if result == "CLEAN":
        driver_summary = f"drivers ran for {', '.join(steps)}" if steps else "no drivers ran"
        ref_summary = f"{len(advances)} ref movement{'s' if len(advances) != 1 else ''}"
        log_status("RESULT", f"CLEAN: {len(scope)}/{len(scope)} units; {driver_summary}; {ref_summary}")
    else:
        log_status("RESULT", result)
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{unit_name}: {status.lower()}; {reason}")


def log_convergence_summary(
    environment: str,
    targets: list[str],
    scope: list[str],
    steps: list[str],
    advances: list[RefAdvance],
    start_heads: tuple[str | None, str | None],
    end_heads: tuple[str | None, str | None],
    result: str,
    unselected: list[tuple[str, str, str]] | None = None,
) -> None:
    log_heading(f"Convergence summary for {environment}")
    log_status("TARGET", ", ".join(targets))
    log_status("SCOPE", ", ".join(scope))
    log_status("STEPS", ", ".join(steps) if steps else "no reconciliation drivers ran")
    if advances:
        for index, advance in enumerate(advances, 1):
            attribution = f" ({advance.unit})" if advance.unit else ""
            log_status(
                "MOVE",
                f"{index}. {advance.kind} {advance.ref} "
                f"{short_revision(advance.before)} -> "
                f"{short_revision(advance.after)}{attribution}",
            )
    else:
        log_status("MOVE", "no desired or observed ref advances")
    log_status(
        "DESIRED",
        f"{short_revision(start_heads[0])} -> {short_revision(end_heads[0])}",
    )
    log_status(
        "OBSERVED",
        f"{short_revision(start_heads[1])} -> {short_revision(end_heads[1])}",
    )
    for unit_name, status, reason in unselected or []:
        if status not in {"CLEAN", "MATERIALIZED"}:
            log_status("UNSCOPED", f"{unit_name}: {status.lower()}; {reason}")
    log_status("RESULT", result)


def command_dependencies(args: argparse.Namespace) -> None:
    source_revision = git("rev-parse", f"{args.source_revision}^{{commit}}").stdout.strip()
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_root = Path(temporary_directory) / "source"
        materialize_revision(source_revision, source_root)
        specifications = load_environment_specifications(source_root, args.environment)
        targets, scope = convergence_scope(specifications, args.unit, args.depth)
        graph = observation_dependency_graph(specifications, scope)
        order = convergence_order(specifications, scope)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "environment": args.environment,
                    "sourceRevision": source_revision,
                    "targets": targets,
                    "units": [{"name": unit_name, "dependencies": graph[unit_name]} for unit_name in order],
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
        print("\n".join(render_dependency_tree(target, graph)))


def command_converge(args: argparse.Namespace) -> None:
    if args.max_steps is not None and args.max_steps < 1:
        raise OperationError("--max-steps must be a positive integer")
    source_revision = resolve_advance_source_revision(REPOSITORY_ROOT, args.environment, args.source_revision)
    if args.require_source_ref and source_revision is None:
        raise OperationError("--require-source-ref applies only to source-tracked environments")
    log_heading(f"Converge {args.environment}")
    log_status(
        "SOURCE",
        short_revision(source_revision) if source_revision else "merged promotion",
    )

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
            log_status("PIN", f"reviewed specification {short_revision(effective_source_revision)}")
        else:
            assert source_revision is not None
            effective_source_revision = source_revision
            source_root = probe_source
        specifications = load_environment_specifications(source_root, args.environment)
        targets, scope = convergence_scope(specifications, args.unit)
        order = convergence_order(specifications, scope)
        if args.verbose:
            log_status("REFS", f"desired {desired_ref}; observed {observed_ref}")
            log_status("TARGET", ", ".join(targets))
            log_status("SCOPE", ", ".join(scope))
            log_dependency_graph(observation_dependency_graph(specifications, scope))
        else:
            log_status("DESIRED", f"{desired_ref} at {short_revision(start_desired)}")
            log_status("OBSERVED", f"{observed_ref} at {short_revision(start_observed)}")
            if targets != scope:
                log_status("TARGET", ", ".join(targets))
                log_status("SCOPE", ", ".join(scope))

        advances: list[RefAdvance] = []
        steps: list[str] = []
        last_desired = start_desired
        last_observed = start_observed
        max_steps = args.max_steps or max(2, 2 * len(scope))
        iterations = 0
        previous_plan: list[tuple[str, str, str]] | None = None

        promotion_units = sorted(
            unit_name for unit_name in scope if reference_paths(specifications[unit_name], "fromPromotion")
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
                            f"{desired_ref} {short_revision(before_desired)} -> "
                            f"{short_revision(desired_revision)} (advanced)",
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
                    log_heading(f"Convergence step {len(steps) + 1} (limit {max_steps}): {unit_name}")
                else:
                    log_status("RUN", unit_name)
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
                            f"{observed_ref} {short_revision(before_observed)} -> "
                            f"{short_revision(after_observed)} ({unit_name})",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repository",
        help="Git working tree; defaults to the repository containing the current directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    schemas = commands.add_parser("schemas", help="show or export public JSON Schemas")
    schema_commands = schemas.add_subparsers(dest="schema_command", required=True)
    schemas_show = schema_commands.add_parser("show", help="print one core or driver schema")
    schemas_show.add_argument("driver", help="driver name or 'core'")
    schemas_show.add_argument("kind", help="document kind")
    schemas_show.set_defaults(handler=command_schemas_show)
    schemas_export = schema_commands.add_parser("export", help="write the deterministic schema catalog")
    schemas_export.add_argument("directory")
    schemas_export.add_argument("--check", action="store_true", help="fail if generated files differ")
    schemas_export.set_defaults(handler=command_schemas_export)

    read = commands.add_parser("read-tree", help="materialize a data-only Git ref")
    read.add_argument("--ref", required=True)
    read.add_argument("--revision")
    read.add_argument("--require-ancestor", action="store_true")
    read.add_argument("--allow-missing", action="store_true")
    read.add_argument("--output", required=True)
    read.set_defaults(handler=command_read_tree)

    publish = commands.add_parser("publish-tree", help="commit and push a supplied data tree")
    publish.add_argument("--ref", required=True)
    publish.add_argument("--directory", required=True)
    publish.add_argument("--parent")
    publish.add_argument("--message", required=True)
    publish.set_defaults(handler=command_publish_tree)

    advance = commands.add_parser(
        "advance-desired",
        help="materialize ready units and atomically advance desired state",
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
        help="materialize and publish a reviewed environment promotion candidate",
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
        help="publish a forward-only rollback of desired deployment state",
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
    rollback.add_argument("--dry", action="store_true")
    rollback.set_defaults(handler=command_rollback)

    resolve = commands.add_parser(
        "resolve-desired",
        help="resolve an exact commit from desired-ref history",
    )
    resolve.add_argument("--desired-ref", required=True)
    resolve.add_argument("--desired-revision")
    resolve.set_defaults(handler=command_resolve_desired)

    status = commands.add_parser(
        "status",
        help="show which deployment units are clean, ready, or waiting",
    )
    status.add_argument("--environment", required=True)
    status.add_argument("--desired-ref", help="override the environment's desired ref")
    status.add_argument(
        "--desired-revision",
        help="exact desired commit; defaults to the current desired ref head",
    )
    status.add_argument("--observed-ref", help="override the environment's observed ref")
    status.add_argument("--verbose", action="store_true")
    status.set_defaults(handler=command_status)

    facts = commands.add_parser(
        "facts",
        help="show the facts recorded by deployment unit observations",
    )
    facts.add_argument("--environment", required=True)
    facts.add_argument("--unit", help="show facts for one deployment unit")
    facts.add_argument("--observed-ref", help="override the environment's observed ref")
    facts.add_argument("--json", action="store_true", help="emit one machine-readable document")
    facts.set_defaults(handler=command_facts)

    verify = commands.add_parser(
        "verify",
        help="check current desired units for external drift without changing state",
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
        help="reconcile one deployment unit against desired and observed Git refs",
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
        help="show a unit's transitive observation dependencies",
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
        help="reconcile selected units and their dependencies until clean",
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
