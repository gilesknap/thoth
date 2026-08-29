"""The bounded-pass capture pipeline that files an inbound item into the vault.

This module is the orchestration core of capture (SPEC section 6). It runs a fixed,
ordered sequence of *validated passes* over one :class:`Capture` and never lets the
appliance LLM touch disk or the network directly. Every byte that reaches the vault
goes through :class:`thoth.vault.Vault`, which confines paths and enforces the folder,
type and slug contract, and every web fetch goes through the SSRF-guarded
:class:`thoth.extract.Extractor`. git is a deterministic collaborator, never an LLM
tool. The passes are:

0. **orient**. :meth:`thoth.git_sync.GitSync.pull` runs so writes land on current
   state.
0b. **persist inbound, a durable hold**. :meth:`Ingestor.persist_inbound` extracts the
   inbound text or bytes, the only network step, and writes a durable ``inbox/``
   holding page keyed on the body SHA-256 *before any LLM call*, so an Anthropic outage
   can never lose a capture. Issue #14 decoupled capture durability from the classify
   call, as SPEC section 6 "pass 0b" records. When the later classify or curate cannot
   run because the LLM is unavailable, the held raw is committed and a
   *deferred-curation* report returns for a later reindex or sweep. On success the
   now-superseded holding page is removed.
1. **classify**. One cheap Claude call returns a :class:`Classification` whose ``type``
   and ``slug`` are validated through :class:`~thoth.vault.Vault` before use.
2. **capture raw**. :class:`~thoth.extract.Extractor` runs by kind, reusing the text
   already extracted in pass 0b so the source is fetched once. The body SHA-256 is
   compared to any existing raw page's stored digest *before* writing, so an identical
   re-ingest is skipped and a changed body is flagged as drift, the idempotency rule. A
   binary image or PDF capture applies the same rule over the *bytes* SHA-256: an
   already-present asset with matching bytes is skipped, and a byte mismatch at the
   same slug surfaces as drift rather than overwrites (SPEC step 2, 'Skip if sha256
   exists'). A PDF additionally lands a ``raw/papers/<slug>.md`` page, so the curate
   pass and retrieval have a searchable text body. Full PDF text extraction is deferred
   to Phase 3, so that page records the provenance plus a pointer to the kept binary.
3. **fetch candidates**. A read-only lexical scan for each named entity and concept.
4. **curate**. A second Claude call returns a file-plan that
   :func:`thoth.llm.validate_file_plan` validates *and* the
   :class:`~thoth.vault.Vault` write helpers re-validate, before it is written.
5. **navigation**. :meth:`~thoth.vault.Vault.append_log` runs for every file touched. A
   reference page's one-line gloss rides in its own ``summary`` frontmatter, so there
   is no separate ``index.md`` catalog pass (ADR 0008).
6. **retain**. :meth:`thoth.hindsight.Hindsight.retain` runs per curated page, then a
   ``probe`` that the page came back.
7. **commit**. :meth:`~thoth.git_sync.GitSync.commit` runs, and a rebase conflict
   surfaces loudly, never with ``--force``.
8. **report**. A structured :class:`IngestReport` carries the touched paths plus
   ``obsidian://`` links the *harness* built through
   :meth:`~thoth.vault.Vault.obsidian_uri`, so the model cannot fabricate them.

Every collaborator is injected, ``vault``, ``llm``, ``extractor``, ``hindsight`` and
``git``, so a test substitutes fakes for every external boundary and a real
:class:`~thoth.vault.Vault` over a temporary vault. Module top level imports only the
standard library and ``thoth.*``, so importing this module at pytest collection is
always safe, the heavy clients living behind the injected seams.
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
