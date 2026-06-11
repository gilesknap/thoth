"""Content analysis of binary captures (images + PDFs) via a vision/document call.

This package is the **analyse seam** issue #42 adds to the capture pipeline. A binary
capture (an uploaded image or PDF) historically reached the classify/curate passes as a
single ``File: screenshot.png`` line -- the model never saw the file -- so every
attachment was filed blind into ``memories/`` with a boilerplate stub. The analyse pass
fixes that: it sends the *bytes* of the staged asset to a multimodal Claude model (an
image as a base64 ``image`` content block, a PDF as a base64 ``document`` block) and
returns the OCR'd / extracted text, a structured description/summary, and routing hints
(a suggested ``type`` plus named ``entities``/``concepts``) that drive both the classify
*routing* and the curate *body*.

Transient base64 vs SPEC section 6. SPEC section 6 forbids binary bytes ever travelling
*as base64* -- a **storage** rule: the vault never holds base64, and a byte-blob is
never the canonical form. Sending base64 to the vision API to *analyse* an image, while
the asset is still saved as a real binary file under ``raw/assets/`` and embedded with
``![[...]]``, is a deliberate amendment recorded in ADR 0006: the base64 is transient
(it lives only inside one request) and analysis-only (it enriches and routes; it is
never written or treated as the source of truth).

Cost + durability. The analyse call goes through the injected :class:`thoth.llm.LLM`, so
it is charged against the **same daily budget guard** as every other Anthropic call
(issue #16) and a cap-reached day raises :class:`thoth.budget.BudgetExceededError`
*before* the request -- which the ingest pass treats as a *deferral* (the raw asset is
already durable; a later sweep re-analyses it) rather than a lost capture, exactly like
the existing classify/curate deferral.

The :class:`Analyser` is injectable and the LLM client behind it is a fake in tests, so
the whole pass is unit-testable with **no real model call**: a test scripts the vision /
document JSON response (or injects a fake :class:`Analyser` directly).
"""

from __future__ import annotations

from .analyser import _EXCALIDRAW_MAX_TOKENS as _EXCALIDRAW_MAX_TOKENS
from .analyser import AnalyseError, Analyser, image_media_type
from .excalidraw_elements import _text_block_id as _text_block_id
from .prompts import _RESULT_SHAPE as _RESULT_SHAPE
from .result import Analysis

__all__ = [
    "Analyser",
    "AnalyseError",
    "Analysis",
    "image_media_type",
]
