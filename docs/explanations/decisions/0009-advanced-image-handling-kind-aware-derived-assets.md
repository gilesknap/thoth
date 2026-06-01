# 9. Kind-aware advanced image handling: derived assets from one vision call

Date: 2026-06-01

## Status

Accepted

## Context

The analyse seam (`thoth.analyse.Analyser`, ADR-0006) already sends one transient-base64
vision call per binary capture and returns an `Analysis` (extracted text, description,
summary, suggested type, entities, concepts) that routes `classify` and bodies `curate`.
But every image was treated identically — a flat OCR-style pass — even though the captures
fall into visibly different *kinds* that each reward different handling:

- A **hand-drawn diagram / whiteboard** photo is most useful as an *editable* artefact, not
  a flat description: the owner wants to reopen and rework it.
- A **document** (a phone snap or scan of a printed/handwritten page) is captured skewed,
  loosely cropped, and unevenly lit — a clean top-down "scan" is far more legible — and its
  text deserves a *faithful structured markdown* transcription (headings, lists, tables),
  not loose OCR prose.
- A **screenshot** or a real-world **photo** wants neither of those; the existing flat
  analysis is exactly right.

The naive way to get per-kind behaviour is a separate cheap pre-call (a Haiku classifier)
to label the image before the main analyse call. That doubles the per-capture model calls,
adds a second deferral point, and re-derives information the main vision model already has
in front of it.

Two further constraints shape the design. First, the OpenCV document cleanup is a heavy
dependency (`opencv-python-headless`) that must not become a base install or run in CI.
Second, the project's durability invariant is sacred: the primary
analyse/classify/curate/commit path must never be deferred or lost for the sake of a nicety
— a capture is held and filed regardless of whether any enhancement succeeds.

## Decision

**Fold the image *kind* into the existing single vision call, and use it to branch into
three best-effort *derived assets* that enrich — but never replace and never block — the
original capture.**

- **`kind` is part of the one analyse call, not a separate pre-call.** `Analysis` gains a
  `kind` field (one of `diagram` / `document` / `screenshot` / `photo`, or `""` when
  unknown). The existing `_RESULT_SHAPE` prompt now also asks for `kind` with a one-line
  definition of the four values, and the parser validates the answer against that closed
  set (anything else collapses to `""`). No extra model round-trip is added for labelling.

- **Document transcription is a *strengthened prompt* on that same call**, not a new call.
  When the model judges an image a `document`, the prompt instructs it to return the `text`
  as a **faithful structured markdown transcription** — preserving headings, lists, and
  tables as markdown — rather than loose OCR. One combined prompt still serves every image
  and the PDF path.

- **`kind == "diagram"` → an idealised, editable Excalidraw reconstruction.** A *second*
  vision call (`Analyser.reconstruct_excalidraw`) asks the model for only the *structure* — a
  list of simple node/connector specs (`{id, type, x, y, width, height, text}` for a shape,
  `{from, to}` ids for a connector) — and the harness **expands each spec into a fully-formed
  Excalidraw element in code** (ids, the full styling/bookkeeping property set, a shape's
  label rendered as a centred overlaid `text` element, a connector routed between the named
  shapes' centres), then assembles the complete `.excalidraw.md` envelope: frontmatter, the
  plugin's switch-to-view banner, a `## Text Elements` search index, and a `%%`-commented
  `## Drawing` block holding the scene as **uncompressed `json`** (the plugin reads both
  `json` and `compressed-json`; plain JSON keeps the vault canonical-as-plain-text). The file
  is saved *alongside* the original as `<slug>.excalidraw.md` and embedded as
  `![[<slug>.excalidraw]]` — **the `.md` is dropped** so Obsidian renders the *drawing*, not
  the raw JSON note; **the original photo is always kept**. (Live-verify lessons: a
  `label`-shorthand / minimal-property scene renders as empty boxes, and a `.md`-suffixed
  embed shows raw JSON — both are designed out here.)

- **`kind == "document"` → a model-free OpenCV cleaned scan.** `thoth.scanner.clean_document`
  decodes the bytes, finds the largest 4-point contour, perspective-de-warps to a top-down
  view, adaptively thresholds to crisp B/W, and re-encodes to PNG — **pure OpenCV/NumPy, no
  Anthropic call, no budget cost**. A detected quad is accepted **only when it covers ≥50% of
  the frame and spans ≥60% of each axis** (a conservative page gate): naive "largest 4-point
  contour" otherwise latches onto a small logo/icon and warps to a tiny junk crop (the
  live-verify failure), so a low-confidence detection degrades to *no scan* rather than a bad
  one. The cleaned scan is saved as `<slug>-scan.png` *alongside* the original and embedded;
  again the original is kept. `opencv-python-headless` is a **runtime optional dependency**
  (the `runtime` extra), lazily imported *inside* the function so the module stays
  import-safe under pytest collection and the autosummary docs build where OpenCV is absent —
  exactly like the `whisper` / `exa_py` / `firecrawl` seams.

- **Both derivations are strictly best-effort.** They run only after the primary analysis
  succeeds, reuse the *same* image bytes already read for the analyse call (no second read),
  and are wrapped so that *any* failure — unparseable model output, empty elements, a budget
  trip (`BudgetExceededError`), a broken OpenCV install, or no document-like quad — degrades
  to "no derived asset". The capture is never deferred or lost for an enhancement; the
  original asset is always saved and filed.

- **Two env-configurable model knobs** (ADR-style, not module constants): `analyse_model`
  (`THOTH_ANALYSE_MODEL`) selects the model for the folded analyse/kind/transcription call —
  letting the owner drop to a Haiku for cheap document A/B — and `diagram_model`
  (`THOTH_DIAGRAM_MODEL`) selects the model for the spatial-reasoning Excalidraw
  reconstruction. Both default to `None`, which falls back to `config.anthropic_model`
  (Sonnet) via the injected `LLM`, so both calls remain on the daily budget guard.

- **`ASSET_SLUG_RE` is relaxed to allow compound extensions**
  (`^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)+$`) so `<slug>.excalidraw.md` validates while
  `..`, leading dots, uppercase, and spaces stay forbidden.

## Consequences

- A binary image capture is now handled *by what it is*: a whiteboard photo gains an
  editable `.excalidraw.md` the owner can reopen and rework; a document photo gains a legible
  `-scan.png` and a structured-markdown transcription in its body; a screenshot or snapshot
  keeps the lean flat analysis. Every derived asset is embedded with `![[…]]` next to the
  original and reaches curate via `RawCaptureResult.asset_paths`.
- The cost ceiling is unchanged for non-diagram images (one vision call, as before) and rises
  by exactly one extra reconstruction call for diagrams — bounded by the same daily spend
  guard (issue #16). The OpenCV cleanup adds *zero* model cost.
- CI and the docs build stay green without OpenCV: `cv2`/`numpy` are never imported at module
  scope, `tests/test_scanner.py` is guarded with `pytest.importorskip("cv2")` so it skips when
  OpenCV is absent and runs when it is installed, and the scanner module's docstring carries no
  cv2 doctest.
- Idempotency and durability are preserved end-to-end: derived assets route through the same
  byte-SHA-keyed `_store_asset` path, so a byte-identical re-ingest skips the original *and*
  the derivations; and because the derivations sit *after* the durable raw hold and the primary
  analysis, an enhancement failure can never defer or lose a capture.
- Cross-references ADR-0006 (transient base64 to the vision API — the call this builds on) and
  builds on issue #16 (the daily budget guard the second call is charged against).
