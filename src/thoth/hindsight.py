"""Subprocess wrappers over the attested ``hindsight-embed`` semantic-index CLI.

This module is the appliance's *only* path to Hindsight, and Hindsight is a
**rebuildable derived index** over the canonical vault (SPEC sections 8 and 15),
never the store of record. :class:`Hindsight` shells out to the attested
``hindsight-embed -p hermes`` CLI and never imports any ``hindsight`` Python
package, so importing this module at pytest collection is always safe even on a
bare checkout where the binary and its Postgres/Gemini backend are absent.

Two facts from the SPEC shape the design:

* **The vault path is the key, carried in-band.** There is no verified
  ``reference=`` flag, so the owning vault-relative path travels inside the fact
  text as a single ``SOURCE: <vault-rel-path>`` sentinel line (see
  :func:`retain_text`) and is parsed back out of recall stdout by
  :func:`parse_recall`. This keeps the wrapper client-agnostic: whatever the CLI
  echoes, the path survives a round-trip as long as the sentinel line does.
* **The subcommand spellings are UNVERIFIED (VPS-time).** The attested surface is
  exactly ``hindsight-embed -p hermes`` (:data:`BASE_ARGS`, :data:`BANK`); the
  ``retain`` / ``query`` / ``forget`` subcommand words are best-guess constants
  isolated here (:data:`RETAIN_SUBCOMMAND` etc.) so a single edit — or a
  ``base_args`` / constant override at construction — re-points the wrapper once
  the real CLI is confirmed.

Only the standard library and :class:`thoth.config.Config` are imported at module
top level. Every process spawn goes through an injectable
:class:`SubprocessRunner` seam (defaulting to :func:`default_runner`, a thin
``subprocess.run`` wrapper) so tests substitute a fake that records argv and
returns a canned :class:`subprocess.CompletedProcess` without spawning anything.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from thoth.config import Config

__all__ = [
    "BANK",
    "BASE_ARGS",
    "FORGET_SUBCOMMAND",
    "RECALL_SUBCOMMAND",
    "RETAIN_SUBCOMMAND",
    "SOURCE_SENTINEL",
    "Hindsight",
    "HindsightError",
    "RecallHit",
    "SubprocessRunner",
    "default_runner",
    "parse_recall",
    "retain_text",
]

BANK: str = "hermes"
"""Attested Hindsight bank id (``-p hermes``); pinned, not a guess."""

BASE_ARGS: tuple[str, ...] = ("hindsight-embed", "-p", BANK)
"""Attested CLI surface subcommands append to (``hindsight-embed -p hermes``)."""

# UNVERIFIED subcommand spellings (SPEC section 15 open item). Isolated as
# module constants so the VPS-time fix is one edit here, or a per-instance
# override via the ``base_args`` seam / monkeypatching these names in a test.
RETAIN_SUBCOMMAND: tuple[str, ...] = ("retain",)
"""UNVERIFIED subcommand words for storing facts (overridable at VPS time)."""

RECALL_SUBCOMMAND: tuple[str, ...] = ("query",)
"""UNVERIFIED subcommand words for semantic recall (overridable at VPS time)."""

FORGET_SUBCOMMAND: tuple[str, ...] = ("forget",)
"""UNVERIFIED subcommand words for dropping a path's facts (overridable at VPS time)."""

SOURCE_SENTINEL: str = "SOURCE:"
"""In-band marker prefixing the vault path so recall can echo it back."""

# Match a SOURCE: line and capture the first whitespace-delimited token (the
# vault-relative path). Multiline so every line in CLI stdout is considered.
_SOURCE_LINE_RE: re.Pattern[str] = re.compile(r"^SOURCE:\s*(\S+)", re.MULTILINE)


class HindsightError(Exception):
    """Raised when the ``hindsight-embed`` CLI exits non-zero on a checked call."""


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One recall result: the vault path parsed from a ``SOURCE:`` line plus raw text.

    Attributes:
        path: The vault-relative path recovered from the fact's ``SOURCE:`` line.
        text: The raw stdout text the path was parsed from (provenance for callers).
    """

    path: str
    text: str


def retain_text(rel_path: str, facts: str) -> str:
    """Prefix the ``SOURCE:`` sentinel so recall can echo the vault path back.

    The returned blob is exactly one ``SOURCE: <rel_path>`` line, a blank line,
    then ``facts``. Keeping the path in-band (rather than assuming a CLI flag)
    makes the wrapper client-agnostic: the path survives the round-trip as long as
    the sentinel line is echoed by recall.

    Args:
        rel_path: The vault-relative path of the page these facts describe.
        facts: The curated fact text to retain.

    Returns:
        The fact text with the single ``SOURCE:`` sentinel line prepended.
    """
    return f"{SOURCE_SENTINEL} {rel_path}\n\n{facts}"


def parse_recall(stdout: str) -> list[RecallHit]:
    """Pull ``SOURCE: <path>`` lines out of CLI stdout into ordered, de-duped hits.

    Every ``SOURCE:`` line found anywhere in ``stdout`` yields one
    :class:`RecallHit`; the first occurrence of each distinct path wins and later
    duplicates are dropped, preserving first-seen order. Lines without the
    sentinel are ignored, so arbitrary CLI chatter around the markers is tolerated.

    Args:
        stdout: The raw standard output captured from a recall run.

    Returns:
        The de-duplicated :class:`RecallHit` list in first-seen order (``[]`` when
        no ``SOURCE:`` line is present).
    """
    hits: list[RecallHit] = []
    seen: set[str] = set()
    for match in _SOURCE_LINE_RE.finditer(stdout):
        path = match.group(1)
        if path in seen:
            continue
        seen.add(path)
        hits.append(RecallHit(path=path, text=match.group(0)))
    return hits


class SubprocessRunner(Protocol):
    """Seam over :func:`subprocess.run` so tests inject a fake without spawning a CLI.

    A runner takes the full ``argv`` and a ``timeout`` and returns a completed
    process with text streams. The default implementation is :func:`default_runner`.
    """

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv``; return the completed process (text mode, ``check=False``)."""
        ...


def default_runner(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` via :func:`subprocess.run` capturing text output, never raising.

    Uses ``capture_output=True``, ``text=True`` and ``check=False`` so the caller
    inspects ``returncode`` itself (the wrapper decides which calls are checked).
    This is the default :class:`SubprocessRunner`; tests inject their own.

    Args:
        argv: The full command line to execute (no shell).
        timeout: Seconds to allow before :class:`subprocess.TimeoutExpired`.

    Returns:
        The completed process with captured ``stdout``/``stderr`` as text.
    """
    return subprocess.run(  # noqa: S603 - fixed argv from module constants, no shell
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class Hindsight:
    """Subprocess wrapper over the attested ``hindsight-embed`` CLI.

    Construct it from the frozen :class:`~thoth.config.Config` that owns the
    deployment. The instance is cheap and stateless beyond its configuration; every
    process spawn goes through the injected :class:`SubprocessRunner` (defaulting to
    :func:`default_runner`), so tests substitute a fake that records argv and
    returns a canned result. No ``hindsight`` Python package is ever imported.

    Subcommand spellings are UNVERIFIED (SPEC section 15): override them at VPS time
    by editing the module constants, or per-instance by passing a custom
    ``base_args`` and monkeypatching the ``*_SUBCOMMAND`` names.
    """

    def __init__(
        self,
        config: Config,
        *,
        base_args: Sequence[str] = BASE_ARGS,
        runner: SubprocessRunner | None = None,
        timeout: float = 120.0,
    ) -> None:
        """Build a :class:`Hindsight` wrapper.

        Args:
            config: The frozen runtime configuration (retained for parity with the
                other appliance modules; the CLI reads its own backend config).
            base_args: The attested CLI prefix every subcommand is appended to;
                defaults to :data:`BASE_ARGS` (``hindsight-embed -p hermes``).
            runner: The :class:`SubprocessRunner` seam; defaults to
                :func:`default_runner`.
            timeout: Seconds to allow each CLI call before
                :class:`subprocess.TimeoutExpired`.
        """
        self._config = config
        self._base_args: tuple[str, ...] = tuple(base_args)
        self._runner: SubprocessRunner = default_runner if runner is None else runner
        self._timeout = timeout

    @property
    def base_args(self) -> tuple[str, ...]:
        """The attested CLI prefix every subcommand is appended to."""
        return self._base_args

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Retain a curated page's facts, keyed by the ``SOURCE:`` sentinel.

        Builds ``base_args + RETAIN_SUBCOMMAND + ['--text', retain_text(...),
        '--tags', '<tag1>,<tag2>,...']`` and runs it as a checked call. The page
        type and the vault-relative path are the conventional tags (see
        :mod:`thoth.ingest`). A non-zero exit is treated as a hard failure so the
        ingest pass can surface that the page did not land.

        Args:
            rel_path: The vault-relative path of the page being retained.
            facts: The curated fact text (the ``SOURCE:`` line is prepended for you).
            tags: Tags to attach (joined with commas); typically ``[page_type,
                rel_path]``. Empty tags are dropped and no ``--tags`` flag is sent
                when none remain.

        Raises:
            HindsightError: if the CLI exits non-zero (stderr surfaced in the message).
        """
        argv: list[str] = [
            *self._base_args,
            *RETAIN_SUBCOMMAND,
            "--text",
            retain_text(rel_path, facts),
        ]
        tag_value = ",".join(tag for tag in tags if tag)
        if tag_value:
            argv += ["--tags", tag_value]
        result = self._runner(argv, timeout=self._timeout)
        if result.returncode != 0:
            raise HindsightError(self._format_failure("retain", rel_path, result))

    def recall(self, query: str, *, limit: int = 10) -> list[RecallHit]:
        """Semantic recall; return vault paths parsed from ``SOURCE:`` lines.

        Builds ``base_args + RECALL_SUBCOMMAND + [query, '--limit', str(limit)]``
        and parses the stdout with :func:`parse_recall`. An empty result set is a
        normal outcome and returns ``[]``; only a non-zero exit raises.

        Args:
            query: The natural-language recall query.
            limit: Maximum number of results to request from the CLI.

        Returns:
            The de-duplicated :class:`RecallHit` list (``[]`` when nothing matched).

        Raises:
            HindsightError: if the CLI exits non-zero (stderr surfaced in the message).
        """
        argv: list[str] = [
            *self._base_args,
            *RECALL_SUBCOMMAND,
            query,
            "--limit",
            str(limit),
        ]
        result = self._runner(argv, timeout=self._timeout)
        if result.returncode != 0:
            raise HindsightError(self._format_failure("recall", query, result))
        return parse_recall(result.stdout)

    def forget(self, rel_path: str) -> None:
        """Best-effort drop of stale facts for a path; never raises on CLI failure.

        Builds ``base_args + FORGET_SUBCOMMAND + [rel_path]`` and runs it with
        check-disabled semantics: a non-zero exit is swallowed because the
        authoritative reset is a full rebuild (SPEC section 8), so a failed
        per-path forget must not abort an ingest or reindex pass.

        Args:
            rel_path: The vault-relative path whose facts should be dropped.
        """
        argv: list[str] = [*self._base_args, *FORGET_SUBCOMMAND, rel_path]
        # check=False semantics by design: ignore the returncode entirely.
        self._runner(argv, timeout=self._timeout)

    def probe(self, rel_path: str, query: str) -> bool:
        """Recall ``query`` and report whether ``rel_path`` is among the hits.

        This is the "did it land?" check the ingest retain pass runs after a
        :meth:`retain`: it recalls and tests membership of the just-written path.

        Args:
            rel_path: The vault-relative path expected to surface.
            query: The recall query to probe with.

        Returns:
            ``True`` if ``rel_path`` is one of the recalled paths, else ``False``.

        Raises:
            HindsightError: if the underlying :meth:`recall` exits non-zero.
        """
        return any(hit.path == rel_path for hit in self.recall(query))

    @staticmethod
    def _format_failure(
        op: str, subject: str, result: subprocess.CompletedProcess[str]
    ) -> str:
        """Build a diagnostic message embedding the op, subject, and CLI output."""
        return (
            f"hindsight {op} for {subject!r} failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )
