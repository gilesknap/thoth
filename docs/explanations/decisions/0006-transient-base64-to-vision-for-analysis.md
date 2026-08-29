# 6. Transient base64 to the vision/document API for analysis

Date: 2026-05-31

## Status

Accepted

## Context

The capture pipeline (`thoth.ingest`) historically filed a binary capture, an uploaded image or PDF, blind. The model never saw the file, because `Ingestor._capture_summary` rendered only a single `File: screenshot.png` line, and that line was all the classify and curate passes ever received.

The consequences were confirmed in code and live on 2026-05-31:

- `classify` had no content to route on, so it defaulted `type` to `memory` and every image and PDF landed in `memories/`.
- `curate` wrote a boilerplate stub, "an image captured and stored…", around an `![[asset]]` embed, so nothing about the asset's actual content was searchable.

Issue #42 adds an analyse seam (`thoth.analyse.Analyser`) that OCRs or vision-analyses an image and reads a PDF natively, then feeds the extracted text, a suggested type and named entities and concepts into both `classify` for routing and `curate` for the body.

To do that, the asset's bytes must reach a multimodal Claude model. The Anthropic Messages API takes image bytes as a base64 `image` content block and PDF bytes as a base64 `document` content block, and there is no by-reference alternative for a server-held binary.

This appears to collide with the rule baked into `Capture` and the persona:

> Binary bytes never travel as base64 (SPEC §6). … Never store base64.

But that rule is about storage and canonical form rather than transport for analysis. Its intent is twofold and unchanged here:

1. The vault never holds base64. An asset is a real binary file under `raw/assets/`, embedded with `![[…]]`, and we never write a base64 blob into a markdown page or a descriptive sidecar.
2. A byte-blob is never the *source of truth*. The canonical artefact is the stored binary, and the curated page links to it.

Sending the same bytes as base64 to the vision API to analyse them, while the asset is still saved as a real binary and embedded, is consistent with that intent. The base64 is transient, existing only inside one outbound request and never persisted, and it is analysis-only, enriching the body and driving routing while never being written back or treated as canonical.

It is nonetheless a deliberate amendment to the literal "binary bytes never travel as base64" wording, so it is recorded here rather than left implicit.

## Decision

**Permit transient base64 encoding of a binary capture's bytes for the sole purpose of sending them to the vision or document API in the analyse pass (issue #42). The storage rule is unchanged: the vault never holds base64, and the stored binary remains canonical.**

Concretely:

- `thoth.analyse.Analyser.analyse_image` and `analyse_pdf` base64-encode the bytes into a vision `image` or `document` content block, send one Claude call, and return the parsed `Analysis`. The base64 lives only in that request.
- The asset continues to be saved as a real binary under `raw/assets/`, idempotent on its bytes SHA-256, and embedded with `![[…]]`. The analysis only enriches the curated body and routes the capture, and no base64 is ever written to a vault file.
- The `Capture` docstring is tightened to scope the SPEC §6 rule to storage and canonical form, and to point at this ADR for the transient-analysis amendment.
- The analyse call goes through the injected `thoth.llm.LLM`, so it is charged against the same daily budget guard as every other Anthropic call (the ADR-adjacent issue #16). That is one heavier vision or document call per binary capture, and on a cap-reached day it raises `BudgetExceededError` *before* the request.

## Consequences

- A binary capture is now routed by its content, so a whiteboard photo goes to `notes/` and a receipt becomes an `action`, instead of always defaulting to `memories/`. The curated page body holds the real OCR'd or extracted text, which is searchable, clusterable and cross-linked, rather than a blind stub.
- Durability is preserved by reusing the existing decoupled-durability and deferral pattern. The raw asset and the `inbox/` hold are persisted *before* any model call, so if the analyse call is unavailable through a transport failure, or the daily budget cap is reached, the capture defers with the raw held and is re-analysed on a later sweep rather than being lost, exactly like the classify and curate deferral. An unparseable analysis is non-fatal, so the binary is filed blind as before and never aborted.
- The storage invariant is untouched and still enforced. `Vault.save_asset` writes real bytes, `![[…]]` embeds reference the bare filename, and no code path writes base64 into a page. The amendment is strictly about a transient, analysis-only request payload.
- Cost rises by one heavier vision or document model call per binary capture, bounded by the same daily spend guard as the rest of the pipeline (issue #16).
