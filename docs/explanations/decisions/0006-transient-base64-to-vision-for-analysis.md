# 6. Transient base64 to the vision/document API for analysis

Date: 2026-05-31

## Status

Accepted

## Context

The capture pipeline (`thoth.ingest`) historically filed a binary capture (an uploaded
image or PDF) **blind**: the model never saw the file. `Ingestor._capture_summary`
rendered only a single `File: screenshot.png` line, and that line was all the classify
and curate passes ever received. Consequences (confirmed in code and live, 2026-05-31):

- `classify` had no content to route on, so it defaulted `type` to `memory` and **every
  image/PDF landed in `memories/`**.
- `curate` wrote a boilerplate stub ("an image captured and stored…") around an
  `![[asset]]` embed — nothing about the asset's actual content was searchable.

Issue #42 adds an **analyse** seam (`thoth.analyse.Analyser`) that OCRs / vision-analyses
an image and reads a PDF natively, then feeds the extracted text + a suggested type +
named entities/concepts into both `classify` (routing) and `curate` (body). To do that,
the asset's bytes must reach a multimodal Claude model. The Anthropic Messages API takes
image bytes as a base64 `image` content block and PDF bytes as a base64 `document`
content block — there is no by-reference alternative for a server-held binary.

This appears to collide with the rule baked into `Capture` and the persona:

> Binary bytes never travel as base64 (SPEC §6). … Never store base64.

But that rule is about **storage / canonical form**, not transport for analysis. Its
intent is twofold and unchanged here:

1. The vault never holds base64 — an asset is a real binary file under `raw/assets/`,
   embedded with `![[…]]`; we never write a base64 blob into a markdown page or a
   descriptive sidecar.
2. A byte-blob is never the *source of truth* — the canonical artefact is the stored
   binary, and the curated page links to it.

Sending the same bytes as base64 to the vision API to **analyse** them — while the asset
is still saved as a real binary and embedded — is consistent with that intent: the base64
is **transient** (it exists only inside one outbound request and is never persisted) and
**analysis-only** (it enriches the body and drives routing; it is never written back or
treated as canonical). It is, nonetheless, a deliberate amendment to the literal "binary
bytes never travel as base64" wording, so it is recorded here rather than left implicit.

## Decision

**Permit transient base64 encoding of a binary capture's bytes for the sole purpose of
sending them to the vision/document API in the analyse pass (issue #42). The storage rule
is unchanged: the vault never holds base64, and the stored binary remains canonical.**

Concretely:

- `thoth.analyse.Analyser.analyse_image` / `analyse_pdf` base64-encode the bytes into a
  vision `image` / `document` content block, send one Claude call, and return the parsed
  `Analysis`. The base64 lives only in that request.
- The asset continues to be saved as a real binary under `raw/assets/` (idempotent on its
  bytes SHA-256) and embedded with `![[…]]`; the analysis only enriches the curated body
  and routes the capture. No base64 is ever written to a vault file.
- The `Capture` docstring is tightened to scope the SPEC §6 rule to *storage / canonical
  form* and to point at this ADR for the transient-analysis amendment.
- The analyse call goes through the injected `thoth.llm.LLM`, so it is charged against the
  **same daily budget guard** as every other Anthropic call (ADR-adjacent issue #16): one
  heavier vision/document call per binary capture, and a cap-reached day raises
  `BudgetExceededError` *before* the request.

## Consequences

- A binary capture is now routed **by its content** (a whiteboard photo → `notes/`, a
  receipt → an `action`, etc.) instead of always defaulting to `memories/`, and the
  curated page body holds the real OCR'd/extracted text — searchable, clusterable, and
  cross-linked — rather than a blind stub.
- Durability is preserved by reusing the existing decoupled-durability/deferral pattern:
  the raw asset and the `inbox/` hold are persisted *before* any model call, so if the
  analyse call is unavailable (transport failure) or the daily budget cap is reached, the
  capture **defers** (raw held, re-analysed on a later sweep) rather than being lost —
  exactly like the classify/curate deferral. An *unparseable* analysis is non-fatal: the
  binary is filed blind (the prior behaviour), never aborted.
- The storage invariant is untouched and still enforced: `Vault.save_asset` writes real
  bytes, `![[…]]` embeds reference the bare filename, and no code path writes base64 into
  a page. The amendment is strictly about a transient, analysis-only request payload.
- Cost rises by one heavier (vision/document) model call per binary capture, bounded by
  the same daily spend guard as the rest of the pipeline (issue #16).
