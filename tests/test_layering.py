"""Guard the core modules against importing the Playwright adapter."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "linkedin_auto_connect"

CORE_MODULES = (
    "models.py",
    "ports.py",
    "config.py",
    "runner.py",
    "app.py",
    "matcher.py",
    "jev_cache.py",
    "limits.py",
    "cookies.py",
)


def _missing_modules() -> list[str]:
    return [name for name in CORE_MODULES if not (PACKAGE / name).is_file()]


def _playwright_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "playwright" or name.startswith("playwright."):
                    violations.append(f"line {node.lineno}: import {name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "playwright" or module.startswith("playwright."):
                violations.append(f"line {node.lineno}: from {module} import ...")
    return violations


def test_all_core_modules_exist() -> None:
    """Every module owned by a parallel lane is present for the final suite."""
    missing = _missing_modules()
    assert not missing, (
        "core modules from parallel lanes are missing (do not skip this check): "
        + ", ".join(missing)
    )


def test_core_modules_do_not_import_playwright() -> None:
    """Core stays adapter-free even while the browser adapter uses Playwright."""
    missing = _missing_modules()
    assert not missing, (
        "core modules from parallel lanes are missing (do not skip this check): "
        + ", ".join(missing)
    )

    violations: dict[str, list[str]] = {}
    for name in CORE_MODULES:
        found = _playwright_imports(PACKAGE / name)
        if found:
            violations[name] = found

    assert not violations, f"core modules must stay adapter-free: {violations}"
