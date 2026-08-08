import json
from pathlib import Path

import pytest

from gitopsctr import cli

FIXTURE_REPOSITORY = Path(__file__).parent / "fixtures/repository"


def write_test_document(path: Path, value: object) -> None:
    """Write legacy-shaped test data, enveloping authored source documents."""

    if not isinstance(value, dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return
    parts = path.parts
    deployment_index = next(
        (index for index in range(len(parts) - 1) if parts[index : index + 2] == ("deployment", "environments")),
        None,
    )
    if deployment_index is not None:
        project_root = Path(*parts[:deployment_index])
        project_path = project_root / "gitopsctr.yaml"
        if not any((project_root / name).is_file() for name in cli.PROJECT_CONFIG_NAMES):
            project_root.mkdir(parents=True, exist_ok=True)
            project_path.write_text(
                json.dumps(
                    {
                        "apiVersion": "gitopsctr.io/v1",
                        "kind": "Project",
                        "metadata": {"name": "test-project"},
                        "spec": {},
                    }
                )
            )
        if path.stem == "environment":
            value = cli.serialize_environment_document(cli.normalize_environment_document(value, path.parent.name))
        elif path.parent.name == "units":
            value = cli.serialize_unit_document(
                cli.normalize_unit_document(value, path.stem),
                profile="authored",
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture(autouse=True)
def repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep controller tests independent from the gitopsctr source checkout."""
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", FIXTURE_REPOSITORY)
