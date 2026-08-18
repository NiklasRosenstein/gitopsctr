from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "gitopsctr"
ISSUANCE_NAMES = {
    "_issue_accepted_desired_snapshot",
    "_issue_effect_authorization",
}
MARKER_NAMES = {"_ACCEPTED_DESIRED_ISSUANCE", "_EFFECT_AUTHORIZATION_ISSUANCE"}
TRUSTED_ISSUERS = {
    Path("adapters/authority.py"),
    Path("adapters/effect_fencing.py"),
    Path("adapters/memory/authority.py"),
    Path("adapters/memory/effect_fencing.py"),
}


def test_authority_issuance_factories_stay_inside_trusted_adapters() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE)
        if relative == Path("application/model.py") or relative in TRUSTED_ISSUERS:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                accessed = ISSUANCE_NAMES & {alias.name for alias in node.names}
            elif isinstance(node, ast.Attribute):
                accessed = ISSUANCE_NAMES & {node.attr}
            else:
                continue
            if accessed:
                violations.append(f"{relative}: {', '.join(sorted(accessed))}")
    assert not violations, "authority issuance factories accessed outside trusted adapters: " + "; ".join(violations)


def test_authority_issuance_markers_are_never_imported() -> None:
    for path in sorted(SOURCE.rglob("*.py")):
        if path == SOURCE / "application" / "model.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        accessed: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                accessed.update(MARKER_NAMES & {alias.name for alias in node.names})
            elif isinstance(node, ast.Attribute):
                accessed.update(MARKER_NAMES & {node.attr})
        assert not accessed, f"{path.relative_to(SOURCE)} accesses an authority issuance marker"
