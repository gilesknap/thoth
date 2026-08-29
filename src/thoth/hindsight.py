"""HTTP client over a standalone ``hindsight-api`` semantic-index server.

This module is the appliance's *only* path to Hindsight, and Hindsight is a
**rebuildable derived index** over the canonical vault (SPEC sections 8 and 15),
never the store of record. :class:`Hindsight` is a thin :class:`httpx.Client` over a
long-running ``hindsight-api`` HTTP server, ``http://127.0.0.1:8888`` by default. It
never imports any ``hindsight`` Python package, so importing this module at pytest
collection is always safe, even on a bare checkout lacking the server and its Postgres
and Gemini backend. Module top level imports only the standard library, :mod:`httpx`,
:mod:`tenacity` and :class:`thoth.config.Config`.

The server exposes a REST surface under ``/v1/default/banks/{bank}``, where the **bank
is a path segment** (env ``THOTH_HINDSIGHT_BANK``, default :data:`DEFAULT_BANK`):

* ``retain`` maps to ``POST .../memories`` with ``{"items": [...], "async": false}``,
  each item carrying the curated facts as ``content``, the vault path as
  ``document_id`` and ``context``, and the page type as ``tags``. ``async: false``
  extracts facts synchronously, so a 2xx means the page is indexed.
* ``recall`` maps to ``POST .../memories/recall`` with ``{"query": ...}``, sent
  **unfiltered** because a tags filter would *suppress* untagged hits, and the
  page-type and path scope is applied client-side.
* ``forget`` maps to ``DELETE .../documents/{document_id}``, a real per-document
  delete.
* ``reset_bank`` maps to ``DELETE .../{bank}``, a full wipe for
  ``reindex --full-rebuild``.

Provenance survives Hindsight's **LLM fact-extraction** (SPEC section 8), so every
recall hit carries the vault path, recovered through redundant channels that
:func:`_path_for_hit` documents. The page type round-trips as the hit's ``tags`` and
recall is scoped by it client-side (ADR 0004). The item's ``tags`` do **not** gate
recall, so a page type carried there stays fully recallable.

The seam for tests is an injectable :class:`httpx.BaseTransport`. A test passes an
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

    A permanent failure is an HTTP 4xx, a bad request or an auth error, which a retry
    can never fix, so it propagates immediately from the bounded retry in
    :class:`Hindsight`.
    """


class HindsightTransientError(HindsightError):
    """A retryable ``hindsight-api`` failure.

    Covers :class:`httpx.TransportError`, for a connect, timeout, read, write or pool
    failure, and an HTTP **5xx** response. It stays distinct from a permanent
    :class:`HindsightError`, an HTTP 4xx, so the bounded retry re-attempts only failures
    a retry could fix.
    """


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One recall result: the vault path recovered for the hit plus its raw text.

    Attributes:
        path: The vault-relative path recovered for the hit, through the first
            provenance channel that yielded one, as :func:`_path_for_hit` documents.
        text: The raw fact text the hit carried, the caller's provenance.
        page_type: The page-type tag recovered for the hit, such as ``entity``,
            ``concept`` or ``memory``, or ``""`` when none was carried. Recall is scoped
            by this tag client-side, so knowledge Q&A stays precise while life-admin
            content is indexed too (ADR 0004).
    """

    path: str
    text: str
    page_type: str = ""


def retain_text(rel_path: str, facts: str) -> str:
    """Prefix the ``SOURCE:`` sentinel so recall can echo the vault path back.

    The returned blob is exactly one ``SOURCE: <rel_path>`` line, a blank line, then
    ``facts``. This is the **final fallback** provenance channel, and
    :func:`_path_for_hit` gives the channel order.

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

    The contract envelope nests hits under ``results``, and a bare ``hits`` list is
    tolerated too. A non-dict record is skipped.
    """
    for key in ("results", "hits"):
        value = payload.get(key)
        if isinstance(value, list):
            for record in value:
                if isinstance(record, dict):
                    yield record
            return


def _hit_text(record: dict[str, object]) -> str:
    """Return the fact text from a recall record's ``text`` or ``content`` field."""
    return _str_field(record, "text", "content") or ""


def _path_for_hit(record: dict[str, object]) -> str | None:
    """Recover a vault path for one hit via the three provenance channels, in order.

    1. ``document_id`` echoed on the hit. This is PRIMARY: the item's ``document_id`` is
       the vault-relative path, and also the :meth:`Hindsight.forget` target.
    2. ``context`` echoed on the hit, which is the item's ``context`` and also the path.
    3. a ``SOURCE: <rel-path>`` line surviving inside the hit text, the final fallback.

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

    The retain item carries the page type in ``tags``, the field Hindsight echoes onto
    every extracted fact. Returns the first tag token that is **not** a vault path,
    since skipping a path-shaped ``a/b.md`` tag stops a belt-and-braces path tag
    masquerading as the page type, or ``""`` when none was carried.
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

    ``payload`` is the **parsed JSON dict** of a ``memories/recall`` response, holding a
    ``results`` list of hits. :func:`_path_for_hit` recovers each hit's vault path, and
    :func:`_page_type_for_hit` recovers its page type from the hit's ``tags``. The first
    occurrence of each distinct path wins and later duplicates drop, preserving
    first-seen order. A hit with no recoverable path is skipped.

    Args:
        payload: The parsed JSON dict of a recall response.

    Returns:
        The de-duplicated :class:`RecallHit` list in first-seen order, ``[]`` when no
        path could be recovered.
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

    Construct it from the frozen :class:`~thoth.config.Config` that owns the deployment,
    where the base URL defaults to ``config.hindsight_base_url``. The instance holds a
    long-lived :class:`httpx.Client` bound to ``{base_url}``, and the bank is a path
    segment of every request URL, from ``THOTH_HINDSIGHT_BANK`` and overridable at
    construction. No ``hindsight`` Python package is ever imported.

    The ``transport=`` seam is injectable, so a test passes an
    :class:`httpx.MockTransport` and no socket is opened. The checked calls
    :meth:`retain`, :meth:`recall` and :meth:`reset_bank` are wrapped in a bounded
    :mod:`tenacity` retry of up to ``retries`` attempts with exponential backoff,
    re-attempting only :class:`HindsightTransientError`, a transport error or HTTP 5xx,
    and failing fast on a permanent :class:`HindsightError`, an HTTP 4xx. :meth:`forget`
    is best-effort: one attempt that swallows every error and never raises.
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
            config: The frozen runtime configuration, supplying the default ``base_url``
                from ``config.hindsight_base_url``.
            bank: The Hindsight bank id, a path segment, defaulting to
                ``THOTH_HINDSIGHT_BANK`` then :data:`DEFAULT_BANK`.
            base_url: The ``hindsight-api`` base URL, defaulting to
                ``config.hindsight_base_url``.
            transport: An :class:`httpx.BaseTransport` seam for tests, typically an
                :class:`httpx.MockTransport`. ``None`` uses the default transport.
            timeout: Seconds to allow each HTTP call.
            retries: Maximum attempts for a checked call, where ``1`` disables retry.
            retry_wait_initial: Initial exponential backoff in seconds.
            retry_wait_max: Cap on the exponential backoff in seconds.
            guard: An optional daily-spend :class:`thoth.budget.BudgetGuard`. When
                wired, :meth:`retain` charges one Hindsight Gemini fact-extraction
                call against the daily budget *before* the HTTP call, and raises
                :class:`thoth.budget.BudgetExceededError` at the cap, guarding the
                ``reindex --full-rebuild`` cost burst (issue #16). ``None``, the
                default, disables the cap, leaving existing callers unaffected.
        """
        self._config = config
        self._guard = guard
        self._bank: str = (
            bank
            if bank is not None
            else (os.environ.get("THOTH_HINDSIGHT_BANK") or DEFAULT_BANK)
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
        """Close the underlying :class:`httpx.Client`, idempotently."""
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

        POSTs one item to ``.../memories``, where ``content`` is :func:`retain_text`,
        ``document_id`` and ``context`` both carry ``rel_path`` as the provenance
        channels, and ``tags`` carries the **page type only**, never the path. ``async``
        is ``false``, so the call blocks until the facts are extracted and indexed. A
        non-2xx is a hard failure, so the ingest pass can surface that the page did not
        land.

        Args:
            rel_path: The vault-relative path of the page being retained.
            facts: The curated fact text, to which the ``SOURCE:`` line is prepended.
            tags: Page-type tokens, typically ``[page_type]``. ``rel_path`` is
                stripped when present, because ``tags`` is the page-type axis and the
                path travels through ``document_id`` and ``context``. With no
                page-type token left, no ``tags`` key is sent.

        Raises:
            HindsightError: on a non-2xx response, after the bounded retry on transient
                failures that the class docstring describes.
            thoth.budget.BudgetExceededError: when a budget guard is wired and the daily
                call cap is reached, raised before the HTTP call so no Gemini extraction
                is spent.
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

        POSTs ``{"query": query}`` to ``.../memories/recall``, unfiltered with no tags
        filter, and parses the JSON body with :func:`parse_recall`. An empty result set
        is a normal outcome returning ``[]``, and only a non-2xx raises.

        Now that the index covers life-admin content too (ADR 0004), ``types`` scopes
        recall by the hit's ``page_type`` **client-side**, so only hits whose page type
        is in ``types`` survive. Knowledge Q&A can therefore filter to knowledge types
        and keep its precision, while "search my memories" can ask for life-admin types.
        The filter runs *before* the ``limit`` cap. ``None``, the default, keeps every
        hit, preserving the retain-then-probe round-trip and any "search everything"
        caller.

        Args:
            query: The natural-language recall query.
            limit: Maximum hits to return, applied client-side after parsing.
            types: When given, keeps only hits whose ``page_type`` is in this set, the
                domain scope such as :data:`thoth.vault.REFERENCE_TYPES`. ``None`` keeps
                all.

        Returns:
            The de-duplicated :class:`RecallHit` list, scoped by ``types`` and capped at
            ``limit``, or ``[]`` when nothing matched.

        Raises:
            HindsightError: on a non-2xx response, as :meth:`retain`.
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
        semantics and **no retry**, because a failed forget must neither abort nor slow
        an ingest or reindex pass, so every transport or HTTP-status error is swallowed.

        Args:
            rel_path: The vault-relative path whose document should be deleted, the
                ``document_id`` set on :meth:`retain`.
        """
        try:
            self._client.request("DELETE", self._doc_path(rel_path))
        except httpx.HTTPError:
            # Best-effort: swallow transport errors. Non-2xx statuses are ignored too
            # (we never call raise_for_status here).
            pass

    def reset_bank(self) -> None:
        """Wipe the whole bank (``DELETE .../{bank}``) for ``reindex --full-rebuild``.

        A checked call with the same 4xx and 5xx classification and bounded retry as
        :meth:`retain` and :meth:`recall`.

        Raises:
            HindsightError: on a non-2xx response, as :meth:`retain`.
        """
        self._request_checked("reset_bank", self._bank, "DELETE", "")

    def probe(self, rel_path: str, query: str) -> bool:
        """Recall ``query`` and report whether ``rel_path`` is among the hits.

        This is the "did it land?" check the ingest retain pass runs after a
        :meth:`retain`, recalling and testing membership of the just-written path.

        Args:
            rel_path: The vault-relative path expected to surface.
            query: The recall query to probe with.

        Returns:
            ``True`` when ``rel_path`` is one of the recalled paths, else ``False``.

        Raises:
            HindsightError: when the underlying :meth:`recall` fails on a non-2xx.
        """
        return any(hit.path == rel_path for hit in self.recall(query))

    # ---- internals ---------------------------------------------------------------

    def _bank_prefix(self) -> str:
        """Return the URL-encoded ``/v1/default/banks/{bank}`` path prefix."""
        return f"/v1/default/banks/{quote(self._bank, safe='')}"

    def _doc_path(self, rel_path: str) -> str:
        """Return the bank-relative ``/documents/{rel_path}`` path.

        The ``document_id`` is the vault-relative path, so its ``/`` separators stay
        path separators and only other reserved characters are percent-encoded.
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
    ) -> httpx.Response:
        """Issue a checked HTTP call with bounded retry on transient failures.

        ``path`` is appended to the ``/v1/default/banks/{bank}`` prefix, and an empty
        ``path`` addresses the bank itself, for ``reset_bank``. It re-attempts only
        :class:`HindsightTransientError`, a transport error or HTTP 5xx, up to
        ``retries`` times with exponential backoff, while a permanent
        :class:`HindsightError`, an HTTP 4xx, propagates immediately.

        Args:
            op: The operation name, ``"retain"`` or ``"recall"``, for diagnostics.
            subject: The path or query the call concerns, for the error message.
            method: The HTTP method.
            path: The bank-relative path, ``""`` for the bank itself.
            json: An optional JSON body.

        Returns:
            The successful 2xx response.

        Raises:
            HindsightError: the last transient failure once attempts are exhausted, or a
                permanent failure immediately.
        """
        url = f"{self._bank_prefix()}{path}"
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
        """Issue one HTTP call, one retry attempt, and classify the outcome.

        Raises:
            HindsightTransientError: on a transport error or an HTTP 5xx response.
            HindsightError: on an HTTP 4xx response, a bad request or an auth error.
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
        """Build a diagnostic message embedding the op, subject, status and body."""
        return (
            f"hindsight {op} for {subject!r} failed "
            f"(HTTP {response.status_code}). body: {response.text.strip()!r}"
        )


def _response_json(response: httpx.Response) -> dict[str, object]:
    """Decode a body to a JSON dict, empty on a non-object or a decode error."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
