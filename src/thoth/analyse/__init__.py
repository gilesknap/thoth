"""Content analysis of binary captures (images and PDFs) via a vision/document call.

The analyse seam from issue #42. Before it a binary capture reached classify and curate
as a single ``File: screenshot.png`` line, so every attachment was filed blind into
``memories/`` with a boilerplate stub. This pass sends the staged asset's bytes to a
multimodal Claude model and returns the extracted text, a description, and routing hints
that drive both the classify routing and the curate body.

SPEC section 6 forbids binary bytes travelling as base64, which is a storage rule: the
vault never holds base64 and a byte-blob is never the canonical form. ADR 0006 amends it
for this pass, where the base64 is transient and analysis-only while the asset itself is
saved as a real binary under ``raw/assets/``.

The call goes through the injected :class:`thoth.llm.LLM`, so it is charged against the
same daily budget as every other Anthropic call (issue #16) and a cap-reached day raises
:class:`thoth.budget.BudgetExceededError` before the request. Ingest treats that as a
deferral rather than a lost capture: the raw asset is already durable and a later sweep
re-analyses it.

The :class:`Analyser` is injectable and the client behind it is a fake in tests, so the
whole pass is unit-testable with no real model call.
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
