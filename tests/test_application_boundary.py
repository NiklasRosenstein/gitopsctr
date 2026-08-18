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


_APPLICATION_FORBIDDEN_IMPORTS = (
    "pathlib",
    "gitopsctr.adapters",
    "gitopsctr.controller",
    "gitopsctr.git_local",
    "gitopsctr.inspection",
    "gitopsctr.inventory",
    "gitopsctr.plane_repositories",
    "gitopsctr.registry",
    "gitopsctr.state",
)


def _application_import_targets(path: Path, node: ast.Import | ast.ImportFrom) -> set[str]:
    """Resolve absolute and relative import targets for one application module."""

    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}

    if not node.level:
        base = node.module.split(".") if node.module else []
    else:
        relative = path.relative_to(SOURCE).with_suffix("")
        package = ["gitopsctr", *relative.parts[:-1]]
        package = package[: len(package) - node.level + 1]
        base = [*package, *(node.module.split(".") if node.module else ())]
    targets = {".".join(base)} if base else set()
    targets.update(".".join((*base, alias.name)) for alias in node.names if alias.name != "*")
    return targets


def _is_forbidden_application_import(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(f"{forbidden}.") for forbidden in _APPLICATION_FORBIDDEN_IMPORTS
    )


def test_application_orchestration_has_no_legacy_path_inventory_or_git_dependency() -> None:
    """Phase 3a keeps backend discovery outside the typed application slice."""

    violations: list[str] = []
    for path in sorted((SOURCE / "application").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = _application_import_targets(path, node)
            elif isinstance(node, ast.ImportFrom):
                imported = _application_import_targets(path, node)
            else:
                continue
            prohibited = sorted(module for module in imported if _is_forbidden_application_import(module))
            if prohibited:
                violations.append(f"{path.relative_to(SOURCE)} imports {', '.join(prohibited)}")
    assert not violations, "application orchestration imports a legacy backend: " + "; ".join(violations)


def test_application_import_boundary_resolves_descendants_and_relative_imports() -> None:
    """The boundary detector cannot be bypassed with an alias or relative form."""

    path = SOURCE / "application" / "boundary_probe.py"
    direct = ast.parse("import gitopsctr.adapters.git.snapshots as snapshots").body[0]
    descendant = ast.parse("from gitopsctr.inspection import build_resource_inspection").body[0]
    parent_alias = ast.parse("from gitopsctr import inventory").body[0]
    relative = ast.parse("from .. import inventory").body[0]

    assert isinstance(direct, ast.Import)
    assert isinstance(descendant, ast.ImportFrom)
    assert isinstance(parent_alias, ast.ImportFrom)
    assert isinstance(relative, ast.ImportFrom)
    assert any(_is_forbidden_application_import(target) for target in _application_import_targets(path, direct))
    assert any(_is_forbidden_application_import(target) for target in _application_import_targets(path, descendant))
    assert any(_is_forbidden_application_import(target) for target in _application_import_targets(path, parent_alias))
    assert any(_is_forbidden_application_import(target) for target in _application_import_targets(path, relative))
