"""Guard the package's layer boundaries.

The dependency rule is: ``adapters`` -> ``application`` -> ``domain``. The
domain and application layers must not import an adapter, Playwright, or an
HTTP client, and the domain must not depend on the root configuration. The test
scans the source so the rule cannot rot silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "linkedin_auto_connect"

EXPECTED_MODULES = (
    "__init__.py",
    "__main__.py",
    "cli.py",
    "config.py",
    "domain/__init__.py",
    "domain/models.py",
    "domain/ports.py",
    "domain/matcher.py",
    "domain/limits.py",
    "application/__init__.py",
    "application/app.py",
    "application/runner.py",
    "adapters/__init__.py",
    "adapters/browser.py",
    "adapters/cookies.py",
    "adapters/jev.py",
    "adapters/jev_cache.py",
)

IO_MODULES = frozenset(
    {"playwright", "urllib", "http", "httpx", "requests", "socket"}
)
DOMAIN_FORBIDDEN = frozenset(
    {"application", "adapters", "config", *IO_MODULES}
)
APPLICATION_FORBIDDEN = frozenset({"adapters", *IO_MODULES})
CONFIG_FORBIDDEN = frozenset({"application", "domain", "adapters", *IO_MODULES})


def _core_files() -> list[Path]:
    """Every file whose imports must obey the inward dependency rule."""
    return [
        *sorted((PACKAGE / "domain").glob("*.py")),
        *sorted((PACKAGE / "application").glob("*.py")),
        PACKAGE / "config.py",
    ]


def _imports(path: Path) -> list[tuple[int, str]]:
    """Return ``(line, target)`` for every import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            found.append((node.lineno, module))
    return found


def _top_target(imported: str) -> str:
    """Reduce an import target to its package name, absolute or relative."""
    target = imported.lstrip(".")
    parts = target.split(".")
    if len(parts) >= 2 and parts[0] == "linkedin_auto_connect":
        return parts[1]
    return parts[0]


def _forbidden_for(path: Path) -> frozenset[str]:
    """Return the import roots *path* is not allowed to use."""
    if path.parent.name == "domain":
        return DOMAIN_FORBIDDEN
    if path.parent.name == "application":
        return APPLICATION_FORBIDDEN
    return CONFIG_FORBIDDEN


def test_expected_layered_modules_exist() -> None:
    """Every module in the documented layout is present."""
    missing = [name for name in EXPECTED_MODULES if not (PACKAGE / name).is_file()]
    assert not missing, "missing layered modules: " + ", ".join(missing)


def test_core_layers_do_not_import_adapters_or_io_libraries() -> None:
    """Domain/application/config stay free of adapters, Playwright and HTTP."""
    violations: list[str] = []
    for path in _core_files():
        forbidden = _forbidden_for(path)
        for line, imported in _imports(path):
            top = _top_target(imported)
            if top in forbidden:
                relative = path.relative_to(PACKAGE)
                violations.append(f"{relative}:{line}: {imported}")
    assert not violations, (
        "layer boundary broken (adapters must only be imported by cli.py): "
        + ", ".join(violations)
    )


def test_adapters_package_does_not_import_playwright_eagerly() -> None:
    """Importing the adapters package must not launch a Playwright import."""
    init = PACKAGE / "adapters" / "__init__.py"
    assert not _imports(init), "adapters/__init__.py must stay import-free"
