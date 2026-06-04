"""HTTP client over a standalone ``hindsight-api`` semantic-index server.

This module is the appliance's *only* path to Hindsight, and Hindsight is a
**rebuildable derived index** over the canonical vault (SPEC sections 8 and 15),
never the store of record. :class:`Hindsight` is a thin :class:`httpx.Client` over a
long-running ``hindsight-api`` HTTP server (default ``http://127.0.0.1:8888``); it
never imports any ``hindsight`` Python package, so importing this module at pytest
collection is always safe even on a bare checkout where the server and its
Postgres/Gemini backend are absent. Only the standard library, :mod:`httpx`,
:mod:`tenacity`, and :class:`thoth.config.Config` are imported at module top level.

The server exposes a REST surface under ``/v1/default/banks/{bank}``, where the **bank
is a path segment** (env ``THOTH_HINDSIGHT_BANK``, default :data:`DEFAULT_BANK`):

* ``retain`` -> ``POST .../memories`` with ``{"items": [...], "async": false}``; each
  item carries the curated facts as ``content`` plus the vault path as ``document_id``
  and ``context``, and the page type as ``tags``. (``async: false`` extracts facts
  synchronously, so a 2xx means the page is indexed.)
* ``recall`` -> ``POST .../memories/recall`` with ``{"query": ...}``; recall is sent
  **unfiltered** (no tags filter -- a tag filter would *suppress* untagged hits) and the
  page-type / path scope is applied client-side.
* ``forget`` -> ``DELETE .../documents/{document_id}`` (a real per-document delete).
* ``reset_bank`` -> ``DELETE .../{bank}`` (a full wipe for ``reindex --full-rebuild``).

Provenance survives Hindsight's **LLM fact-extraction** (SPEC section 8): a whole-page
``retain`` may be split into several atomic facts, each surfacing as its own recall hit,
so the vault path is carried on every hit and :func:`parse_recall` recovers it from the
first channel that yields a path, in preference order: the hit's echoed ``document_id``
(the item's ``document_id``, also the :meth:`Hindsight.forget` target); the hit's echoed
``context``; and finally the in-band ``SOURCE: <rel-path>`` sentinel
(:func:`retain_text`) surviving inside the hit text. The page type round-trips as the
hit's ``tags`` and recall is scoped by it client-side (ADR 0004). The item's ``tags``
are echoed onto every extracted fact and do **not** gate recall, so a page type carried
there stays fully recallable.

The seam for tests is an injectable :class:`httpx.BaseTransport`: tests pass an
:class:`httpx.MockTransport` that records each :class:`httpx.Request` and returns a
canned :class:`httpx.Response`, so no socket is ever opened.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

import httpx
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
    "DEFAULT_BASE_URL",
    "SOURCE_SENTINEL",
    "Hindsight",
    "HindsightError",
    "HindsightTransientError",
    "RecallHit",
    "parse_recall",
    "retain_text",
]

DEFAULT_BASE_URL: str = "http://127.0.0.1:8888"
"""Default ``hindsight-api`` base URL (overridable via ``THOTH_HINDSIGHT_BASE_URL``)."""

DEFAULT_BANK: str = "thoth"
"""Default Hindsight bank id (a path segment; overridable via ``THOTH_HINDSIGHT_BANK``).
"""

SOURCE_SENTINEL: str = "SOURCE:"
"""In-band marker prefixing the vault path (final fallback provenance channel)."""

# Match a SOURCE: line and capture the first whitespace-delimited token (the
# vault-relative path). Multiline so every line in a multi-fact hit is considered.
_SOURCE_LINE_RE: re.Pattern[str] = re.compile(r"^SOURCE:\s*(\S+)", re.MULTILINE)


class HindsightError(Exception):
    """Raised when a checked ``hindsight-api`` call fails permanently.

    A permanent failure is an HTTP 4xx (bad request / auth) -- a retry can never fix it,
    so it propagates immediately from the bounded retry in :class:`Hindsight`.
    """


class HindsightTransientError(HindsightError):
    """A retryable ``hindsight-api`` failure.

    Covers :class:`httpx.TransportError` (connect / timeout / read / write / pool) and
    HTTP **5xx** responses. Distinguished from a permanent :class:`HindsightError` (HTTP
    4xx) so the bounded retry re-attempts only failures a retry could fix.
    """


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One recall result: the vault path recovered for the hit plus its raw text.

    Attributes:
        path: The vault-relative path recovered for the hit, via the first provenance
            channel that yielded one (echoed ``document_id``; echoed ``context``; or a
            ``SOURCE:`` line in the text). See :func:`parse_recall`.
        text: The raw fact text the hit carried (provenance for callers).
        page_type: The page-type tag recovered for the hit (``entity``/``concept``/
            ``memory``/...), or ``""`` when none was carried. Recall is scoped by this
            tag client-side so knowledge Q&A stays precise while life-admin content is
            indexed too (ADR 0004).
    """

    path: str
    text: str
    page_type: str = ""


def retain_text(rel_path: str, facts: str) -> str:
    """Prefix the ``SOURCE:`` sentinel so recall can echo the vault path back.

    The returned blob is exactly one ``SOURCE: <rel_path>`` line, a blank line, then
    ``facts``. This is the **final fallback** provenance channel: because Hindsight runs
    LLM fact-extraction (not token chunking), a whole-page retain may be split into
    atomic facts and this in-band sentinel can attach to only one of them or none -- so
    the vault path is *also* (and preferentially) carried as ``document_id`` and
    ``context`` on the retained item (see :meth:`Hindsight.retain`).

    Args:
        rel_path: The vault-relative path of the page these facts describe.
        facts: The curated fact text to retain.

    Returns:
        The fact text with the single ``SOURCE:`` sentinel line prepended.
    """
    return f"{SOURCE_SENTINEL} {rel_path}\n\n{facts}"


def _str_field(record: dict[str, object], *keys: str) -> str | None:
    """Return the first ``record[key]`` that is a non-empty string, else ``None``."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _iter_recall_records(payload: dict[str, object]) -> Iterable[dict[str, object]]:
    """Yield per-hit JSON records from a parsed recall payload.

    The contract envelope nests hits under ``results``; a bare ``hits`` list is also
    tolerated. Non-dict records are skipped.
    """
    for key in ("results", "hits"):
        value = payload.get(key)
        if isinstance(value, list):
            for record in value:
                if isinstance(record, dict):
                    yield record
            return


def _hit_text(record: dict[str, object]) -> str:
    """Return the fact text from a recall record (the ``text``/``content`` field)."""
    return _str_field(record, "text", "content") or ""


def _path_for_hit(record: dict[str, object]) -> str | None:
    """Recover a vault path for one hit via the three provenance channels, in order.

    1. ``document_id`` echoed on the hit (PRIMARY -- the item's ``document_id``, which
       is the vault-relative path, and also the :meth:`Hindsight.forget` target).
    2. ``context`` echoed on the hit (the item's ``context``, also the path).
    3. a ``SOURCE: <rel-path>`` line surviving inside the hit text (final fallback).

    Returns the first path found, or ``None`` when no channel yields one.
    """
    path = _str_field(record, "document_id", "context")
    if path is not None:
        return path

    sentinel = _SOURCE_LINE_RE.search(_hit_text(record))
    if sentinel is not None:
        return sentinel.group(1)
    return None


def _page_type_for_hit(record: dict[str, object]) -> str:
    """Recover the page-type tag for one hit from its echoed ``tags``.

    The retain item carries the page type in ``tags`` (the field Hindsight echoes onto
    every extracted fact). Returns the first tag token that is **not** a vault path (a
    path-shaped ``a/b.md`` tag is skipped so a belt-and-braces path tag never
    masquerades as the page type), or ``""`` when none was carried.
    """
    tags = record.get("tags")
    if isinstance(tags, (list, tuple)):
        for item in tags:
            if isinstance(item, str) and item and not _is_path_tag(item):
                return item
    return ""


def _is_path_tag(tag: str) -> bool:
    """Return ``True`` when ``tag`` looks like a vault path (``a/b.md``), not a type."""
    return "/" in tag and tag.endswith(".md")


def parse_recall(payload: dict[str, object]) -> list[RecallHit]:
    """Parse a recall response payload into ordered, de-duped :class:`RecallHit` values.

    ``payload`` is the **parsed JSON dict** of a ``memories/recall`` response: a
    ``results`` list of hits. Each hit's vault path is recovered via
    :func:`_path_for_hit` (echoed ``document_id`` -> echoed ``context`` -> ``SOURCE:``
    sentinel, in that order), and its page type via :func:`_page_type_for_hit` (the
    hit's ``tags``). The first occurrence of each distinct path wins and later
    duplicates are dropped, preserving first-seen order. Hits with no recoverable path
    are skipped.

    Args:
        payload: The parsed JSON dict of a recall response.

    Returns:
        The de-duplicated :class:`RecallHit` list in first-seen order (``[]`` when no
        path could be recovered).
    """
    hits: list[RecallHit] = []
    seen: set[str] = set()
    for record in _iter_recall_records(payload):
        path = _path_for_hit(record)
        if path is None or path in seen:
            continue
        seen.add(path)
        text = _hit_text(record)
        hits.append(
            RecallHit(
                path=path,
                text=text or f"{SOURCE_SENTINEL} {path}",
                page_type=_page_type_for_hit(record),
            )
        )
    return hits


class Hindsight:
    """HTTP client over a standalone ``hindsight-api`` server.

    Construct it from the frozen :class:`~thoth.config.Config` that owns the deployment;
    the base URL defaults to ``config.hindsight_base_url``. The instance holds a
    long-lived :class:`httpx.Client` bound to ``{base_url}``; the bank is a path segment
    (env ``THOTH_HINDSIGHT_BANK``, overridable at construction) of every request URL.
    No ``hindsight`` Python package is ever imported.

    The transport is injectable (``transport=``) so tests pass an
    :class:`httpx.MockTransport` and no socket is opened. Checked calls (:meth:`retain`,
    :meth:`recall`, :meth:`reset_bank`) are wrapped in a bounded retry
    (:mod:`tenacity`): up to ``retries`` attempts with exponential backoff,
    re-attempting only :class:`HindsightTransientError` (transport error / HTTP 5xx) and
    failing fast on a permanent :class:`HindsightError` (HTTP 4xx). :meth:`forget` is
    best-effort: one attempt that swallows every error and never raises.
    """

    def __init__(
        self,
        config: Config,
        *,
        bank: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
        retries: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 4.0,
        guard: BudgetGuardLike | None = None,
    ) -> None:
        """Build a :class:`Hindsight` HTTP client.

        Args:
            config: The frozen runtime configuration; supplies the default
                ``base_url`` (``config.hindsight_base_url``).
            bank: The Hindsight bank id (a path segment); defaults to
                ``THOTH_HINDSIGHT_BANK`` then :data:`DEFAULT_BANK`.
            base_url: The ``hindsight-api`` base URL; defaults to
                ``config.hindsight_base_url``.
            transport: An :class:`httpx.BaseTransport` seam for tests (an
                :class:`httpx.MockTransport`); ``None`` uses the default network
                transport.
            timeout: Seconds to allow each HTTP call.
            retries: Maximum attempts for a checked call (``1`` disables retry).
            retry_wait_initial: Initial exponential backoff in seconds.
            retry_wait_max: Cap on the exponential backoff in seconds.
            guard: An optional daily-spend guard (:class:`thoth.budget.BudgetGuard`);
                when wired, :meth:`retain` charges one Hindsight (Gemini
                fact-extraction) call against the daily budget *before* the HTTP call
                and raises :class:`thoth.budget.BudgetExceededError` at the cap
                -- the guard for the ``reindex --full-rebuild`` cost burst (issue #16).
                ``None`` (the default) disables the cap, so existing callers are
                unaffected.
        """
        self._config = config
        self._guard = guard
        self._bank: str = (
            bank
            if bank is not None
            else (_opt_env("THOTH_HINDSIGHT_BANK") or DEFAULT_BANK)
        )
        self._base_url: str = base_url or config.hindsight_base_url
        self._timeout = timeout
        self._retries = max(1, retries)
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        # A long-lived client; the bank prefix is part of the per-call path so the same
        # client can also DELETE the bank itself (one segment up). No connection is made
        # until the first request, so constructing with the default transport on a bare
        # checkout is free.
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    @property
    def bank(self) -> str:
        """The Hindsight bank id carried as a path segment in every request URL."""
        return self._bank

    @property
    def base_url(self) -> str:
        """The ``hindsight-api`` base URL the client is bound to."""
        return self._base_url

    def close(self) -> None:
        """Close the underlying :class:`httpx.Client` (idempotent)."""
        self._client.close()

    def __enter__(self) -> Hindsight:
        """Return ``self`` for use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the underlying client on context exit."""
        self.close()

    # ---- public surface ----------------------------------------------------------

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Retain a curated page's facts, with the vault path carried as provenance.

        POSTs one item to ``.../memories``: ``content`` is :func:`retain_text` (facts
        with the ``SOURCE:`` sentinel prepended for the fallback channel),
        ``document_id`` and ``context`` both carry ``rel_path`` (the primary provenance
        channels, and ``document_id`` is the :meth:`forget` target), and ``tags``
        carries the **page type only** -- never the path. ``async`` is ``false`` so the
        call blocks until the facts are extracted and indexed. A non-2xx is a hard
        failure so the ingest pass can surface that the page did not land.

        Args:
            rel_path: The vault-relative path of the page being retained.
            facts: The curated fact text (the ``SOURCE:`` line is prepended for you).
            tags: Page-type token(s) (typically ``[page_type]``). ``rel_path`` is
                stripped out if present -- ``tags`` is the page-type axis only; the
                path travels via ``document_id``/``context``. When no page-type token
                remains, no ``tags`` key is sent.

        Raises:
            HindsightError: on a non-2xx response (HTTP 4xx fails fast; transient
                failures -- transport error / HTTP 5xx -- are retried up to ``retries``
                times first).
            thoth.budget.BudgetExceededError: when a budget guard is wired and the daily
                call cap has been reached (raised before the HTTP call, so no Gemini
                extraction is spent).
        """
        if self._guard is not None:
            # Charge before the HTTP call so a cap-reached day defers the embedding
            # rather than spending it; this guards the reindex burst (issue #16).
            self._guard.charge(KIND_HINDSIGHT)
        item: dict[str, object] = {
            "content": retain_text(rel_path, facts),
            "document_id": rel_path,
            "context": rel_path,
        }
        page_tags = [tag for tag in tags if tag and tag != rel_path]
        if page_tags:
            item["tags"] = page_tags
        body = {"items": [item], "async": False}
        self._request_checked("retain", rel_path, "POST", "/memories", json=body)

    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        types: frozenset[str] | None = None,
    ) -> list[RecallHit]:
        """Semantic recall; return vault paths recovered from each hit's provenance.

        POSTs ``{"query": query}`` to ``.../memories/recall`` (unfiltered -- no tags
        filter) and parses the JSON body with :func:`parse_recall`. An empty result set
        is a normal outcome and returns ``[]``; only a non-2xx raises.

        Now that the index covers life-admin content too (ADR 0004), ``types`` scopes
        recall by the hit's ``page_type`` **client-side**: only hits whose page type is
        in ``types`` survive, so knowledge Q&A can filter to knowledge types and keep
        its precision while "search my memories" can ask for life-admin types. The
        filter runs *before* the ``limit`` cap. ``None`` (the default) keeps every hit,
        so the retain-then-probe round-trip and any "search everything" caller are kept.

        Args:
            query: The natural-language recall query.
            limit: Maximum number of hits to return (applied client-side after parsing).
            types: When given, keep only hits whose ``page_type`` is in this set (the
                domain scope, e.g. :data:`thoth.vault.REFERENCE_TYPES`); ``None`` keeps
                all.

        Returns:
            The de-duplicated :class:`RecallHit` list, scoped by ``types`` and capped at
            ``limit`` (``[]`` when nothing matched).

        Raises:
            HindsightError: on a non-2xx response (HTTP 4xx fails fast; transient
                failures are retried up to ``retries`` times first).
        """
        response = self._request_checked(
            "recall", query, "POST", "/memories/recall", json={"query": query}
        )
        hits = parse_recall(_response_json(response))
        if types is not None:
            hits = [hit for hit in hits if hit.page_type in types]
        return hits[:limit]

    def forget(self, rel_path: str) -> None:
        """Best-effort per-document delete; never raises on failure.

        Issues a single ``DELETE .../documents/{rel_path}`` with check-disabled
        semantics and **no retry**: a failed forget must not abort -- nor slow -- an
        ingest or reindex pass, so every error (transport or HTTP status) is swallowed.

        Args:
            rel_path: The vault-relative path whose document should be deleted (the
                ``document_id`` set on :meth:`retain`).
        """
        try:
            self._client.request("DELETE", self._doc_path(rel_path))
        except httpx.HTTPError:
            # Best-effort: swallow transport errors. Non-2xx statuses are ignored too
            # (we never call raise_for_status here).
            pass

    def reset_bank(self) -> None:
        """Wipe the whole bank (``DELETE .../{bank}``) for ``reindex --full-rebuild``.

        A checked call with the same 4xx/5xx classification and bounded retry as
        :meth:`retain`/:meth:`recall`.

        Raises:
            HindsightError: on a non-2xx response (HTTP 4xx fails fast; transient
                failures are retried up to ``retries`` times first).
        """
        # The bank itself is one segment up from the per-call ``/memories`` paths; an
        # absolute path on the client (whose base_url ends ``/banks/{bank}``) addresses
        # the full URL, so build the bank URL explicitly.
        self._request_checked(
            "reset_bank", self._bank, "DELETE", self._bank_url(), absolute=True
        )

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
            HindsightError: if the underlying :meth:`recall` fails on a non-2xx.
        """
        return any(hit.path == rel_path for hit in self.recall(query))

    # ---- internals ---------------------------------------------------------------

    def _bank_prefix(self) -> str:
        """Return the URL-encoded ``/v1/default/banks/{bank}`` path prefix."""
        return f"/v1/default/banks/{quote(self._bank, safe='')}"

    def _bank_url(self) -> str:
        """Return the absolute URL of the bank itself (for ``reset_bank``)."""
        return f"{self._base_url.rstrip('/')}{self._bank_prefix()}"

    def _doc_path(self, rel_path: str) -> str:
        """Return the bank-relative ``/documents/{rel_path}`` path.

        The ``document_id`` is the vault-relative path, so its ``/`` separators are kept
        as path separators (only other reserved characters are percent-encoded).
        """
        return f"{self._bank_prefix()}/documents/{quote(rel_path, safe='/')}"

    def _request_checked(
        self,
        op: str,
        subject: str,
        method: str,
        path: str,
        *,
        json: object | None = None,
        absolute: bool = False,
    ) -> httpx.Response:
        """Issue a checked HTTP call with bounded retry on transient failures.

        ``path`` is appended to the ``/v1/default/banks/{bank}`` prefix unless
        ``absolute`` is set (then it is the full URL, e.g. the bank itself). Re-attempts
        only :class:`HindsightTransientError` (transport error / HTTP 5xx) up to
        ``retries`` times with exponential backoff; a permanent :class:`HindsightError`
        (HTTP 4xx) propagates immediately.

        Args:
            op: The operation name for diagnostics (``"retain"`` / ``"recall"`` / ...).
            subject: The path or query the call concerns (for the error message).
            method: The HTTP method.
            path: The bank-relative path, or the absolute URL when ``absolute`` is set.
            json: An optional JSON body.
            absolute: When ``True``, ``path`` is used verbatim as the request URL.

        Returns:
            The successful (2xx) response.

        Raises:
            HindsightError: the last failure once attempts are exhausted (transient) or
                immediately (permanent).
        """
        url = path if absolute else f"{self._bank_prefix()}{path}"
        retrying = Retrying(
            stop=stop_after_attempt(self._retries),
            wait=wait_exponential(
                multiplier=self._retry_wait_initial, max=self._retry_wait_max
            ),
            retry=retry_if_exception_type(HindsightTransientError),
            reraise=True,
        )
        return retrying(self._attempt, op, subject, method, url, json)

    def _attempt(
        self,
        op: str,
        subject: str,
        method: str,
        url: str,
        json: object | None,
    ) -> httpx.Response:
        """Issue one HTTP call and classify the outcome (one retry attempt).

        Raises:
            HindsightTransientError: on a transport error or an HTTP 5xx response.
            HindsightError: on an HTTP 4xx response (bad request / auth).
        """
        try:
            response = self._client.request(method, url, json=json)
        except httpx.TransportError as exc:
            # Connect / timeout / read / write / pool errors are transient.
            raise HindsightTransientError(
                f"hindsight {op} for {subject!r} transport error: {exc}"
            ) from exc
        status = response.status_code
        if status < 400:
            return response
        message = self._format_failure(op, subject, response)
        if status >= 500:
            raise HindsightTransientError(message)
        raise HindsightError(message)

    @staticmethod
    def _format_failure(op: str, subject: str, response: httpx.Response) -> str:
        """Build a diagnostic message embedding the op, subject, status, and body."""
        return (
            f"hindsight {op} for {subject!r} failed "
            f"(HTTP {response.status_code}). body: {response.text.strip()!r}"
        )


def _opt_env(name: str) -> str | None:
    """Return ``os.environ[name]`` or ``None`` when unset or empty."""
    return os.environ.get(name) or None


def _response_json(response: httpx.Response) -> dict[str, object]:
    """Decode a body to a JSON dict (empty dict on a non-object / decode error)."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
