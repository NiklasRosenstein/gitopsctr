from __future__ import annotations

import ast
import sys
from pathlib import Path

RESOURCE_API = Path(__file__).parents[1] / "src" / "gitopsctr" / "resource_api"


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return tuple(modules)


def test_resource_api_uses_only_its_own_modules_and_library_neutral_standard_library():
    forbidden_standard_library = {"pathlib"}
    for path in sorted(RESOURCE_API.glob("*.py")):
        for module in _imports(path):
            if module.startswith("gitopsctr.resource_api"):
                continue
            root = module.partition(".")[0]
            assert root in sys.stdlib_module_names, f"{path.name} imports non-standard dependency {module}"
            assert root not in forbidden_standard_library, f"{path.name} imports forbidden dependency {module}"


def test_legacy_api_module_is_removed_after_the_preproduction_cutover():
    assert not (RESOURCE_API.parent / "api.py").exists()
