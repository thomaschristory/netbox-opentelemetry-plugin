"""All opentelemetry.* imports in package code must live in otel.py.

This is enforced statically (via ast) rather than at runtime so it catches imports that are
never exercised by the rest of the test suite (for example imports only reached on an error
path).
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "netbox_opentelemetry_plugin"
EXEMPT_MODULE = "otel.py"


def _is_type_checking(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


def _walk_non_type_checking(tree: ast.AST):
    """Yield all nodes in tree, skipping the bodies of `if TYPE_CHECKING:` blocks."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            # Still descend into orelse (an `else:` branch is not exempt).
            for child in node.orelse:
                yield from _walk_non_type_checking(child)
            continue
        yield node
        yield from _walk_non_type_checking(node)


def _opentelemetry_imports(tree: ast.Module) -> list[str]:
    offenders = []
    for node in _walk_non_type_checking(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "opentelemetry" or alias.name.startswith("opentelemetry."):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "opentelemetry" or module.startswith("opentelemetry."):
                offenders.append(f"line {node.lineno}: from {module} import ...")
    return offenders


def _python_files():
    return sorted(p for p in PACKAGE_ROOT.rglob("*.py") if p.name != EXEMPT_MODULE)


def test_no_module_imports_opentelemetry_directly_outside_otel():
    failures: dict[str, list[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders = _opentelemetry_imports(tree)
        if offenders:
            failures[str(path.relative_to(PACKAGE_ROOT))] = offenders
    assert not failures, f"opentelemetry imports found outside otel.py: {failures}"


def test_python_files_were_actually_checked():
    # Guard against the glob silently matching nothing (e.g. a bad PACKAGE_ROOT).
    assert len(_python_files()) > 3
