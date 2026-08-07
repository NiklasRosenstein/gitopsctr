from pathlib import Path

import pytest

from gitopsctr import cli

FIXTURE_REPOSITORY = Path(__file__).parent / "fixtures/repository"


@pytest.fixture(autouse=True)
def repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep controller tests independent from the gitopsctr source checkout."""
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", FIXTURE_REPOSITORY)
