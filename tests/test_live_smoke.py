"""Opt-in *live* first-light smoke suite -- one happy-path probe per real boundary.

CI mocks every external boundary (SPEC section 12), so these seams are unverified until
the appliance first runs against the real services on the VPS. This module is the
executable companion to ``docs/how-to/first-light.md``: one happy-path test per boundary
(Anthropic, Hindsight, Slack, MCP, Firecrawl, the cron entrypoints), each driving
the *real* ``thoth`` code path against the *real* service.

It is **opt-in and skipped offline**. The whole module is guarded by a module-level
``pytestmark`` that skips unless ``THOTH_LIVE_SMOKE=1``, and every test carries the
registered ``live`` marker (declared in ``pyproject.toml`` so no
``PytestUnknownMarkWarning`` is raised). Run them on the VPS with::

    THOTH_LIVE_SMOKE=1 uv run pytest -m live

Import safety (the pytest-collection trap): the heavy/absent runtime clients
(``anthropic`` / ``slack_bolt`` / ``mcp`` / ``firecrawl``) are **never** imported at
this module's top level. Each is pulled in **lazily inside its test function** -- either
directly or (the common case) by the ``thoth`` entry point the test calls, which already
imports its client lazily. So this module imports cleanly at collection on a bare CI
checkout where those libraries are absent, and every test is collected-then-skipped.

No fixture and no top-level statement here performs network, subprocess, or service I/O:
all real I/O happens *inside* a test body, and every test body is skipped unless the
live flag is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from thoth.config import Config, load_config

if TYPE_CHECKING:
    # Pure dataclass, import-safe, but imported lazily at runtime (inside the
    # test/helper bodies) to keep the module's runtime imports minimal; pulled in here
    # only so the ``_real_tool_context`` return annotation resolves for the type
    # checker.
    from thoth.mcp_server import ToolContext

# --- the opt-in gate ----------------------------------------------------------------
# Skip the ENTIRE module unless explicitly enabled. Offline (CI, dev box) the tests are
# collected but skipped, so the suite stays import-safe and green without the real
# services. ``live`` is registered in pyproject [tool.pytest.ini_options] markers.
LIVE_SMOKE_ENABLED = os.environ.get("THOTH_LIVE_SMOKE") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LIVE_SMOKE_ENABLED,
        reason="live smoke tests run only when THOTH_LIVE_SMOKE=1 (VPS first-light)",
    ),
]


@pytest.fixture
def live_config() -> Config:
    """The real runtime config from the environment / ``~/.thoth/.env`` (VPS only).

    Reached only when the live gate is open, so reading the real environment here is
    intended. ``PKM_VAULT`` is required; the rest of the keys are validated per-boundary
    by the individual tests (each ``require_*`` / lazy client raises a clear error).
    """
    return load_config()


# --------------------------------------------------------------------------------------
# 1. Anthropic -- a trivial classify returns valid JSON (the cheapest real LLM call)
# --------------------------------------------------------------------------------------


def test_live_anthropic_classify_returns_valid_json(live_config: Config) -> None:
    """A trivial ingest classify round-trips to Claude and returns a valid routing JSON.

    Exercises the real Anthropic boundary end to end: ``LLM.complete`` (which imports
    ``anthropic`` lazily) drives :meth:`thoth.ingest.Ingestor.classify`, whose output is
    validated as a :class:`~thoth.ingest.Classification` (a non-empty ``type`` and
    ``slug``). A failure here means the model id / key / prompt is wrong.
    """
    from thoth.extract import Extractor
    from thoth.git_sync import GitSync
    from thoth.hindsight import Hindsight
    from thoth.ingest import Capture, Ingestor
    from thoth.llm import LLM
    from thoth.vault import Vault

    live_config.require_anthropic()  # fail fast + clearly if ANTHROPIC_API_KEY is unset

    # Build a minimal Ingestor whose only exercised collaborator is the real LLM; the
    # other seams are unused by classify, so an unused-sentinel stands in for each.
    llm = LLM(live_config)
    ingestor = Ingestor(
        config=live_config,
        vault=cast(Vault, _unused("vault")),
        llm=llm,
        extractor=cast(Extractor, _unused("extractor")),
        hindsight=cast(Hindsight, _unused("hindsight")),
        git=cast(GitSync, _unused("git")),
    )

    capture = Capture(
        text="Remember to renew the car insurance next week.", source="mcp"
    )
    classification = ingestor.classify(capture)

    assert classification.page_type, "classify returned an empty page type"
    assert classification.slug, "classify returned an empty slug"


def test_live_curate_repair_turn_round_trips_after_tool_use_rejection(
    live_config: Config,
) -> None:
    """The curate repair retry round-trips on the REAL Messages API after a rejection.

    Regression guard for #110's repair-loop fix. The curate pass FORCES the
    ``submit_file_plan`` tool, so a rejected plan must be re-asked with a user turn that
    LEADS with a ``tool_result`` block keyed to the prior ``tool_use`` id -- a plain
    text user turn there makes the live Messages API reject the request with HTTP 400
    ("tool_use ids were found without tool_result blocks immediately after"). Injected
    fakes ignore that precondition, so only a real round-trip proves the shape.

    This forces exactly ONE validation failure: ``_parse_and_validate_plan`` is wrapped
    to reject the first (real) tool_use plan and then delegate to the genuine validator
    on the second attempt. So curate builds the ``tool_result`` repair turn and sends it
    to the real API; the test passes only if that second call is accepted and returns a
    validated plan. The vault write is stubbed (the boundary under test is the LLM
    repair turn, not the filesystem), and no budget guard is wired so two real calls
    are allowed.
    """
    from thoth.extract import Extractor
    from thoth.git_sync import GitSync
    from thoth.hindsight import Hindsight
    from thoth.ingest import (
        Capture,
        Classification,
        IngestError,
        Ingestor,
        RawCaptureResult,
    )
    from thoth.llm import LLM
    from thoth.vault import Vault

    live_config.require_anthropic()  # fail fast + clearly if ANTHROPIC_API_KEY is unset

    # A real LLM (no budget guard, so the repair attempt's second call is allowed);
    # every other collaborator is unused by this path except the stubbed page writer.
    llm = LLM(live_config)
    ingestor = Ingestor(
        config=live_config,
        vault=cast(Vault, _unused("vault")),
        llm=llm,
        extractor=cast(Extractor, _unused("extractor")),
        hindsight=cast(Hindsight, _unused("hindsight")),
        git=cast(GitSync, _unused("git")),
    )

    # Force exactly one validation failure: reject the first parsed plan (so curate
    # emits the tool_result repair turn to the live API), then pass the second through
    # the real validator.
    original_validate = ingestor._parse_and_validate_plan  # noqa: SLF001
    attempts = {"n": 0}

    def _reject_once(response: object) -> dict[str, object]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise IngestError(
                "pages[0]: forced validation failure (live repair-turn probe)"
            )
        return original_validate(response)

    ingestor._parse_and_validate_plan = _reject_once  # type: ignore[method-assign]  # noqa: SLF001
    # The boundary under test is the LLM repair turn, not the vault write.
    ingestor._write_planned_page = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda page, source, raw, *, analysis=None: "notes/live-repair-probe.md"
    )

    capture = Capture(
        text=(
            "Remote login to ArgoCD: set up a VPN, run `ec-login`, then ctrl-click the "
            "shown URL and complete SSO in the browser."
        ),
        source="mcp",
    )
    cls = Classification(
        page_type="note",
        slug="live-repair-probe",
        title="Remote Login to ArgoCD",
    )
    raw = RawCaptureResult(raw_path=None, disposition="none")

    plan = ingestor.curate(capture, cls, raw, [])

    assert attempts["n"] == 2, (
        "expected exactly one repair retry (first plan rejected, second accepted); "
        f"saw {attempts['n']} validation attempt(s)"
    )
    assert plan.get("pages"), (
        "curate did not return a validated plan after the tool_result repair turn -- "
        "the live Messages API likely rejected the repair message shape (#110)"
    )


# --------------------------------------------------------------------------------------
# 2. Hindsight -- retain/recall round-trip + page-type provenance over the real HTTP API
# --------------------------------------------------------------------------------------


def test_live_hindsight_retain_recall_roundtrip(live_config: Config) -> None:
    """``retain`` then ``recall`` round-trips and recall recovers the vault path (#7).

    Drives the real standalone ``hindsight-api`` HTTP server through
    :class:`thoth.hindsight.Hindsight` (base URL from ``THOTH_HINDSIGHT_BASE_URL``):
    retains a uniquely-tagged probe fact (the vault path carried as ``document_id`` /
    ``context``), then recalls it and asserts the path is recovered via
    :func:`thoth.hindsight.parse_recall`. This is the executable proof that recall
    provenance round-trips over the HTTP contract -- the central risk #157 cannot verify
    under mocked CI. The bank is env-overridable (``THOTH_HINDSIGHT_BANK``) for the VPS
    surface. ``forget`` cleans the probe doc out of the bank afterwards.
    """
    import uuid

    from thoth.hindsight import Hindsight

    hindsight = Hindsight(live_config)
    # A unique token so this probe cannot collide with real bank contents.
    token = uuid.uuid4().hex
    rel_path = f"notes/first-light-{token}.md"
    query = f"first light smoke probe {token}"

    try:
        hindsight.retain(rel_path, f"A first-light smoke probe fact tagged {token}.")
        hits = hindsight.recall(query)

        assert any(hit.path == rel_path for hit in hits), (
            f"recall did not recover the vault path {rel_path!r} from hits "
            f"{[hit.path for hit in hits]!r}"
        )
    finally:
        # Per-document delete leaves the live bank clean (best-effort, never raises).
        hindsight.forget(rel_path)
        hindsight.close()


def test_live_hindsight_recall_scopes_by_page_type_tag(live_config: Config) -> None:
    """The ``page_type`` tag round-trips through recall and scopes results (ADR 0004).

    Issue #40 indexes all content and partitions recall **by the page-type tag at
    query time**. CI mocks the boundary, so the live risk is whether the real
    ``hindsight-api`` server round-trips that tag back in the recall JSON (as
    ``tags`` on
    retain, recovered by :func:`thoth.hindsight.parse_recall`). This retains a
    uniquely-tagged ``entity`` probe, recalls it, and asserts the recovered
    :attr:`~thoth.hindsight.RecallHit.page_type` is ``entity`` -- then that a
    reference-type scope keeps the hit while an actionable-only scope filters it out
    (ADR 0005, filtered client-side). ``forget`` cleans the probe afterwards.
    """
    import uuid

    from thoth.hindsight import Hindsight
    from thoth.vault import REFERENCE_TYPES

    hindsight = Hindsight(live_config)
    token = uuid.uuid4().hex
    rel_path = f"entities/scope-probe-{token}.md"
    # Keep the query lexically close to the fact so the probe reliably surfaces among
    # real bank content (recall is semantic + token-bounded), as the path round-trip
    # test above does -- this test is about the tag round-trip, not recall ranking.
    query = f"tag scope probe entity fact {token}"

    try:
        hindsight.retain(
            rel_path, f"A tag scope probe entity fact tagged {token}.", tags=["entity"]
        )

        match = next((h for h in hindsight.recall(query) if h.path == rel_path), None)
        assert match is not None, f"recall did not recover {rel_path!r}"
        assert match.page_type == "entity", (
            f"page_type tag did not round-trip; got {match.page_type!r}"
        )

        # Reference scope keeps the entity hit; an actionable-only scope filters it out.
        reference = hindsight.recall(query, types=REFERENCE_TYPES)
        assert any(h.path == rel_path for h in reference), (
            "reference scope dropped a hit"
        )
        actionable = hindsight.recall(query, types=frozenset({"action"}))
        assert all(h.path != rel_path for h in actionable), (
            "actionable scope wrongly kept an entity hit"
        )
    finally:
        hindsight.forget(rel_path)
        hindsight.close()


def test_live_hindsight_reset_bank_wipes_the_bank(live_config: Config) -> None:
    """``reset_bank`` DELETEs the whole bank over the real ``hindsight-api`` server.

    This is the HTTP wipe behind ``reindex --full-rebuild``: retain a uniquely-tagged
    probe, prove it is recallable, then ``reset_bank`` and assert the probe no longer
    surfaces. CI mocks the boundary, so only the live server proves the bank DELETE is
    honoured. **Destructive: empties the configured bank**, so it runs only under the
    live gate against a throwaway/test bank (override ``THOTH_HINDSIGHT_BANK`` on the
    box if the default bank holds content the owner cares about).
    """
    import uuid

    from thoth.hindsight import Hindsight

    hindsight = Hindsight(live_config)
    token = uuid.uuid4().hex
    rel_path = f"notes/reset-probe-{token}.md"
    query = f"reset bank wipe probe {token}"

    try:
        hindsight.retain(rel_path, f"A reset-bank wipe probe fact tagged {token}.")
        hits = hindsight.recall(query)
        assert any(hit.path == rel_path for hit in hits), (
            f"precondition failed: probe {rel_path!r} not recallable before reset"
        )

        hindsight.reset_bank()

        after = hindsight.recall(query)
        assert all(hit.path != rel_path for hit in after), (
            f"reset_bank did not wipe the bank; {rel_path!r} still recallable in "
            f"{[hit.path for hit in after]!r}"
        )
    finally:
        hindsight.close()


# --------------------------------------------------------------------------------------
# 3. Slack -- Socket Mode credentials authenticate and the app wires up
# --------------------------------------------------------------------------------------


def test_live_slack_app_builds_and_auth_succeeds(live_config: Config) -> None:
    """``build_app`` constructs the real Slack app and ``auth.test`` succeeds.

    Imports ``slack_bolt`` lazily (inside :func:`thoth.slack_app.build_app`), builds the
    fully-wired app from the real bot token, then calls ``auth.test`` over the web
    client to prove the credentials authenticate. This is the precondition for Socket
    Mode + the DM round-trip in the docs checklist (which needs a human in the loop, so
    it stays manual). The collaborators the *listeners* would use are unused by
    ``auth.test`` here.
    """
    from thoth.ingest import Ingestor
    from thoth.query import QueryEngine
    from thoth.slack_app import build_app

    live_config.require_slack()  # fail fast if SLACK_BOT_TOKEN / SLACK_APP_TOKEN unset

    app = build_app(
        live_config,
        ingestor=cast(Ingestor, _unused("ingestor")),
        query_engine=cast(QueryEngine, _unused("query_engine")),
    )
    response = app.client.auth_test()
    assert response.get("ok") is True, f"Slack auth.test failed: {response!r}"


# --------------------------------------------------------------------------------------
# 4. MCP -- the pkm_* tools list (one server build over the real FastMCP)
# --------------------------------------------------------------------------------------


def test_live_mcp_server_lists_pkm_tools(live_config: Config, tmp_path: Path) -> None:
    """``build_server`` registers exactly the five ``pkm_*`` tools on a real FastMCP.

    Imports ``mcp`` lazily (inside :func:`thoth.mcp_server.build_server`) and builds the
    real FastMCP server over a real (throwaway ``tmp_path``) vault, asserting the
    registered tool set equals :data:`thoth.mcp_server.TOOL_NAMES`. Listing the tools
    is the stdio-handshake the checklist verifies with an external client; executing one
    over live stdio needs a client process, so that stays manual. No stdio loop is
    started here.
    """
    from thoth.mcp_server import TOOL_NAMES, build_server

    vault_config = _seed_real_vault(tmp_path)
    ctx = _real_tool_context(vault_config)
    server = build_server(ctx)

    # FastMCP exposes registered tools via list_tools(); fall back to the internal tool
    # manager only if the API shape differs on the installed version (VPS-time).
    tool_names = _registered_tool_names(server)
    assert set(tool_names) == set(TOOL_NAMES), (
        f"registered tools {sorted(tool_names)!r} != expected {sorted(TOOL_NAMES)!r}"
    )


# --------------------------------------------------------------------------------------
# 5. Firecrawl -- one real extract returns non-empty markdown
# --------------------------------------------------------------------------------------


def test_live_firecrawl_extract_returns_markdown(live_config: Config) -> None:
    """A single real Firecrawl extract of a stable URL returns non-empty markdown.

    Imports the Firecrawl SDK lazily (inside
    :attr:`thoth.extract.Extractor.firecrawl`), extracts a stable public page through
    the SSRF-guarded :meth:`Extractor.web_extract`, and asserts non-empty markdown. A
    missing-key error means ``FIRECRAWL_API_KEY`` is unset.
    """
    from thoth.extract import Extractor

    extractor = Extractor(live_config)
    doc = extractor.web_extract("https://example.com/")
    assert doc.markdown.strip(), "Firecrawl extract returned empty markdown"


# --------------------------------------------------------------------------------------
# 6. Cron entrypoints -- an incremental reindex runs and records its marker
# --------------------------------------------------------------------------------------


def test_live_reindex_incremental_runs(live_config: Config) -> None:
    """An incremental ``reindex`` runs against the real vault + Hindsight and succeeds.

    Drives the ``thoth reindex`` entrypoint body (:meth:`thoth.reindex_from_vault.
    Reindexer.run`) incrementally over the real vault; unchanged pages are skipped via
    the body-``sha256`` manifest, so on a quiet vault this is near-zero work. Proves the
    06:30 cron entrypoint and the Hindsight retain path are healthy together. The
    summary cron post needs a live Slack channel and is verified in the manual
    checklist.
    """
    from thoth.hindsight import Hindsight
    from thoth.reindex_from_vault import Reindexer
    from thoth.vault import Vault

    hindsight = Hindsight(live_config)
    reindexer = Reindexer(
        config=live_config,
        vault=Vault(live_config),
        hindsight=hindsight,
    )
    try:
        # changed + skipped cover every page scanned; non-negative counts = success.
        result = reindexer.run(full_rebuild=False)
        assert result.changed >= 0
        assert result.skipped >= 0
    finally:
        hindsight.close()


# --------------------------------------------------------------------------------------
# 7. Budget guard -- a real Anthropic call is charged, the next is blocked (issue #16)
# --------------------------------------------------------------------------------------


class _RecordingAlerter:
    """Captures the one-per-day budget alert without touching Slack (issue #16)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def alert_budget_exceeded(
        self, *, day: str, limit: int, breakdown: dict[str, int]
    ) -> bool:
        """Record the cap-reached alert and report success."""
        self.calls.append((day, limit))
        return True


def test_live_budget_guard_blocks_real_anthropic_call(
    live_config: Config, tmp_path: Path
) -> None:
    """The daily guard charges a *real* Anthropic call, then blocks the next (#16).

    The single live risk the unit tests cannot cover is that wrapping the real Anthropic
    SDK call with the budget charge still works end to end. With a budget of 1 over a
    throwaway state DB (``thoth_home`` redirected into ``tmp_path``), the first
    ``LLM.complete`` reaches Claude and returns text; the second is blocked *before* the
    SDK is touched with :class:`~thoth.budget.BudgetExceededError`, and exactly one
    cap-reached alert is emitted. The real ``~/.thoth/state.db`` is never touched.

    The Hindsight (Gemini) chokepoint is exercised without spending: a guard pre-charged
    to its cap makes ``retain`` raise before the HTTP call is ever issued, so no bank is
    touched -- proof of the wiring with zero side effects.
    """
    import dataclasses

    from thoth.budget import (
        KIND_HINDSIGHT,
        BudgetExceededError,
        make_budget_guard,
    )
    from thoth.hindsight import Hindsight
    from thoth.llm import LLM, Message, extract_text

    live_config.require_anthropic()  # fail fast if ANTHROPIC_API_KEY is unset

    # Budget of 1 over a throwaway state DB (thoth_home -> tmp_path); real keys intact.
    budget_config = dataclasses.replace(
        live_config, daily_llm_budget=1, thoth_home=tmp_path
    )
    alerter = _RecordingAlerter()
    guard = make_budget_guard(budget_config, alerter=alerter)
    llm = LLM(budget_config, guard=guard)

    response = llm.complete(
        [Message(role="user", content="Reply with the single word OK.")],
        max_tokens=16,
    )
    assert extract_text(response).strip(), "the real Anthropic call returned no text"

    with pytest.raises(BudgetExceededError):
        llm.complete([Message(role="user", content="this must be blocked")])
    assert len(alerter.calls) == 1, "the cap-reached alert must fire exactly once"

    # Hindsight chokepoint: a pre-exhausted guard blocks retain before any HTTP call, so
    # no Gemini extraction is spent and no bank is touched.
    hs_guard = make_budget_guard(
        dataclasses.replace(live_config, daily_llm_budget=1, thoth_home=tmp_path / "hs")
    )
    hs_guard.charge(KIND_HINDSIGHT)  # exhaust the single-call budget
    hindsight = Hindsight(live_config, guard=hs_guard)
    try:
        with pytest.raises(BudgetExceededError):
            hindsight.retain("notes/never-spent.md", "this must be blocked")
    finally:
        hindsight.close()


# --------------------------------------------------------------------------------------
# helpers (only ever reached when the live gate is open)
# --------------------------------------------------------------------------------------


_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "notes",
    "memories",
    "actions",
    "inbox",
)

_INDEX_SEED = """\
---
title: Home
type: summary
created: 2026-05-30
updated: 2026-05-30
source: manual
tags: [home]
---

# Home

## Knowledge catalog
"""

_LOG_SEED = """\
# Vault Log

## [2026-05-30] create | Vault initialized
- structure seeded
"""


def _seed_real_vault(root: Path) -> Config:
    """Seed a throwaway vault skeleton under ``root`` and return a Config for it."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (root / "log.md").write_text(_LOG_SEED, encoding="utf-8")
    return load_config({"PKM_VAULT": str(root)})


def _real_tool_context(config: Config) -> ToolContext:
    """Build a :class:`ToolContext` whose collaborators are unused by ``build_server``.

    ``build_server`` only registers tool callables; it does not invoke them, so the
    ingestor/query seams need not be real for the tools/list smoke. The vault is the
    real one over the seeded tmp vault.
    """
    from thoth.git_sync import GitSync
    from thoth.ingest import Ingestor
    from thoth.mcp_server import ToolContext
    from thoth.query import QueryEngine
    from thoth.vault import Vault

    return ToolContext(
        config=config,
        vault=Vault(config),
        ingestor=cast(Ingestor, _unused("ingestor")),
        query_engine=cast(QueryEngine, _unused("query_engine")),
        git=cast(GitSync, _unused("git")),
    )


def _registered_tool_names(server: object) -> list[str]:
    """Return the tool names registered on a built FastMCP server, API-shape tolerant.

    Tries the documented ``list_tools()`` coroutine result first; if the installed
    FastMCP exposes the registry differently, falls back to the internal tool manager.
    Kept defensive because the exact FastMCP version is VPS-time.
    """
    import asyncio

    lister = getattr(server, "list_tools", None)
    if lister is not None:
        tools = asyncio.run(lister())  # type: ignore[misc]
        return [getattr(tool, "name", "") for tool in tools]
    manager = getattr(server, "_tool_manager", None)
    if manager is not None:
        return list(getattr(manager, "_tools", {}).keys())
    raise AssertionError("could not enumerate FastMCP tools on this build")


def _unused(name: str) -> object:
    """A placeholder collaborator that errors loudly if a skipped seam is ever touched.

    The per-boundary live tests each exercise exactly one real seam; the other
    collaborators are structurally required by a constructor but never called on the
    happy path. This sentinel makes any accidental use fail with a clear message rather
    than a confusing ``AttributeError``.
    """

    class _Unused:
        def __getattr__(self, attr: str) -> object:
            raise AssertionError(
                f"the {name!r} collaborator is not exercised by this live smoke test "
                f"but its {attr!r} was accessed"
            )

    return _Unused()
