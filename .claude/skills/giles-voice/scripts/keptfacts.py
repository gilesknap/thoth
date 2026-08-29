"""Reports the hard facts a docstring rewrite dropped, against a git ref.

Prose can be tightened freely, but some tokens carry what no paraphrase reproduces: a
parameter name, a literal symbol, a constant, an exception type, an issue reference and
a Sphinx cross-reference. This walks both ASTs, pairs each docstring by qualname, and
lists what the old one said and the new one does not.

It also flags two things a diff hides: the control byte an unescaped regex escape leaves
in a docstring, and the capitalisation slip the comment rule invites, where opening a
sentence with a capital rewrites an identifier that is lowercase by nature.

Usage: keptfacts.py <git-ref> [path]... , defaulting to src, exiting non-zero on a hit.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

DOUBLE = re.compile(r"``([^`\n]{2,60})``")
SINGLE = re.compile(r"`([^`\n]{2,60})`")
CONSTANT = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
REFERENCE = re.compile(r"(?:#\d{1,5}|\b(?:ADR|SPEC)\s+(?:section\s+)?[\w.]+)")
ROLE = re.compile(r":(?:class|meth|func|data|mod|attr|exc):`~?([^`\n]+)`")
EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b")
SECTION = re.compile(r"^\s*(Args|Returns|Yields|Raises|Attributes):\s*$", re.M)
ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|etc)\.\s+[A-Z]")
OPENER = re.compile(r"#\s*([A-Z][A-Za-z0-9_]*)([\[(.=]?)")


def owners(tree: ast.Module) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Maps every qualname in a tree to its docstring and its parameter names."""
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    doc = ast.get_docstring(tree)
    if doc:
        out["<module>"] = (doc, ())

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            name = f"{prefix}{child.name}"
            doc = ast.get_docstring(child)
            if doc:
                params: tuple[str, ...] = ()
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = child.args
                    params = tuple(
                        p.arg
                        for p in a.posonlyargs + a.args + a.kwonlyargs
                        if p.arg not in ("self", "cls")
                    )
                out[name] = (doc, params)
            walk(child, f"{name}.")

    walk(tree, "")
    return out


def is_symbol(token: str) -> bool:
    """Rejects a backtick span that is really a clause rather than a symbol."""
    token = token.strip()
    return bool(token) and not {",", ";"} & set(token) and len(token.split()) <= 6


def tokens(text: str) -> dict[str, set[str]]:
    """Extracts the token classes worth preserving from one docstring."""
    roles = set(ROLE.findall(text))
    # a role carries its target in backticks too, and a double span would otherwise let
    # a single one match the prose between two of them, so peel both off in order
    plain = ROLE.sub(" ", text)
    literals = set(DOUBLE.findall(plain))
    literals |= set(SINGLE.findall(DOUBLE.sub(" ", plain)))
    literals = {t.strip() for t in literals if is_symbol(t)}
    return {
        "symbol": literals | set(CONSTANT.findall(text)),
        "ref": set(REFERENCE.findall(text)),
        "role": roles,
        "exc": set(EXCEPTION.findall(text)),
    }


def code_names(tree: ast.Module) -> set[str]:
    """Collects every name, attribute and string literal the executable code carries."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    return out


def lost(
    old: str, new: str, params: tuple[str, ...], code: set[str]
) -> dict[str, list[str]]:
    """Returns the old docstring's facts that neither the new one nor the code gives."""
    was, now = tokens(old), tokens(new)
    out = {k: sorted(was[k] - now[k]) for k in was}
    # a token the code spells out is recoverable by reading two lines down, and the
    # rule already says a restatement of the signature may go
    for kind in ("symbol", "role"):
        out[kind] = [t for t in out[kind] if t.lstrip("~").split(".")[-1] not in code]
    # an exception type is different: the point of naming it is not having to read the
    # body, so it survives unless a Raises: block now carries it
    if SECTION.search(new):
        out["exc"] = []
    # a parameter counts as documented by a bare mention or by an Args: entry, so only
    # one that has left the docstring altogether is a loss
    out["param"] = sorted(
        p
        for p in params
        if re.search(rf"\b{re.escape(p)}\b", old)
        and not re.search(rf"\b{re.escape(p)}\b", new)
    )
    return {k: v for k, v in out.items() if v}


def control_bytes(source: str) -> list[tuple[int, str]]:
    """Finds the control characters an unescaped regex escape leaves in a docstring."""
    rows = []
    for number, line in enumerate(source.split("\n"), start=1):
        found = sorted({c for c in line if c in "\x07\x08\x0b\x0c\x00"})
        if found:
            names = ", ".join(f"0x{ord(c):02x}" for c in found)
            rows.append((number, f"{names} in {line.strip()[:60]!r}"))
    return rows


def case_slips(source: str) -> list[tuple[int, str]]:
    """Finds comments whose opening capital rewrites a lowercase identifier."""
    rows = []
    for number, line in enumerate(source.split("\n"), start=1):
        stripped = line.strip()
        if ABBREVIATION.search(stripped):
            rows.append((number, stripped))
            continue
        if not stripped.startswith("#"):
            continue
        match = OPENER.match(stripped)
        if not match:
            continue
        word, suffix = match.group(1), match.group(2)
        lower = word[0].lower() + word[1:]
        # an ordinary English word also opens a comment, so only a token that looks
        # like code counts: snake_case, or one a subscript, call, attribute or
        # assignment follows
        if "_" not in word and not suffix:
            continue
        if lower != word and re.search(rf"\b{re.escape(lower)}\b", source):
            rows.append((number, stripped))
    return rows


def at_ref(ref: str, path: Path) -> str:
    """Reads a file's contents at a git ref, returning empty when it is absent there."""
    done = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    return done.stdout if done.returncode == 0 else ""


def report(ref: str, path: Path) -> int:
    """Prints one file's dropped facts and hidden slips, returning the hit count."""
    source = path.read_text()
    before = at_ref(ref, path)
    if not before:
        return 0
    tree = ast.parse(source)
    old, new = owners(ast.parse(before)), owners(tree)
    code = code_names(tree)
    hits = 0
    for name, (old_doc, params) in old.items():
        if name not in new:
            continue
        gone = lost(old_doc, new[name][0], params, code)
        if not gone:
            continue
        # a promoted Args: entry is the fix for a dropped parameter, so say when the
        # rewrite has no block to have promoted it into
        blockless = "param" in gone and not SECTION.search(new[name][0])
        hits += sum(len(v) for v in gone.values())
        print(f"{path}::{name}{'  (no Args: block)' if blockless else ''}")
        for kind, values in sorted(gone.items()):
            print(f"    {kind:<7} {', '.join(values)}")
    for number, text in control_bytes(source):
        hits += 1
        print(f"{path}:{number}  control  {text}")
    for number, text in case_slips(source):
        hits += 1
        print(f"{path}:{number}  case  {text}")
    return hits


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: keptfacts.py <git-ref> [path]...")
    ref, args = sys.argv[1], sys.argv[2:]
    roots = [Path(a) for a in args] or [Path("src")]
    files = sorted(
        {f for r in roots for f in ([r] if r.is_file() else r.rglob("*.py"))}
    )
    total = sum(report(ref, f) for f in files)
    print(f"{total} fact(s) dropped against {ref}")
    sys.exit(1 if total else 0)
