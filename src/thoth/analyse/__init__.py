"""Content analysis of binary captures, images and PDFs, via a multimodal call.

The **analyse seam** issue #42 adds to the capture pipeline. It sends the staged asset's
*bytes* to a multimodal Claude model, an image as a base64 ``image`` block and a PDF as
a base64 ``document`` block. It returns the extracted text, a description and summary,
and routing hints: a suggested ``type`` plus named ``entities`` and ``concepts``. Those
hints drive both the classify *routing* and the curate *body*. Before it existed, a
binary capture reached classify and curate as a bare ``File: screenshot.png`` line, so
every attachment was filed blind into ``memories/`` with a boilerplate stub.

Transient base64 against SPEC section 6. That section forbids binary bytes travelling
*as base64*, which is a **storage** rule: the vault never holds base64 and a byte-blob
is never canonical. ADR 0006 records the deliberate amendment for analysis. The asset is
still saved as a real binary under ``raw/assets/`` and embedded with ``![[...]]``, and
the base64 lives only inside one request. It enriches and routes, and nothing writes it
or treats it as the source of truth.

Cost and durability. The call goes through the injected :class:`thoth.llm.LLM`, so the
**same daily budget guard** charges it as every other Anthropic call (issue #16). A
cap-reached day raises :class:`thoth.budget.BudgetExceededError` *before* the request,
which the ingest pass defers exactly like a classify or curate trip. The raw asset is
already durable and a later sweep re-analyses it, so nothing is lost.

The :class:`Analyser` is injectable and its LLM client is a fake in tests, so the whole
pass is unit-testable with **no real model call**. A test scripts the JSON response, or
injects a fake :class:`Analyser`.
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
