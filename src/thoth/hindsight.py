"""Subprocess wrappers over the official ``hindsight`` semantic-index CLI.

This module is the appliance's *only* path to Hindsight, and Hindsight is a
**rebuildable derived index** over the canonical vault (SPEC sections 8 and 15),
never the store of record. :class:`Hindsight` shells out to the ``hindsight``
CLI (https://hindsight.vectorize.io/sdks/cli) and never imports any ``hindsight``
Python package, so importing this module at pytest collection is always safe even
on a bare checkout where the binary and its Postgres/Gemini backend are absent.

The CLI surface (from the official docs) drives the shape of this wrapper:

* The binary is ``hindsight`` and the verbs are **two tokens** under ``memory`` --
  ``memory retain <bank_id> "<text>"``, ``memory recall <bank_id> "<query>"``. The
  **bank id is a positional argument** of each subcommand (not ``-p``); ``-p`` is the
  **profile** (an optional, named CLI profile), so :func:`base_args` is just
  ``hindsight`` plus an optional ``-p <profile>`` and the bank id is appended by each
  method (see :func:`base_args`, :attr:`Hindsight.bank`).
* ``recall`` emits structured output with ``-o json``; this wrapper parses that JSON
  (:func:`parse_recall`) rather than scraping pretty stdout, and recovers each hit's
  vault path from its ``rel`` tag. (The official CLI also supports
  ``--tags <rel> --tags-match all`` to filter recall by relevance tags; thoth does
  not send that filter -- it recalls unfiltered and keys provenance off result tags.)

Provenance survives Hindsight's **LLM fact-extraction** (SPEC section 8): a whole-page
``retain`` may be split into several atomic facts, so the in-band
``SOURCE: <rel-path>`` sentinel line (:func:`retain_text`) can end up attached to only
one fact, or none. **Tags are therefore the primary provenance channel:** :meth:`retain`
passes the vault-relative path as a tag (alongside the page type), and
:func:`parse_recall` recovers the path from each hit's ``rel`` tag, falling back to the
``SOURCE:`` sentinel only when tags are absent. Both channels are kept; tags are
preferred.

The exact binary name, profile flag, verb spellings, tag round-trip, and JSON field
names are **confirmed against the installed binary at VPS-time** (the VPS currently has
``hindsight-embed`` installed under the hermes user). Everything that could differ is
therefore overridable: the binary via ``THOTH_HINDSIGHT_BINARY`` (default
``hindsight``), the optional profile via ``THOTH_HINDSIGHT_PROFILE``, the bank via
``THOTH_HINDSIGHT_BANK`` (default ``thoth``), and the verb tokens via the module
constants (or a per-instance ``base_args`` / ``bank`` override at construction).

Only the standard library, :mod:`tenacity`, and :class:`thoth.config.Config` are
imported at module top level. Every process spawn goes through an injectable
:class:`SubprocessRunner` seam (defaulting to :func:`default_runner`, a thin
``subprocess.run`` wrapper) so tests substitute a fake that records argv and returns a
canned :class:`subprocess.CompletedProcess` without spawning anything.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from thoth.budget import KIND_HINDSIGHT, BudgetGuardLike
from thoth.config import Config

__all__ = [
    "DEFAULT_BANK",
    "DEFAULT_BINARY",
    "FORGET_SUBCOMMAND",
    "RECALL_SUBCOMMAND",
    "RETAIN_SUBCOMMAND",
    "SOURCE_SENTINEL",
    "Hindsight",
    "HindsightError",
    "HindsightTransientError",
    "RecallHit",
    "SubprocessRunner",
    "base_args",
    "default_runner",
    "page_record_text",
    "parse_recall",
    "retain_text",
]

DEFAULT_BINARY: str = "hindsight"
"""Official CLI binary name (overridable via ``THOTH_HINDSIGHT_BINARY`` at VPS-time)."""

DEFAULT_BANK: str = "thoth"
"""Default Hindsight bank id (overridable via ``THOTH_HINDSIGHT_BANK``)."""

# Two-token ``memory <verb>`` subcommands from the official CLI. Isolated as module
# constants so a VPS-time correction is one edit here, or a per-instance ``base_args``
# override / monkeypatching these names in a test.
RETAIN_SUBCOMMAND: tuple[str, ...] = ("memory", "retain")
"""Official subcommand words for storing facts (``hindsight ... memory retain``)."""

RECALL_SUBCOMMAND: tuple[str, ...] = ("memory", "recall")
"""Official subcommand words for semantic recall (``hindsight ... memory recall``)."""

# The official CLI has no per-PATH forget (clear-observations takes a memory_id, not a
# vault path); the authoritative reset is a full rebuild (SPEC section 8). ``forget`` is
# therefore best-effort and check-disabled, and its spelling is the one part of the
# surface with NO confirmed official equivalent -- overridable at VPS-time.
FORGET_SUBCOMMAND: tuple[str, ...] = ("memory", "forget")
"""Best-effort drop subcommand (no confirmed official per-path form; overridable)."""

SOURCE_SENTINEL: str = "SOURCE:"
"""In-band marker prefixing the vault path (fallback provenance behind tags)."""

# Match a SOURCE: line and capture the first whitespace-delimited token (the
# vault-relative path). Multiline so every line in CLI stdout is considered.
_SOURCE_LINE_RE: re.Pattern[str] = re.compile(r"^SOURCE:\s*(\S+)", re.MULTILINE)

# Permanent-failure signals: an exit code that means "your arguments/credentials are
# wrong", which a retry can never fix. Everything else (non-zero exit, spawn error,
# daemon-not-ready) is treated as transient and retried.
# VPS-time: if the installed CLI signals auth/credential failure with a distinct exit
# code (often 1, or a 70s sysexits.h code like EX_NOPERM=77), add it here so auth
# errors fail fast instead of retrying. The exact code is only observable on the binary.
_PERMANENT_EXIT_CODES: frozenset[int] = frozenset({2})
"""Exit codes treated as permanent (no retry): ``2`` is argparse's bad-usage code."""


def _opt_env(name: str) -> str | None:
    """Return ``os.environ[name]`` or ``None`` when unset or empty."""
    return os.environ.get(name) or None


def base_args(
    *, binary: str | None = None, profile: str | None = None
) -> tuple[str, ...]:
    """Build the CLI prefix before the verb: binary plus an optional ``-p <profile>``.

    The bank id is **not** part of this prefix (it is a positional argument of each
    subcommand). ``binary`` defaults to ``THOTH_HINDSIGHT_BINARY`` then
    :data:`DEFAULT_BINARY`; ``profile`` defaults to ``THOTH_HINDSIGHT_PROFILE`` and,
    when absent, no ``-p`` is emitted (the CLI uses its default profile).

    Args:
        binary: Override the binary name; falls back to the env var then the default.
        profile: Override the named CLI profile; falls back to the env var, else
            omitted.

    Returns:
        ``(binary,)`` or ``(binary, "-p", profile)``.
    """
    bin_name = binary or _opt_env("THOTH_HINDSIGHT_BINARY") or DEFAULT_BINARY
    prof = profile if profile is not None else _opt_env("THOTH_HINDSIGHT_PROFILE")
    if prof:
        return (bin_name, "-p", prof)
    return (bin_name,)


class HindsightError(Exception):
    """Raised when the ``hindsight`` CLI exits non-zero on a checked call."""


class HindsightTransientError(HindsightError):
    """A retryable CLI failure (non-zero exit, spawn error, or daemon-not-ready).

    Distinguished from a permanent :class:`HindsightError` (bad arguments / auth) so the
    bounded retry in :class:`Hindsight` re-attempts only failures a retry could fix.
    """


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One recall result: the vault path recovered for the hit plus its raw text.

    Attributes:
        path: The vault-relative path -- recovered from the hit's ``rel`` tag when
            present (the primary provenance channel), else parsed from a ``SOURCE:``
            line in the hit text (the fallback).
        text: The raw text the hit carried (provenance for callers).
        page_type: The page-type tag recovered for the hit (``entity``/``concept``/
            ``memory``/...), or ``""`` when none was carried. Recall is scoped by this
            tag so knowledge Q&A stays precise while life-admin content is indexed too
            (ADR 0004).
    """

    path: str
    text: str
    page_type: str = ""


def retain_text(rel_path: str, facts: str) -> str:
    """Prefix the ``SOURCE:`` sentinel so recall can echo the vault path back.

    The returned blob is exactly one ``SOURCE: <rel_path>`` line, a blank line, then
    ``facts``. This is the **fallback** provenance channel: because Hindsight runs LLM
    fact-extraction (not token chunking), a whole-page retain may be split into atomic
    facts and this in-band sentinel can attach to only one of them or none -- so the
    vault path is *also* (and preferentially) carried as a tag (see
    :meth:`Hindsight.retain`).

    Args:
        rel_path: The vault-relative path of the page these facts describe.
        facts: The curated fact text to retain.

    Returns:
        The fact text with the single ``SOURCE:`` sentinel line prepended.
    """
    return f"{SOURCE_SENTINEL} {rel_path}\n\n{facts}"


def _dedup_terms(*groups: Sequence[str]) -> list[str]:
    """Flatten the term groups into one order-preserving, case-folded de-duped list.

    Empty/whitespace-only terms are dropped; duplicates that differ only in case or
    surrounding whitespace collapse to their first-seen spelling. Used to build the
    page-record's entity/concept/tag lines without repeating a term the title already
    carried.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            term = raw.strip()
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                ordered.append(term)
    return ordered


def page_record_text(
    *,
    title: str,
    summary: str = "",
    entities: Sequence[str] = (),
    concepts: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> str:
    """Build the synthetic *page-record* block prepended to every retained page (#98).

    Hindsight's ``retain`` is **LLM fact-extraction**, not token-chunking or text
    embedding: a page with no discrete extractable facts (a photo/``memory`` page, a
    terse note, a bookmark, a list) yields **zero** stored units and is then completely
    absent from semantic recall. This guarantees a **single page-level record per
    page**, built only from material thoth already has (the classify/curate ``title`` +
    ``summary`` + ``entities``/``concepts`` + frontmatter ``tags``), so *every* curated
    page contributes at least one recallable unit regardless of fact density (issue #98,
    Direction 1).

    The block is phrased as plain **declarative assertions about the page** -- "This
    page is about ...", "It concerns ...", "It is tagged ..." -- precisely the shape the
    extractor keeps as a fact rather than discarding as "no facts" or re-splitting into
    many. It is *one* compact block (bounded per-page cost, no fact fan-out) and it
    *complements* extraction: it is prepended to the page body, so a fact-rich body
    still yields its extra facts while a fact-light body still lands its page-record.

    Phrasing -- not a CLI flag -- is the lever here because the wrapper's only retain
    surface is ``memory retain <bank> "<text>"`` (fact-extraction); there is no
    verbatim/embed retain mode to bypass extraction with.

    Args:
        title: The page title (always present; the record's anchor sentence).
        summary: The one-line curate ``summary`` gloss, if the page type carries one.
        entities: Named entities the page concerns (classify output / frontmatter).
        concepts: Named concepts the page concerns (classify output / frontmatter).
        tags: The page's frontmatter ``tags``.

    Returns:
        A short multi-line declarative block (never empty -- the title line is always
        present), suitable for prepending to the page body before :func:`retain_text`.
    """
    clean_title = title.strip() or "Untitled"
    lines: list[str] = [f"This page is about {clean_title}."]
    clean_summary = summary.strip()
    if clean_summary:
        lines.append(clean_summary)
    # Subject terms the title does not already carry, so a photo/terse page is still
    # recallable by what it *concerns* even when its body has no extractable facts.
    subjects = _dedup_terms(entities, concepts)
    subjects = [
        term for term in subjects if term.casefold() not in clean_title.casefold()
    ]
    if subjects:
        lines.append(f"It concerns {', '.join(subjects)}.")
    clean_tags = _dedup_terms(tags)
    if clean_tags:
        lines.append(f"It is tagged {', '.join(clean_tags)}.")
    return "\n".join(lines)


def _path_from_tags(tags: object) -> str | None:
    """Recover a vault path from a hit's ``rel`` tag, if any.

    Accepts either a list of ``"<key>:<value>"`` strings (e.g.
    ``"rel:entities/foo.md"``) or a mapping ``{"rel": "entities/foo.md"}``. The retain
    side tags pages with the page type *and* the bare vault-relative path; both the bare
    path and an explicit ``rel:`` prefixed form are honoured here so the round-trip is
    robust to whichever shape the CLI echoes. Returns ``None`` when no path-like tag is
    found.
    """
    candidates: list[str] = []
    if isinstance(tags, dict):
        value = tags.get("rel")
        if isinstance(value, str):
            candidates.append(value)
    elif isinstance(tags, (list, tuple)):
        for item in tags:
            if isinstance(item, str):
                candidates.append(item)
    for raw in candidates:
        tag = raw[len("rel:") :] if raw.startswith("rel:") else raw
        # A vault-relative page path is the only tag with a path separator + .md suffix;
        # the page-type tag ("entity", "concept", ...) never matches, so this cleanly
        # distinguishes the provenance tag from the type tag.
        if "/" in tag and tag.endswith(".md"):
            return tag
    return None


def _type_from_tags(tags: object) -> str:
    """Recover a page-type tag (e.g. ``entity`` / ``memory``) from a hit's tags.

    The retain side tags each page with ``[page_type, rel_path]`` (see
    :meth:`Hindsight.retain`), so the page type is the tag that is **not** the
    path-shaped ``rel`` tag. Accepts the same list / ``{"type": ...}`` mapping shapes
    :func:`_path_from_tags` tolerates and returns the first bare type token (no path
    separator, no ``.md`` suffix), or ``""`` when none is found. Callers use it to scope
    recall by domain at query time (ADR 0004).
    """
    candidates: list[str] = []
    if isinstance(tags, dict):
        for key in ("type", "page_type"):
            value = tags.get(key)
            if isinstance(value, str):
                candidates.append(value)
    elif isinstance(tags, (list, tuple)):
        for item in tags:
            if isinstance(item, str):
                candidates.append(item)
    for raw in candidates:
        tag = raw[len("type:") :] if raw.startswith("type:") else raw
        # The page-type tag is the bare token (entity/concept/memory/...); the rel-path
        # tag carries a path separator + .md suffix and is excluded here.
        if tag and "/" not in tag and not tag.endswith(".md"):
            return tag
    return ""


def _iter_recall_records(payload: object) -> Iterable[dict[str, object]]:
    """Yield per-hit JSON records from a parsed ``-o json`` recall payload.

    The exact envelope is VPS-confirmed, so several plausible shapes are tolerated: a
    bare list of records, or a dict wrapping the list under ``results``/``hits``/
    ``memories``/``observations``. Anything else yields nothing (an empty recall).
    """
    if isinstance(payload, list):
        records: Iterable[object] = payload
    elif isinstance(payload, dict):
        for key in ("results", "hits", "memories", "observations", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        else:
            records = ()
    else:
        records = ()
    for record in records:
        if isinstance(record, dict):
            yield record


def _hit_text(record: dict[str, object]) -> str:
    """Return the fact text from a recall record, trying the common field names."""
    for key in ("text", "content", "memory", "observation", "fact"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return ""


def parse_recall(stdout: str) -> list[RecallHit]:
    """Parse ``-o json`` recall output into ordered, de-duped :class:`RecallHit` values.

    Each hit's vault path is recovered from its ``rel`` tag (the primary provenance
    channel); when a hit carries no usable tag, the path is parsed from a ``SOURCE:``
    line in the hit text (the fallback). The first occurrence of each distinct path wins
    and later duplicates are dropped, preserving first-seen order. Hits with no
    recoverable path are skipped. If ``stdout`` is not valid JSON at all, the whole blob
    is treated as legacy pretty text and scanned for ``SOURCE:`` lines, so a CLI that
    ignores ``-o json`` still yields provenance.

    Args:
        stdout: The raw standard output captured from a ``recall ... -o json`` run.

    Returns:
        The de-duplicated :class:`RecallHit` list in first-seen order (``[]`` when no
        path could be recovered).
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return _parse_recall_sentinel(stdout)

    hits: list[RecallHit] = []
    seen: set[str] = set()
    for record in _iter_recall_records(payload):
        text = _hit_text(record)
        tags = record.get("tags")
        path = _path_from_tags(tags)
        if path is None:
            sentinel = _SOURCE_LINE_RE.search(text)
            path = sentinel.group(1) if sentinel else None
        if path is None or path in seen:
            continue
        seen.add(path)
        hits.append(
            RecallHit(
                path=path,
                text=text or f"{SOURCE_SENTINEL} {path}",
                page_type=_type_from_tags(tags),
            )
        )
    return hits


def _parse_recall_sentinel(stdout: str) -> list[RecallHit]:
    """Fallback parser: pull ``SOURCE: <path>`` lines out of legacy pretty stdout.

    Used only when recall output is not JSON (a CLI that does not honour ``-o json``).
    Every ``SOURCE:`` line yields one hit; the first occurrence of each path wins,
    preserving first-seen order. Lines without the sentinel are ignored.
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
    """Subprocess wrapper over the official ``hindsight`` CLI.

    Construct it from the frozen :class:`~thoth.config.Config` that owns the
    deployment. The instance is cheap and stateless beyond its configuration; every
    process spawn goes through the injected :class:`SubprocessRunner` (defaulting to
    :func:`default_runner`), so tests substitute a fake that records argv and
    returns a canned result. No ``hindsight`` Python package is ever imported.

    Each method builds ``base_args + <verb tokens> + [bank, payload, ...]``: the binary
    and optional profile come from ``base_args`` (env-overridable), the bank id is a
    positional argument (env ``THOTH_HINDSIGHT_BANK``, overridable at construction), and
    the verb tokens are the module ``*_SUBCOMMAND`` constants. The exact surface is
    VPS-confirmed; override the binary/profile via env, the bank via ``bank=``, or the
    whole prefix via ``base_args=``.

    Checked calls (:meth:`retain`, :meth:`recall`) are wrapped in a bounded retry
    (:mod:`tenacity`): up to ``retries`` attempts with exponential backoff,
    re-attempting only :class:`HindsightTransientError` (non-zero exit / spawn error /
    daemon-not-ready) and failing fast on a permanent :class:`HindsightError` (bad
    arguments / auth).
    """

    def __init__(
        self,
        config: Config,
        *,
        base_args: Sequence[str] | None = None,
        bank: str | None = None,
        runner: SubprocessRunner | None = None,
        timeout: float = 120.0,
        retries: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 4.0,
        guard: BudgetGuardLike | None = None,
    ) -> None:
        """Build a :class:`Hindsight` wrapper.

        Args:
            config: The frozen runtime configuration (retained for parity with the
                other appliance modules; the CLI reads its own backend config).
            base_args: The CLI prefix every subcommand is appended to (binary +
                optional ``-p <profile>``); defaults to :func:`base_args` (env-driven).
            bank: The Hindsight bank id (a positional arg of each subcommand); defaults
                to ``THOTH_HINDSIGHT_BANK`` then :data:`DEFAULT_BANK`.
            runner: The :class:`SubprocessRunner` seam; defaults to
                :func:`default_runner`.
            timeout: Seconds to allow each CLI call before
                :class:`subprocess.TimeoutExpired`.
            retries: Maximum attempts for a checked call (``1`` disables retry).
            retry_wait_initial: Initial exponential backoff in seconds.
            retry_wait_max: Cap on the exponential backoff in seconds.
            guard: An optional daily-spend guard (:class:`thoth.budget.BudgetGuard`);
                when wired, :meth:`retain` charges one Hindsight (Gemini
                fact-extraction) call against the daily budget *before* the CLI runs and
                raises :class:`thoth.budget.BudgetExceededError` once the cap is reached
                -- the guard for the ``reindex --full-rebuild`` cost burst (issue #16).
                ``None`` (the default) disables the cap, so existing callers are
                unaffected.
        """
        self._config = config
        self._guard = guard
        self._base_args: tuple[str, ...] = (
            tuple(base_args) if base_args is not None else _default_base_args()
        )
        self._bank: str = (
            bank
            if bank is not None
            else (_opt_env("THOTH_HINDSIGHT_BANK") or DEFAULT_BANK)
        )
        self._runner: SubprocessRunner = default_runner if runner is None else runner
        self._timeout = timeout
        self._retries = max(1, retries)
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max

    @property
    def base_args(self) -> tuple[str, ...]:
        """The CLI prefix (binary + optional profile) each subcommand appends to."""
        return self._base_args

    @property
    def bank(self) -> str:
        """The Hindsight bank id passed as a positional argument to each subcommand."""
        return self._bank

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Retain a curated page's facts, with the vault path carried as a tag.

        Builds ``base_args + RETAIN_SUBCOMMAND + [bank, retain_text(...)]`` plus a
        ``--document-tags`` value and runs it as a checked call. The vault path is added
        to ``tags`` as the **primary** provenance channel (recovered by
        :func:`parse_recall` from each recall hit's ``rel`` tag), because the LLM
        fact-extraction can split a page into atomic facts and strand the in-band
        ``SOURCE:`` sentinel; the page type is the other conventional tag (see
        :mod:`thoth.ingest`). A non-zero exit is a hard failure so the ingest pass can
        surface that the page did not land.

        Args:
            rel_path: The vault-relative path of the page being retained.
            facts: The curated fact text (the ``SOURCE:`` line is prepended for you).
            tags: Tags to attach (joined with commas); typically ``[page_type]`` or
                ``[page_type, rel_path]``. The ``rel_path`` is always included exactly
                once even if absent from ``tags``. Empty tags are dropped and no
                ``--document-tags`` flag is sent when none remain.

        Raises:
            HindsightError: if the CLI exits non-zero (permanent failures fail fast;
                transient ones are retried up to ``retries`` times first).
            thoth.budget.BudgetExceededError: when a budget guard is wired and the daily
                call cap has been reached (raised before the CLI runs, so no Gemini
                extraction is spent).
        """
        if self._guard is not None:
            # Charge before spawning the CLI so a cap-reached day defers the embedding
            # rather than spending it; this guards the reindex burst (issue #16).
            self._guard.charge(KIND_HINDSIGHT)
        argv: list[str] = [
            *self._base_args,
            *RETAIN_SUBCOMMAND,
            self._bank,
            retain_text(rel_path, facts),
        ]
        tag_value = _join_tags(tags, rel_path)
        if tag_value:
            # VPS-confirmed: the installed ``hindsight-embed`` CLI spells the document
            # tag flag ``--document-tags`` (``memory retain`` has no ``--tags``); the
            # rel-path still surfaces in each recall hit's ``tags`` so provenance is
            # intact (see :func:`parse_recall`).
            argv += ["--document-tags", tag_value]
        self._run_checked("retain", rel_path, argv)

    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        types: frozenset[str] | None = None,
    ) -> list[RecallHit]:
        """Semantic recall; return vault paths recovered from each hit's ``rel`` tag.

        Builds ``base_args + RECALL_SUBCOMMAND + [bank, query, '-o', 'json']`` and
        parses the JSON stdout with :func:`parse_recall` (tag-first, ``SOURCE:``
        fallback). An
        empty result set is a normal outcome and returns ``[]``; only a non-zero exit
        raises.

        Now that the index covers life-admin content too (ADR 0004), ``types`` scopes
        recall by the hit's ``page_type`` tag **client-side**: only hits whose page type
        is in ``types`` survive, so knowledge Q&A can filter to knowledge types and keep
        its precision while "search my memories" can ask for life-admin types. The
        filter runs *before* the ``limit`` cap. ``None`` (the default) keeps every hit,
        so the retain-then-probe round-trip and any "search everything" caller are
        unaffected.

        VPS-confirmed: the installed ``hindsight-embed`` ``memory recall`` has **no**
        ``--limit`` flag (it bounds output by ``--max-tokens``, default 4096), so the
        ``limit`` is applied **client-side** -- the parsed, de-duped hits are filtered
        by ``types`` then truncated to the first ``limit`` entries (first-seen order
        preserved by :func:`parse_recall`). Filtering on the parsed tag rather than a
        CLI flag also keeps "match any of these types" expressible (the CLI's
        ``--tags-match all`` cannot).

        Args:
            query: The natural-language recall query.
            limit: Maximum number of hits to return (applied client-side after parsing).
            types: When given, keep only hits whose ``page_type`` tag is in this set
                (the domain scope, e.g. :data:`thoth.vault.REFERENCE_TYPES`); ``None``
                keeps all.

        Returns:
            The de-duplicated :class:`RecallHit` list, scoped by ``types`` and capped at
            ``limit`` (``[]`` when nothing matched).

        Raises:
            HindsightError: if the CLI exits non-zero (permanent failures fail fast;
                transient ones are retried up to ``retries`` times first).
        """
        argv: list[str] = [
            *self._base_args,
            *RECALL_SUBCOMMAND,
            self._bank,
            query,
            "-o",
            "json",
        ]
        result = self._run_checked("recall", query, argv)
        hits = parse_recall(result.stdout)
        if types is not None:
            hits = [hit for hit in hits if hit.page_type in types]
        return hits[:limit]

    def forget(self, rel_path: str) -> None:
        """Best-effort drop of stale facts for a path; never raises on CLI failure.

        Builds ``base_args + FORGET_SUBCOMMAND + [bank, rel_path]`` and runs it with
        check-disabled semantics and **no retry**: the official CLI has no per-path
        forget, and the authoritative reset is a full rebuild (SPEC section 8), so a
        failed per-path forget must not abort -- nor slow -- an ingest or reindex pass.

        Args:
            rel_path: The vault-relative path whose facts should be dropped.
        """
        argv: list[str] = [*self._base_args, *FORGET_SUBCOMMAND, self._bank, rel_path]
        # check=False semantics by design: ignore the returncode entirely, no retry.
        try:
            self._runner(argv, timeout=self._timeout)
        except OSError:
            # Spawn failures are swallowed too: forget is best-effort.
            pass

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

    # ---- internals ---------------------------------------------------------------

    def _run_checked(
        self, op: str, subject: str, argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        """Run a checked CLI call with bounded retry on transient failures.

        Re-attempts only :class:`HindsightTransientError` (non-zero exit / spawn error /
        daemon-not-ready) up to ``retries`` times with exponential backoff; a permanent
        :class:`HindsightError` (bad arguments / auth) propagates immediately without a
        further attempt.

        Args:
            op: The operation name for diagnostics (``"retain"`` / ``"recall"``).
            subject: The path or query the call concerns (for the error message).
            argv: The full command line to run.

        Returns:
            The successful completed process.

        Raises:
            HindsightError: the last failure once attempts are exhausted (transient) or
                immediately (permanent).
        """
        retrying = Retrying(
            stop=stop_after_attempt(self._retries),
            wait=wait_exponential(
                multiplier=self._retry_wait_initial, max=self._retry_wait_max
            ),
            retry=retry_if_exception_type(HindsightTransientError),
            reraise=True,
        )
        return retrying(self._attempt, op, subject, argv)

    def _attempt(
        self, op: str, subject: str, argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        """Spawn once and classify the outcome (one retry attempt).

        Raises:
            HindsightTransientError: on a spawn error or a non-permanent non-zero exit.
            HindsightError: on a permanent non-zero exit (a bad-usage exit code).
        """
        try:
            result = self._runner(argv, timeout=self._timeout)
        except OSError as exc:
            # A spawn failure (binary missing, daemon socket not up yet) is transient.
            raise HindsightTransientError(
                f"hindsight {op} for {subject!r} could not spawn: {exc}"
            ) from exc
        if result.returncode == 0:
            return result
        message = self._format_failure(op, subject, result)
        if result.returncode in _PERMANENT_EXIT_CODES:
            raise HindsightError(message)
        raise HindsightTransientError(message)

    @staticmethod
    def _format_failure(
        op: str, subject: str, result: subprocess.CompletedProcess[str]
    ) -> str:
        """Build a diagnostic message embedding the op, subject, and CLI output."""
        return (
            f"hindsight {op} for {subject!r} failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )


def _default_base_args() -> tuple[str, ...]:
    """Return the env-driven default CLI prefix (binary + optional profile).

    A thin wrapper over :func:`base_args` with no overrides, evaluated each time a
    :class:`Hindsight` is built so a late ``THOTH_HINDSIGHT_BINARY`` /
    ``THOTH_HINDSIGHT_PROFILE`` in the environment is honoured.
    """
    return base_args()


def _join_tags(tags: Sequence[str], rel_path: str) -> str:
    """Comma-join non-empty ``tags`` with ``rel_path``, de-duped, order-preserving.

    ``rel_path`` is the primary provenance tag and is always present exactly once; the
    caller's tags (typically the page type, and possibly ``rel_path`` itself) are kept
    in order with empties dropped and duplicates removed.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for tag in (*tags, rel_path):
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ",".join(ordered)
