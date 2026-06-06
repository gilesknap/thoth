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
- A **document** (a phone snap or scan of a printed/handwritten page) deserves a *faithful
  structured markdown* transcription (headings, lists, tables), not loose OCR prose — the
  verbatim, well-structured text is the useful artefact.
- A **screenshot** or a real-world **photo** wants neither of those; the existing flat
  analysis is exactly right.

The naive way to get per-kind behaviour is a separate cheap pre-call (a Haiku classifier)
to label the image before the main analyse call. That doubles the per-capture model calls,
adds a second deferral point, and re-derives information the main vision model already has
in front of it.

One further constraint shapes the design: the project's durability invariant is sacred —
the primary analyse/classify/curate/commit path must never be deferred or lost for the sake
of a nicety — a capture is held and filed regardless of whether any enhancement succeeds.

## Decision

**Fold the image *kind* into the existing single vision call, and use it to branch into
best-effort, kind-specific handling that enriches — but never replaces and never blocks —
the original capture.**

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
  `{from, to, text}` ids for a connector) — and the harness **expands each spec into a
  fully-formed Excalidraw element in code** (ids, the full styling/bookkeeping property set),
  wiring up the relationships Excalidraw expects: a shape's label is a **bound text element**
  (`containerId` → the shape, the shape's `boundElements` → the label) so the text is a
  *property of the box*; a connector that joins two shapes is **bound to their edges** — each
  endpoint snaps to the point on a box's border facing the other box (plus a small gap), with
  `startBinding`/`endBinding` and the shapes' `boundElements` recording the bond, so arrows
  attach at the boxes' edges rather than plunging into their centres; and a connector's own
  label is bound to the connector so Excalidraw places it at the line's midpoint over a masked
  background, near the line and never crossing it. The harness then assembles the complete
  `.excalidraw.md` envelope: frontmatter, the plugin's switch-to-view banner, a
  `## Text Elements` search index, and a `%%`-commented `## Drawing` block holding the scene as
  **uncompressed `json`** (the plugin reads both `json` and `compressed-json`; plain JSON keeps
  the vault canonical-as-plain-text). The file is saved *alongside* the original as
  `<slug>.excalidraw.md` and embedded as `![[<slug>.excalidraw]]` — **the `.md` is dropped** so
  Obsidian renders the *drawing*, not the raw JSON note; **the original photo is always kept**.
  (Live-verify lessons: a `label`-shorthand / minimal-property scene renders as empty boxes, a
  `.md`-suffixed embed shows raw JSON, and centre-routed arrows with overlaid labels read as
  broken — all designed out here.)

  *(A model-free OpenCV "scan cleanup" for documents was prototyped here but **dropped**: in
  practice the de-warp/threshold pass produced misaligned, lower-value output, and the
  faithful structured-markdown transcription above is the genuinely useful document artefact.
  No `-scan.png` is produced and `opencv-python-headless` is not a dependency.)*

- **The reconstruction is strictly best-effort.** It runs only after the primary analysis
  succeeds, reuses the *same* image bytes already read for the analyse call (no second read),
  and is wrapped so that *any* failure — unparseable model output, empty elements, or a budget
  trip (`BudgetExceededError`) — degrades to "no derived asset". The capture is never deferred
  or lost for an enhancement; the original asset is always saved and filed.

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
  editable `.excalidraw.md` the owner can reopen and rework; a document photo gains a
  structured-markdown transcription in its body; a screenshot or snapshot keeps the lean flat
  analysis. The derived Excalidraw asset is embedded with `![[…]]` next to the original and
  reaches curate via `RawCaptureResult.asset_paths`.
- The cost ceiling is unchanged for non-diagram images (one vision call, as before) and rises
  by exactly one extra reconstruction call for diagrams — bounded by the same daily spend
  guard (issue #16).
- Idempotency and durability are preserved end-to-end: the derived Excalidraw asset routes
  through the same byte-SHA-keyed `_store_asset` path, so a byte-identical re-ingest skips the
  original *and* the derivation; and because the derivation sits *after* the durable raw hold
  and the primary analysis, an enhancement failure can never defer or lose a capture.
- Cross-references ADR-0006 (transient base64 to the vision API — the call this builds on) and
  builds on issue #16 (the daily budget guard the second call is charged against).
