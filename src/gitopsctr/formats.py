"""Read and write GitOpsCTR documents in YAML or JSON.

YAML is the default authoring and write format.  A project may pin the write
format in ``gitopsctr.yaml`` (or ``.gitopsctr.yaml``) with ``writeFormat`` set
to ``yaml`` or ``json``.  Readers always accept both formats.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

PROJECT_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/ProjectConfig.schema.json",
    "title": "GitOpsCTR project configuration",
    "type": "object",
    "properties": {
        "$schema": {"type": "string"},
        "writeFormat": {"enum": ["yaml", "json"]},
    },
    "additionalProperties": False,
}


class DocumentFormat(StrEnum):
    YAML = "yaml"
    JSON = "json"

    @property
    def suffix(self) -> str:
        return ".yaml" if self is DocumentFormat.YAML else ".json"


class DocumentFormatError(ValueError):
    """A document could not be decoded or the project format is invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    write_format: DocumentFormat = DocumentFormat.YAML


def _config_path(root: Path) -> Path | None:
    for name in ("gitopsctr.yaml", "gitopsctr.yml", ".gitopsctr.yaml", ".gitopsctr.yml"):
        path = root / name
        if path.is_file():
            return path
    return None


def load_project_config(root: Path) -> ProjectConfig:
    path = _config_path(root)
    if path is None:
        return ProjectConfig()
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DocumentFormatError(f"could not read project config {path}: {exc}") from exc
    if value is None:
        return ProjectConfig()
    if not isinstance(value, dict):
        raise DocumentFormatError(f"project config {path} must be a mapping")
    try:
        Draft202012Validator(PROJECT_CONFIG_SCHEMA).validate(value)
    except ValidationError as exc:
        detail = exc.message
        raise DocumentFormatError(f"invalid project config {path}: {detail}") from exc
    selected = value.get("writeFormat", "yaml")
    return ProjectConfig(DocumentFormat.JSON if selected == "json" else DocumentFormat.YAML)


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
    output = path.with_suffix(selected.suffix)
    if selected is DocumentFormat.JSON:
        text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    else:
        text = yaml.safe_dump(value, sort_keys=False, default_flow_style=False, allow_unicode=False)
    output.write_text(text)
    return output
