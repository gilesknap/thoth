"""Replaces one docstring with the text on stdin, addressing it by AST position.

The docstring is found by walking the tree to <qualname> rather than by matching its
text, so a rewrite cannot land on the wrong copy of a repeated line. Indentation and the
triple quotes are added here, so stdin carries the prose alone.

Usage: setdoc.py <path> <qualname> < body , with '<module>' for the module docstring.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def find(tree: ast.AST, want: str):
    if want == "<module>":
        return tree

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                if name == want:
                    return child
                if (r := walk(child, name)) is not None:
                    return r
        return None

    return walk(tree, "")


path, qual = sys.argv[1], sys.argv[2]
new = sys.stdin.read().strip("\n")
src = Path(path).read_text()
lines = src.split("\n")
node = find(ast.parse(src), qual)
if node is None:
    sys.exit(f"no such node: {qual} in {path}")
first = node.body[0]
if not (
    isinstance(first, ast.Expr)
    and isinstance(first.value, ast.Constant)
    and isinstance(first.value.value, str)
):
    sys.exit(f"{qual} in {path} has no docstring")
s, e = first.lineno - 1, first.end_lineno - 1
opener = lines[s]
indent = " " * (len(opener) - len(opener.lstrip()))
nl = new.split("\n")
out = [f'{indent}"""{nl[0]}']
for x in nl[1:]:
    out.append(f"{indent}{x}" if x.strip() else "")
if len(nl) == 1:
    out[0] += '"""'
else:
    out.append(f'{indent}"""')
lines[s : e + 1] = out
Path(path).write_text("\n".join(lines))
