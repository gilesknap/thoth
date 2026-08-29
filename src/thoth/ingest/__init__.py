"""The bounded-pass capture pipeline that files an inbound item into the vault.

This module is the orchestration core of capture (SPEC section 6). It runs a fixed,
ordered sequence of validated passes over one :class:`Capture` and never lets the
appliance LLM touch disk or the network directly: every byte that reaches the vault goes
through :class:`thoth.vault.Vault`, so paths are confined and the folder/type/slug
contract is enforced, and every web fetch goes through the SSRF-guarded
:class:`thoth.extract.Extractor`. git is a deterministic collaborator, never an LLM
tool.

0.  **orient** -- pull, so writes land on current state.
0b. **persist inbound** -- extract the text or bytes, the only network step, and write a
    durable ``inbox/`` holding page keyed on the body SHA-256 before any LLM call, so an
    Anthropic outage can never lose a capture (issue #14). If classify and curate then
    cannot run, the held raw is committed and a deferred-curation report is returned for
    a later sweep; on success the superseded hold is removed.
1.  **classify** -- one cheap Claude call, whose ``type`` and ``slug`` are validated
    through the vault before use.
2.  **capture raw** -- extract by kind, reusing pass 0b's text so the source is fetched
    once, and compare the body SHA-256 to any existing raw page's digest before writing,
    so an identical re-ingest is skipped and a changed body is flagged as drift. A
    binary applies the same rule over the bytes digest. A PDF also lands a
    ``raw/papers/<slug>.md`` page so curate and retrieval have a searchable body.
3.  **fetch candidates** -- a read-only lexical scan per named entity or concept.
4.  **curate** -- a second Claude call, whose file-plan is validated by
    :func:`thoth.llm.validate_file_plan` and re-validated through the vault write
    helpers before it is written.
5.  **navigation** -- append to the log for every file touched. A reference page's gloss
    rides in its own ``summary`` frontmatter, so there is no separate ``index.md``
    catalog pass (ADR 0008).
6.  **retain** -- retain each curated page in Hindsight, then probe that it came back.
7.  **commit** -- surface a rebase conflict loudly, and never force.
8.  **report** -- an :class:`IngestReport` of the touched paths plus ``obsidian://``
    links built by the harness, so they cannot be fabricated by the model.

Every collaborator is injected, so a test substitutes fakes for each external boundary
and a real :class:`~thoth.vault.Vault` over a temporary vault. Only the standard library
plus ``thoth.*`` is imported at module level, so importing this at pytest collection is
always safe and the heavy clients live behind the injected seams.
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
