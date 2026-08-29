"""Proves a file's executable code is unchanged against a git ref.

Docstrings and attribute docstrings are stripped from both sides before the ASTs are
compared, so a docs-only rewrite reads as identical while a real code change is caught.

Usage: codesame.py <git-ref> <path>... , exiting non-zero on a code change.
"""

from __future__ import annotations

import ast
import subprocess
import sys


def _is_str_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def strip_docstrings(tree: ast.AST) -> ast.AST:
    # attribute docstrings: a bare string statement directly after an assignment, at
    # module or class level. Sphinx reads them as documentation and they are a runtime
    # no-op, so they must not read as a code change
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        kept: list[ast.stmt] = []
        for i, stmt in enumerate(body):
            if (
                i > 0
                and _is_str_expr(stmt)
                and isinstance(body[i - 1], (ast.Assign, ast.AnnAssign))
            ):
                continue
            kept.append(stmt)
        if kept != body:
            node.body = kept or [ast.Pass()]
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            if len(body) == 1:
                node.body = [ast.Pass()]
            else:
                node.body = body[1:]
    return tree


def dump(src: str) -> str:
    return ast.dump(strip_docstrings(ast.parse(src)), annotate_fields=True)


if __name__ == "__main__":
    ref = sys.argv[1]
    bad = []
    for path in sys.argv[2:]:
        old = subprocess.run(
            ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
        ).stdout
        new = open(path).read()
        if dump(old) != dump(new):
            bad.append(path)
    if bad:
        print(f"CODE CHANGED in {len(bad)} file(s):")
        for b in bad:
            print("  ", b)
        sys.exit(1)
    print(f"code identical to {ref} in all {len(sys.argv) - 2} file(s) (docs-only)")
