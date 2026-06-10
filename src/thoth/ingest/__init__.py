"""The bounded-pass capture pipeline that files an inbound item into the vault.

This module is the orchestration core of capture (SPEC section 6). It runs a fixed,
ordered sequence of *validated passes* over one :class:`Capture` and never lets the
appliance LLM touch disk or the network directly: every byte that reaches the vault
goes through :class:`thoth.vault.Vault` (so paths are confined and the folder/type/slug
contract is enforced) and every web fetch goes through the SSRF-guarded
:class:`thoth.extract.Extractor`. git is a deterministic collaborator, never an LLM
tool. The passes are:

0. **orient** -- :meth:`thoth.git_sync.GitSync.pull` so writes land on current state.
0b. **persist inbound (durable hold)** -- :meth:`Ingestor.persist_inbound` extracts the
   inbound text/bytes (the only network step) and writes a durable ``inbox/`` holding
   page keyed on the body SHA-256 *before any LLM call*, so an Anthropic outage can
   never lose a capture (per issue #14 -- capture durability decoupled from the
   classify call; SPEC section 6 "pass 0b").
   If the later classify/curate cannot run because the LLM is unavailable, the held raw
   is committed and a *deferred-curation* report is returned for a later reindex/sweep;
   on success the now-superseded holding page is removed.
1. **classify** -- one cheap Claude call -> a :class:`Classification` whose ``type`` and
   ``slug`` are validated through :class:`~thoth.vault.Vault` before use.
2. **capture raw** -- :class:`~thoth.extract.Extractor` by kind (reusing the text
   already extracted in pass 0b, so the source is fetched once); the body SHA-256 is
   compared to any existing raw page's stored digest *before* writing, so an identical
   re-ingest is skipped and a changed body is flagged as drift (the idempotency rule).
   A binary (image/PDF) capture applies the same rule over the *bytes* SHA-256: an
   already-present asset with matching bytes is skipped, and a byte mismatch at the
   same slug is surfaced as drift rather than overwriting (SPEC step 2 'Skip if sha256
   exists'). A PDF additionally lands a ``raw/papers/<slug>.md`` page so the curate
   pass and retrieval have a searchable text body; full PDF text extraction is deferred
   to Phase 3, so the page records the provenance plus a pointer to the kept binary.
3. **fetch candidates** -- a read-only lexical scan for each named entity/concept.
4. **curate** -- a second Claude call returning a file-plan that is validated by
   :func:`thoth.llm.validate_file_plan` *and* re-validated through the
   :class:`~thoth.vault.Vault` write helpers, then written.
5. **navigation** -- :meth:`~thoth.vault.Vault.append_log` for every file touched (a
   reference page's one-line gloss rides in its own ``summary`` frontmatter, so there is
   no separate ``index.md`` catalog pass; ADR 0008).
6. **retain** -- :meth:`thoth.hindsight.Hindsight.retain` per curated page, then a
   ``probe`` that the page came back.
7. **commit** -- :meth:`~thoth.git_sync.GitSync.commit`; a rebase conflict is surfaced
   loudly (never ``--force``).
8. **report** -- a structured :class:`IngestReport` carrying the touched paths plus
   ``obsidian://`` links built by the *harness* (via
   :meth:`~thoth.vault.Vault.obsidian_uri`) so they cannot be fabricated by the model.

All collaborators (``vault``, ``llm``, ``extractor``, ``hindsight``, ``git``) are
injected, so a test substitutes fakes for every external boundary and a real
:class:`~thoth.vault.Vault` over a temporary vault. Only the standard library plus
``thoth.*`` are imported at module top level, so importing this module at pytest
collection is always safe (the heavy clients live behind the injected seams).
"""

from ._shared import _TEXT_EXTS as _TEXT_EXTS
from ._shared import _URL_EXCERPT_CHARS as _URL_EXCERPT_CHARS
from ._shared import (
    HOLD_MODE_AS_IS,
    HOLD_MODE_CURATE,
    HOLD_MODES,
    Capture,
    CaptureKind,
    Classification,
    IngestError,
    IngestReport,
    LLMUnavailableError,
    RawCaptureResult,
)
from ._shared import _ext_kind as _ext_kind
from .curate import _CURATE_ATTEMPTS as _CURATE_ATTEMPTS
from .pipeline import Ingestor

__all__ = [
    "HOLD_MODES",
    "HOLD_MODE_AS_IS",
    "HOLD_MODE_CURATE",
    "Capture",
    "CaptureKind",
    "Classification",
    "IngestError",
    "IngestReport",
    "Ingestor",
    "LLMUnavailableError",
    "RawCaptureResult",
]
