"""Reports docstring paragraphs that run past the three-sentence ceiling.

The rule this checks is the one nothing else could catch: a paragraph is too long only
in the reading, so a bulk pass can bury dozens of them without any test noticing. Lists,
code blocks and the Google sections are skipped, since none of them is prose.

Usage: longblocks.py [--show] [path]... , defaulting to src, exiting non-zero on a hit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

LIMIT = 3
SENTENCE = re.compile(r"[.!?](?:\s|$)")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[a-z]?\.)\s+\S")
SECTION = ("Args:", "Returns:", "Raises:", "Yields:", "Attributes:")


def paragraphs(body: str) -> list[list[str]]:
    """Splits a docstring body into the blank-line-separated blocks it already forms."""
    out: list[list[str]] = []
    cur: list[str] = []
    for line in body.split("\n"):
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def offenders(path: Path) -> list[tuple[int, int, str, str]]:
    """Returns one (sentences, words, location, text) row per over-long paragraph."""
    tree = ast.parse(path.read_text())
    rows = []
    nodes = [(tree, "<module>")] + [
        (n, n.name)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node, name in nodes:
        doc = ast.get_docstring(node)
        if not doc or "\n" not in doc:
            continue
        for block in paragraphs(doc.split("\n", 1)[1]):
            text = " ".join(line.strip() for line in block)
            if ITEM.match(block[0]) or any(k in text for k in SECTION):
                continue
            count = len(SENTENCE.findall(text))
            if count > LIMIT:
                rows.append((count, len(text.split()), f"{path}::{name}", text))
    return rows


if __name__ == "__main__":
    show = "--show" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    roots = [Path(a) for a in args] or [Path("src")]
    files = sorted(
        {f for r in roots for f in ([r] if r.is_file() else r.rglob("*.py"))}
    )
    rows = sorted((row for f in files for row in offenders(f)), reverse=True)
    for count, words, where, text in rows:
        print(f"{count:>3} sentences {words:>4}w  {where}")
        if show:
            print(f"    {text}\n")
    print(f"{len(rows)} paragraph(s) over {LIMIT} sentences")
    sys.exit(1 if rows else 0)
