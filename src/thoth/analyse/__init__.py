"""Content analysis of binary captures (images and PDFs) via a multimodal call.

This package is the **analyse seam** that issue #42 adds to the capture pipeline. A
binary capture, an uploaded image or PDF, historically reached the classify and curate
passes as a single ``File: screenshot.png`` line. The model never saw the file, so the
pipeline filed every attachment blind into ``memories/`` with a boilerplate stub.

The analyse pass fixes that. It sends the *bytes* of the staged asset to a multimodal
Claude model, an image as a base64 ``image`` content block and a PDF as a base64
``document`` block. It returns the OCR'd or extracted text, a structured description and
summary, and routing hints. The hints are a suggested ``type`` plus named ``entities``
and ``concepts``, and they drive both the classify *routing* and the curate *body*.

Transient base64 against SPEC section 6. SPEC section 6 forbids binary bytes ever
travelling *as base64*, which is a **storage** rule. The vault never holds base64, and a
byte-blob is never the canonical form. Sending base64 to the vision API to *analyse* an
image is a deliberate amendment recorded in ADR 0006. The asset is still saved as a real
binary file under ``raw/assets/`` and embedded with ``![[...]]``. The base64 is
transient, because it lives only inside one request, and it is analysis-only. It
enriches and routes, and nothing ever writes it or treats it as the source of truth.

Cost and durability. The analyse call goes through the injected :class:`thoth.llm.LLM`,
so the **same daily budget guard** charges it as every other Anthropic call (issue #16).
On a cap-reached day the guard raises :class:`thoth.budget.BudgetExceededError` *before*
the request. The ingest pass treats that as a *deferral* rather than a lost capture,
exactly like the existing classify and curate deferral. The raw asset is already
durable, and a later sweep re-analyses it.

The :class:`Analyser` is injectable and the LLM client behind it is a fake in tests, so
the whole pass is unit-testable with **no real model call**. A test scripts the vision
or document JSON response, or injects a fake :class:`Analyser` directly.
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
