"""HTTP client over a standalone ``hindsight-api`` semantic-index server.

This is the appliance's only path to Hindsight, and Hindsight is a rebuildable derived
index over the canonical vault (SPEC sections 8 and 15), never the store of record.
:class:`Hindsight` is a thin :class:`httpx.Client` and never imports any ``hindsight``
Python package, so importing this at pytest collection is safe on a bare checkout where
the server and its Postgres backend are absent. The REST surface lives under
``/v1/default/banks/{bank}``, where the bank is a path segment:

* ``retain``     -> ``POST .../memories``
* ``recall``     -> ``POST .../memories/recall``
* ``forget``     -> ``DELETE .../documents/{document_id}``, a real per-document delete
* ``reset_bank`` -> ``DELETE .../{bank}``, the full wipe for ``reindex --full-rebuild``

Recall is sent unfiltered, because a tags filter would suppress untagged hits, and the
page-type scope is applied client-side instead (ADR 0004). Provenance survives
Hindsight's LLM fact-extraction: every hit carries the vault path through redundant
channels, and :func:`_path_for_hit` owns that story. Tests inject an
:class:`httpx.MockTransport`, so no socket is ever opened.
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

# Match a SOURCE: line and capture the first whitespace-delimited token, the vault
# relative path. Multiline so every line of a multi-fact hit is considered
_SOURCE_LINE_RE: re.Pattern[str] = re.compile(r"^SOURCE:\s*(\S+)", re.MULTILINE)


class HindsightError(Exception):
    """Raised when a checked ``hindsight-api`` call fails permanently.

    A permanent failure is an HTTP 4xx, which no retry can fix, so it propagates
    immediately out of the bounded retry.
    """


class HindsightTransientError(HindsightError):
    """A retryable ``hindsight-api`` failure.

    Covers :class:`httpx.TransportError` and HTTP 5xx. Kept distinct from a permanent
    :class:`HindsightError` so the retry re-attempts only what a retry could fix.
    """


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One recall result: the vault path recovered for the hit plus its raw text.

    Attributes:
        path: Vault-relative path, from the first channel that yielded one.
        text: The raw fact text the hit carried.
        page_type: The page-type tag, or "" when none was carried. Recall is scoped
            by this client-side so knowledge Q&A stays precise while life-admin
            content is indexed too (ADR 0004).
    """

    path: str
    text: str
    page_type: str = ""


def retain_text(rel_path: str, facts: str) -> str:
    """Prefixes the ``SOURCE:`` sentinel so recall can echo the vault path back.

    This is the final fallback provenance channel. The path also travels out-of-band on
    the retained item, and :func:`_path_for_hit` owns the channel order.

    Args:
        rel_path: Vault-relative path of the page these facts describe.
        facts: The curated fact text to retain.

    Returns:
        The fact text with one ``SOURCE:`` line prepended.
    """
    return f"{SOURCE_SENTINEL} {rel_path}\n\n{facts}"


def _str_field(record: dict[str, object], *keys: str) -> str | None:
    """Returns the first value that is a non-empty string."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _iter_recall_records(payload: dict[str, object]) -> Iterable[dict[str, object]]:
    """Yields the per-hit JSON records from a parsed recall payload.

    The contract envelope nests hits under ``results``, and a bare ``hits`` list is also
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
    """Returns the fact text carried by a recall record."""
    return _str_field(record, "text", "content") or ""


def _path_for_hit(record: dict[str, object]) -> str | None:
    """Recovers a vault path for one hit through three provenance channels, in order.

    1. ``document_id`` echoed on the hit. The primary channel, and also the
       :meth:`Hindsight.forget` target.
    2. ``context`` echoed on the hit, which also carries the path.
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
    """Recovers the page-type tag for one hit from its echoed ``tags``.

    A path-shaped tag is skipped, so a belt-and-braces path tag never masquerades as the
    page type.
    """
    tags = record.get("tags")
    if isinstance(tags, (list, tuple)):
        for item in tags:
            if isinstance(item, str) and item and not _is_path_tag(item):
                return item
    return ""


def _is_path_tag(tag: str) -> bool:
    """True when ``tag`` looks like a vault path rather than a page type."""
    return "/" in tag and tag.endswith(".md")


def parse_recall(payload: dict[str, object]) -> list[RecallHit]:
    """Parses a recall payload into ordered, de-duped :class:`RecallHit` values.

    The first occurrence of each distinct path wins and later duplicates are dropped, so
    first-seen order is preserved. Hits with no recoverable path are skipped.

    Args:
        payload: The parsed JSON dict of a recall response.

    Returns:
        The de-duplicated hits in first-seen order, or [] when none had a path.
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

    The instance holds a long-lived :class:`httpx.Client` bound to the base URL, with
    the bank as a path segment of every request. No ``hindsight`` package is ever
    imported.

    Checked calls are wrapped in a bounded :mod:`tenacity` retry that re-attempts only
    :class:`HindsightTransientError` and fails fast on a permanent
    :class:`HindsightError`. :meth:`forget` is best-effort and never raises.
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
        """Builds a :class:`Hindsight` HTTP client.

        Args:
            config: Frozen runtime config supplying the default base URL.
            bank: Bank id, defaulting to ``THOTH_HINDSIGHT_BANK`` then
                :data:`DEFAULT_BANK`.
            base_url: The ``hindsight-api`` base URL.
            transport: Test seam, typically an :class:`httpx.MockTransport`.
            timeout: Seconds to allow each HTTP call.
            retries: Maximum attempts for a checked call, where 1 disables retry.
            retry_wait_initial: Initial exponential backoff in seconds.
            retry_wait_max: Cap on the exponential backoff in seconds.
            guard: Optional daily-spend guard. When wired, :meth:`retain` charges one
                call before the HTTP request, guarding the reindex cost burst
                (issue #16). None disables the cap.
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
        # The bank prefix is part of the per-call path, so the same client can also
        # DELETE the bank itself one segment up. No connection is made until the first
        # request, so constructing this on a bare checkout is free
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
        """Closes the underlying :class:`httpx.Client`, and is idempotent."""
        self._client.close()

    def __enter__(self) -> Hindsight:
        """Returns ``self`` for use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Closes the underlying client on context exit."""
        self.close()

    # ---- public surface ----------------------------------------------------------

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Retains a curated page's facts, with the vault path carried as provenance.

        ``document_id`` and ``context`` both carry the path, and ``tags`` carries the
        page type only. ``async`` is false, so the call blocks until the facts are
        extracted and indexed, and a non-2xx means the page did not land.

        Args:
            rel_path: Vault-relative path of the page being retained.
            facts: Curated fact text, with the ``SOURCE:`` line prepended for you.
            tags: Page-type tokens. ``rel_path`` is stripped out if present, since
                the path travels via ``document_id`` and ``context``.

        Raises:
            HindsightError: on a non-2xx, after the bounded retry.
            thoth.budget.BudgetExceededError: when a guard is wired and the daily cap
                is reached, raised before the HTTP call so no extraction is spent.
        """
        if self._guard is not None:
            # Charge before the HTTP call so a capped day defers the embedding rather
            # than spending it, which guards the reindex burst (issue #16)
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
        """Semantic recall, returning the vault paths recovered from each hit.

        The request is sent unfiltered and the page-type scope is applied client-side,
        because a tags filter would suppress untagged hits (ADR 0004). The scope runs
        before the limit cap. An empty result set is normal and returns [].

        Args:
            query: The natural-language recall query.
            limit: Maximum hits to return, applied after parsing.
            types: Keep only hits whose page type is in this set, or None for all.

        Returns:
            The de-duplicated hits, scoped and capped.

        Raises:
            HindsightError: on a non-2xx response.
        """
        response = self._request_checked(
            "recall", query, "POST", "/memories/recall", json={"query": query}
        )
        hits = parse_recall(_response_json(response))
        if types is not None:
            hits = [hit for hit in hits if hit.page_type in types]
        return hits[:limit]

    def forget(self, rel_path: str) -> None:
        """Best-effort per-document delete, which never raises.

        One DELETE with no retry and no status check. A failed forget must not abort or
        slow an ingest or reindex pass, so every error is swallowed.

        Args:
            rel_path: Vault-relative path whose document should be deleted.
        """
        try:
            self._client.request("DELETE", self._doc_path(rel_path))
        except httpx.HTTPError:
            # Best-effort, so swallow transport errors. Non-2xx statuses are ignored
            # too because raise_for_status is never called here
            pass

    def reset_bank(self) -> None:
        """Wipes the whole bank for ``reindex --full-rebuild``.

        A checked call with the same classification and bounded retry as :meth:`retain`.

        Raises:
            HindsightError: on a non-2xx response.
        """
        self._request_checked("reset_bank", self._bank, "DELETE", "")

    def probe(self, rel_path: str, query: str) -> bool:
        """Recalls ``query`` and reports whether ``rel_path`` is among the hits.

        This is the "did it land?" check the ingest retain pass runs after a
        :meth:`retain`.

        Args:
            rel_path: Vault-relative path expected to surface.
            query: The recall query to probe with.

        Returns:
            True when the path is among the hits, otherwise False.

        Raises:
            HindsightError: if the underlying recall fails on a non-2xx.
        """
        return any(hit.path == rel_path for hit in self.recall(query))

    # ---- internals ---------------------------------------------------------------

    def _bank_prefix(self) -> str:
        """Returns the URL-encoded ``/v1/default/banks/{bank}`` path prefix."""
        return f"/v1/default/banks/{quote(self._bank, safe='')}"

    def _doc_path(self, rel_path: str) -> str:
        """Returns the bank-relative ``/documents/{rel_path}`` path.

        The ``document_id`` is the vault path, so its separators are kept as path
        separators and only other reserved characters are encoded.
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
        """Issues a checked HTTP call with bounded retry on transient failures.

        An empty ``path`` addresses the bank itself, which is how ``reset_bank`` works.

        Args:
            op: Operation name for diagnostics.
            subject: The path or query the call concerns.
            method: The HTTP method.
            path: Bank-relative path, or "" for the bank itself.
            json: Optional JSON body.

        Returns:
            The successful 2xx response.

        Raises:
            HindsightError: the last failure once attempts are exhausted, or
                immediately when the failure is permanent.
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
        """Issues one HTTP call and classifies the outcome.

        Raises:
            HindsightTransientError: on a transport error or an HTTP 5xx.
            HindsightError: on an HTTP 4xx.
        """
        try:
            response = self._client.request(method, url, json=json)
        except httpx.TransportError as exc:
            # Connect, timeout, read, write and pool errors are all transient
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
        """Builds a diagnostic message carrying the op, subject, status and body."""
        return (
            f"hindsight {op} for {subject!r} failed "
            f"(HTTP {response.status_code}). body: {response.text.strip()!r}"
        )


def _response_json(response: httpx.Response) -> dict[str, object]:
    """Decodes a response body to a JSON dict."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
