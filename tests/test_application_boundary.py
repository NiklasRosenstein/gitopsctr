from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "gitopsctr"
ISSUANCE_NAMES = {
    "_issue_accepted_desired_snapshot",
    "_issue_effect_authorization",
    "_issue_sealed_candidate",
    "_issue_publication_proof",
    "_issue_review_acceptance_observation",
    "_issue_root_identity",
    "_issue_retained_source",
    "_issue_historical_retained_source_evidence",
    "_issue_authored_document",
    "_new_publication_proof_issuer",
    "_open_publication_proof_issuer",
}
MARKER_NAMES = {
    "_ACCEPTED_DESIRED_ISSUANCE",
    "_EFFECT_AUTHORIZATION_ISSUANCE",
    "_RETAINED_SOURCE_ISSUANCE",
    "_SEALED_CANDIDATE_ISSUANCE",
    "_PUBLICATION_PROOF_ISSUANCE",
    "_PUBLICATION_PROOF_ISSUERS",
}
TRUSTED_ISSUERS = {
    Path("adapters/authority.py"),
    Path("adapters/effect_fencing.py"),
    Path("adapters/git/apply.py"),
    Path("adapters/git/publication.py"),
    Path("adapters/git/remote_authority.py"),
    Path("adapters/memory/authority.py"),
    Path("adapters/memory/effect_fencing.py"),
    Path("adapters/git/sources.py"),
    Path("adapters/memory/snapshots.py"),
    Path("adapters/memory/sources.py"),
}


def test_application_issuance_factories_stay_inside_trusted_adapters() -> None:
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
    assert not violations, "application issuance factories accessed outside trusted adapters: " + "; ".join(violations)


def test_application_issuance_markers_are_never_imported() -> None:
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
        assert not accessed, f"{path.relative_to(SOURCE)} accesses an application issuance marker"


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


def test_git_read_adapters_do_not_reach_legacy_inventory_or_plane_materialization() -> None:
    """Phase 3b keeps the adapter facade clear of the retired path session."""

    forbidden = ("gitopsctr.inventory", "gitopsctr.inspection", "gitopsctr.plane_repositories")
    for relative_name in ("adapters/git/inspection.py", "adapters/git/dependencies.py"):
        path = SOURCE / relative_name
        tree = ast.parse(path.read_text(), filename=str(path))
        targets = {
            target
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for target in _application_import_targets(path, node)
        }
        assert not [
            target for target in targets if any(target == item or target.startswith(f"{item}.") for item in forbidden)
        ]


def test_workspace_get_helper_chain_has_no_legacy_inventory_or_plane_import() -> None:
    """The Git facade cannot hide legacy discovery one helper deeper."""

    modules = (
        "adapters/git/workspace_inspection.py",
        "adapters/git/status.py",
        "workspace_get.py",
        "workspace_status.py",
        "workspace_dependencies.py",
        "workspace_inventory.py",
        "workspace_collections.py",
        "workspace_inspection.py",
    )
    forbidden = (
        "gitopsctr.inventory",
        "gitopsctr.inspection",
        "gitopsctr.plane_repositories",
        "gitopsctr.adapters.git.plane_materialization",
    )
    violations: list[str] = []
    for relative_name in modules:
        path = SOURCE / relative_name
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _application_import_targets(path, node):
                if any(target == item or target.startswith(f"{item}.") for item in forbidden):
                    violations.append(f"{relative_name} imports {target}")
    assert not violations, "workspace get helper chain reaches a legacy backend: " + "; ".join(violations)


def _logical_workspace_path_imports(tree: ast.AST) -> set[str]:
    """Find filesystem-path imports while permitting logical POSIX value types."""

    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.update(alias.name for alias in node.names if alias.name == "pathlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "pathlib":
            violations.update(alias.name for alias in node.names if alias.name != "PurePosixPath")
    return violations


def test_logical_workspace_modules_cannot_import_filesystem_path_api() -> None:
    modules = (
        "workspace_get.py",
        "workspace_status.py",
        "workspace_dependencies.py",
        "workspace_inventory.py",
        "workspace_collections.py",
        "workspace_inspection.py",
    )
    violations: list[str] = []
    for relative_name in modules:
        tree = ast.parse((SOURCE / relative_name).read_text(), filename=relative_name)
        violations.extend(f"{relative_name} imports {name}" for name in _logical_workspace_path_imports(tree))
    assert not violations, "logical workspace modules import filesystem paths: " + "; ".join(violations)


def test_logical_workspace_path_guard_rejects_pathlib_and_path_imports() -> None:
    pathlib_import = ast.parse("import pathlib")
    path_import = ast.parse("from pathlib import Path")
    wildcard_import = ast.parse("from pathlib import *")
    pure_path_import = ast.parse("from pathlib import PurePath")
    posix_path_import = ast.parse("from pathlib import PosixPath")
    windows_path_import = ast.parse("from pathlib import WindowsPath")
    pure_posix_import = ast.parse("from pathlib import PurePosixPath")

    assert _logical_workspace_path_imports(pathlib_import) == {"pathlib"}
    assert _logical_workspace_path_imports(path_import) == {"Path"}
    assert _logical_workspace_path_imports(wildcard_import) == {"*"}
    assert _logical_workspace_path_imports(pure_path_import) == {"PurePath"}
    assert _logical_workspace_path_imports(posix_path_import) == {"PosixPath"}
    assert _logical_workspace_path_imports(windows_path_import) == {"WindowsPath"}
    assert not _logical_workspace_path_imports(pure_posix_import)
