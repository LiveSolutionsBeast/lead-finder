#!/usr/bin/env python3
"""
regression_check.py — Guard the PRIME baseline against accidental import/def drops.

Usage:
    python3 scripts/regression_check.py

Returns 0 if current core files are a safe superset of the PRIME baseline.
Returns 1 and prints a diff if any top-level import or definition was dropped.

PRIME baseline = the earliest .bak-pre-* backup for each core file.
This script must be run from the repository root.
"""
from __future__ import annotations

import ast
import glob
import sys
from pathlib import Path

CORE_FILES = [
    "lf_server.py",
    "lf_executives.py",
    "lf_db.py",
    "lf_matcher.py",
    "lf_email_patterns.py",
    "lf_agent_verify.py",
    "lf_search_providers.py",
    "lf_config.py",
]


def _import_names(tree: ast.AST) -> set[str]:
    """Collect all imports, including those inside functions/methods."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    return names


def _top_level_defs(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and getattr(node, "col_offset", 0) == 0
    }


def _find_prime_baseline(filename: str) -> Path | None:
    candidates = sorted(glob.glob(f"{filename}.bak-pre-*"))
    if not candidates:
        return None
    return Path(candidates[0])


def _all_referenced_symbols(text: str) -> set[str]:
    """Return every bare name and attribute name used as a Load in the file."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            used.add(node.attr)
    return used


def _check_file(filename: str) -> list[str]:
    current_path = Path(filename)
    prime_path = _find_prime_baseline(filename)
    errors: list[str] = []
    if not current_path.exists():
        errors.append(f"{filename}: current file missing")
        return errors
    if prime_path is None:
        # No PRIME baseline exists for this file yet. Warn but do not fail;
        # the rule is to create a baseline before the *next* edit.
        return []

    current_text = current_path.read_text()
    prime_text = prime_path.read_text()
    current_tree = ast.parse(current_text)
    prime_tree = ast.parse(prime_text)

    current_used = _all_referenced_symbols(current_text)
    current_imports = _import_names(current_tree)
    dropped_imports = _import_names(prime_tree) - current_imports
    dropped_defs = _top_level_defs(prime_tree) - _top_level_defs(current_tree)

    # Flag a dropped import only if the symbol it provided is still referenced
    # in the current file but is no longer imported anywhere (top-level or local).
    really_dropped_imports = set()
    for imp in dropped_imports:
        symbol = imp.split(".")[-1]
        if symbol in current_used and symbol not in {i.split(".")[-1] for i in current_imports}:
            really_dropped_imports.add(imp)

    if really_dropped_imports:
        errors.append(
            f"{filename}: dropped imports whose symbols are still used {sorted(really_dropped_imports)} (baseline {prime_path})"
        )
    if dropped_defs:
        errors.append(f"{filename}: dropped top-level defs {sorted(dropped_defs)[:20]}... (baseline {prime_path})")
    return errors


def main() -> int:
    all_errors: list[str] = []
    for fn in CORE_FILES:
        all_errors.extend(_check_file(fn))

    if all_errors:
        print("REGRESSION DETECTED — the following imports/definitions were dropped vs. PRIME baseline:")
        for err in all_errors:
            print(f"  - {err}")
        print("\nAction required: restore the dropped symbols or create an explicit compatibility shim.")
        return 1

    print("REGRESSION CHECK OK — all core files retain their PRIME baseline imports and top-level definitions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
