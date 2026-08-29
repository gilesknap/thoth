"""Normalises rewritten docstrings to the code register's two mechanical rules.

Sections are matched to the git ref's own Google-style layout, so a helper the ref left
bare stays bare and one it documented keeps its block, and a function the ref gave no
sections at all stays a bare one-liner. Prose is collapsed to a single block.

A docstring decorated with .tool is never touched, because a model reads it at runtime.

Usage: degoogle.py <path>... , comparing against main.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import textwrap
from pathlib import Path

WIDTH = 88
SECT = re.compile(r"^(Args|Returns|Yields|Raises|Attributes):\s*$")


def sections(doc: str) -> set[str]:
    return {m.group(1) for line in doc.split("\n") if (m := SECT.match(line.strip()))}


def qualnames(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                d = ast.get_docstring(child)
                if d:
                    out[name] = d
                walk(child, name)

    d = ast.get_docstring(tree)
    if d:
        out["<module>"] = d
    walk(tree, "")
    return out


def split_doc(doc: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Return (prose lines, [(section name, body lines)])."""
    lines = doc.split("\n")
    prose: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for line in lines:
        m = SECT.match(line.strip())
        if m:
            if cur:
                blocks.append(cur)
            cur = (m.group(1), [])
            continue
        if cur:
            cur[1].append(line)
        else:
            prose.append(line)
    if cur:
        blocks.append(cur)
    return prose, blocks


def rebuild(doc: str, keep: set[str], indent: str) -> str:
    prose, blocks = split_doc(doc)
    text = [p for p in prose if p.strip()]
    if not text:
        return doc
    summary = text[0].strip()
    rest = " ".join(p.strip() for p in text[1:])

    out = [summary]
    if rest:
        wrapped = textwrap.wrap(rest, width=WIDTH - len(indent), break_on_hyphens=False)
        # keep a paragraph break only when the collapsed block is genuinely long
        out.append("")
        out.extend(wrapped)
    for name, body in blocks:
        if name not in keep:
            continue
        trimmed = [b for b in body if b.strip()]
        if not trimmed:
            continue
        out.append("")
        out.append(f"{name}:")
        out.extend(
            b[len(indent) :] if b.startswith(indent) else b.lstrip() for b in trimmed
        )
    return "\n".join(out)


def process(path: str, ref: str = "main") -> int:
    src = Path(path).read_text()
    old = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    ).stdout
    if not old:
        return 0
    base = qualnames(ast.parse(old))
    tree = ast.parse(src)
    lines = src.split("\n")
    edits: list[tuple[int, int, list[str]]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                handle(child, name)
                visit(child, name)

    def handle(node: ast.AST, name: str) -> None:
        body = getattr(node, "body", None)
        if not body:
            return
        # never touch a model-facing tool description: an @server.tool docstring is
        # read by the model at runtime, so it stays byte-identical
        for dec in getattr(node, "decorator_list", []):
            if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "tool":
                return
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            return
        doc = first.value.value
        s, e = first.lineno - 1, first.end_lineno - 1
        opener = lines[s]
        indent = " " * (len(opener) - len(opener.lstrip()))
        joined = "\n".join(lines[s : e + 1]).strip()
        if not (joined.startswith('"""') and joined.endswith('"""')):
            return
        # a function absent from the ref is NEW, so its sections are the author's
        # own choice and are left alone. only prose is collapsed
        if name not in base:
            keep = sections(doc)
        else:
            # keep exactly the sections the ref had. where it had none the function
            # was Google-exempt (short and obvious), so it stays a bare docstring
            # rather than gaining a Raises block it never had
            base_sects = sections(base[name])
            keep = base_sects | ({"Raises"} if base_sects else set())
            keep &= sections(doc)
        new_doc = rebuild(doc, keep, indent)
        if new_doc == doc:
            return
        nl = new_doc.split("\n")
        rebuilt = [f'{indent}"""{nl[0]}']
        for x in nl[1:]:
            rebuilt.append(f"{indent}{x}" if x.strip() else "")
        if len(nl) == 1:
            rebuilt[0] += '"""'
        else:
            rebuilt.append(f'{indent}"""')
        edits.append((s, e, rebuilt))

    handle(tree, "<module>")
    visit(tree, "")
    if not edits:
        return 0
    for s, e, repl in sorted(edits, key=lambda x: -x[0]):
        lines[s : e + 1] = repl
    Path(path).write_text("\n".join(lines))
    return len(edits)


if __name__ == "__main__":
    total = sum(process(p) for p in sys.argv[1:])
    print(f"rewrote {total} docstrings across {len(sys.argv) - 1} files")
