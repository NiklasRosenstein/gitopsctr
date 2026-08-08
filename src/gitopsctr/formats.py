"""Read GitOpsCTR's Project resource and YAML or JSON documents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

PROJECT_RESOURCE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Project.schema.json",
    "title": "Project (gitopsctr.io/v1)",
    "type": "object",
    "properties": {
        "$schema": {"type": "string"},
        "apiVersion": {"const": "gitopsctr.io/v1"},
        "kind": {"const": "Project"},
        "metadata": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 253,
                    "pattern": r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "spec": {
            "type": "object",
            "properties": {
                "writeFormat": {"enum": ["yaml", "json"]},
                "environmentsPath": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$",
                    "description": "Repository-relative directory containing authored environments.",
                },
            },
            "additionalProperties": False,
        },
    },
    "required": ["apiVersion", "kind", "metadata", "spec"],
    "additionalProperties": False,
}

PROJECT_CONFIG_NAMES = ("gitopsctr.yaml", "gitopsctr.yml", ".gitopsctr.yaml", ".gitopsctr.yml")


class DocumentFormat(StrEnum):
    YAML = "yaml"
    JSON = "json"

    @property
    def suffix(self) -> str:
        return ".yaml" if self is DocumentFormat.YAML else ".json"


class DocumentFormatError(ValueError):
    """A document could not be decoded or the project format is invalid."""


@dataclass(frozen=True)
class Project:
    name: str
    write_format: DocumentFormat = DocumentFormat.YAML
    environments_path: PurePosixPath = PurePosixPath("deployment/environments")


def project_config_path(root: Path) -> Path:
    paths = [root / name for name in PROJECT_CONFIG_NAMES if (root / name).is_file()]
    if not paths:
        raise DocumentFormatError(f"source tree has no Project configuration: {root / 'gitopsctr.yaml'}")
    if len(paths) > 1:
        raise DocumentFormatError("multiple Project configuration files exist: " + ", ".join(map(str, paths)))
    return paths[0]


def _validate_project_name(name: str, path: Path) -> None:
    labels = name.split(".")
    if any(len(label) > 63 for label in labels):
        raise DocumentFormatError(f"invalid project config {path}: metadata.name must be a DNS-1123 subdomain")


def validate_project_document(value: object, path: Path) -> Project:
    """Validate a Project resource and return its runtime configuration."""

    if not isinstance(value, dict):
        raise DocumentFormatError(f"project config {path} must be a mapping")
    try:
        Draft202012Validator(PROJECT_RESOURCE_SCHEMA).validate(value)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        detail = f"{location}: {exc.message}" if location else exc.message
        raise DocumentFormatError(f"invalid project config {path}: {detail}") from exc
    metadata = cast(dict[str, Any], value["metadata"])
    specification = cast(dict[str, Any], value["spec"])
    project_name = cast(str, metadata["name"])
    _validate_project_name(project_name, path)
    selected = specification.get("writeFormat", "yaml")
    environments_path = PurePosixPath(cast(str, specification.get("environmentsPath", "deployment/environments")))
    if environments_path.is_absolute() or ".." in environments_path.parts:
        raise DocumentFormatError(f"invalid project config {path}: environmentsPath must stay inside the source tree")
    return Project(
        name=project_name,
        write_format=DocumentFormat.JSON if selected == "json" else DocumentFormat.YAML,
        environments_path=environments_path,
    )


def load_project_config(root: Path) -> Project:
    path = project_config_path(root)
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DocumentFormatError(f"could not read project config {path}: {exc}") from exc
    return validate_project_document(value, path)


def project_environment_root(root: Path, environment_name: str) -> Path:
    """Return the configured directory for an authored environment."""

    return root.joinpath(*load_project_config(root).environments_path.parts, environment_name)


def _ensure_json_value(value: object, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DocumentFormatError(f"expected a mapping in {path}")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise DocumentFormatError(f"document {path} contains a non-JSON value: {exc}") from exc
    return cast(dict[str, Any], value)


def load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise DocumentFormatError(f"could not read {path}: {exc}") from exc
    try:
        value = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise DocumentFormatError(f"could not parse {path}: {exc}") from exc
    return _ensure_json_value(value, path)


def document_path(directory: Path, stem: str, root: Path, *, prefer_existing: bool = True) -> Path:
    """Return the canonical path for a logical document.

    Existing files win so changing the project preference does not silently
    create duplicate logical documents.  New files use the configured format.
    """

    candidates = (directory / f"{stem}.yaml", directory / f"{stem}.yml", directory / f"{stem}.json")
    if prefer_existing:
        existing = [path for path in candidates if path.is_file()]
        if len(existing) > 1:
            raise DocumentFormatError("multiple representations exist for " + stem + ": " + ", ".join(map(str, existing)))
        if existing:
            return existing[0]
    return directory / f"{stem}{load_project_config(root).write_format.suffix}"


def document_candidates(directory: Path, stem: str) -> tuple[Path, ...]:
    return tuple(path for path in (directory / f"{stem}.yaml", directory / f"{stem}.yml", directory / f"{stem}.json") if path.is_file())


def write_document(path: Path, value: dict[str, Any], *, format: DocumentFormat | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = format or (DocumentFormat.JSON if path.suffix.lower() == ".json" else DocumentFormat.YAML)
    output = (
        path if selected is DocumentFormat.YAML and path.suffix.lower() == ".yml" else path.with_suffix(selected.suffix)
    )
    if selected is DocumentFormat.JSON:
        text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    else:
        yaml_value = dict(value)
        schema_hint = yaml_value.get("$schema")
        if isinstance(schema_hint, str):
            yaml_value.pop("$schema")
        text = yaml.safe_dump(yaml_value, sort_keys=False, default_flow_style=False, allow_unicode=False)
        if isinstance(schema_hint, str):
            text = f"# yaml-language-server: $schema={schema_hint}\n{text}"
    output.write_text(text)
    return output
