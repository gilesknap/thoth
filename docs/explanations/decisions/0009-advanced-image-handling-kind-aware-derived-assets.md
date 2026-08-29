# 9. Kind-aware advanced image handling: derived assets from one vision call

Date: 2026-06-01

## Status

Accepted

## Context

The analyse seam (`thoth.analyse.Analyser`, ADR-0006) already sends one transient-base64 vision call per binary capture. It returns an `Analysis` of extracted text, description, summary, suggested type, entities and concepts, which routes `classify` and bodies `curate`.

But every image was treated identically, as a flat OCR-style pass, even though the captures fall into visibly different *kinds* that each reward different handling:

- A hand-drawn diagram or whiteboard photo is most useful as an *editable* artefact rather than a flat description, because the owner wants to reopen and rework it.
- A document, meaning a phone snap or scan of a printed or handwritten page, deserves a faithful structured markdown transcription with headings, lists and tables, rather than loose OCR prose. The verbatim, well-structured text is the useful artefact.
- A screenshot or a real-world photo wants neither of those, and the existing flat analysis is exactly right.

The naive way to get per-kind behaviour is a separate cheap pre-call, a Haiku classifier, to label the image before the main analyse call. That doubles the per-capture model calls, adds a second deferral point, and re-derives information the main vision model already has in front of it.

One further constraint shapes the design. The project's durability invariant is sacred: the primary analyse, classify, curate and commit path must never be deferred or lost for the sake of a nicety, so a capture is held and filed regardless of whether any enhancement succeeds.

## Decision

**Fold the image *kind* into the existing single vision call, and use it to branch into best-effort, kind-specific handling that enriches the original capture without ever replacing or blocking it.**

### `kind` is part of the one analyse call, not a separate pre-call

`Analysis` gains a `kind` field, one of `diagram`, `document`, `screenshot` or `photo`, or `""` when unknown.

The existing `_RESULT_SHAPE` prompt now also asks for `kind` with a one-line definition of the four values, and the parser validates the answer against that closed set, collapsing anything else to `""`. No extra model round-trip is added for labelling.

### Document transcription is a strengthened prompt on that same call

When the model judges an image a `document`, the prompt instructs it to return the `text` as a faithful structured markdown transcription, preserving headings, lists and tables as markdown, rather than loose OCR. One combined prompt still serves every image and the PDF path.

### `kind == "diagram"` gives an idealised, editable Excalidraw reconstruction

A *second* vision call, `Analyser.reconstruct_excalidraw`, asks the model for only the structure. That is a list of simple node and connector specs, `{id, type, x, y, width, height, text}` for a shape and `{from, to, text}` ids for a connector.

The harness expands each spec into a fully-formed Excalidraw element in code, minting ids and the full styling and bookkeeping property set, and wiring up the relationships Excalidraw expects:

- A shape's label is a bound text element, with `containerId` pointing at the shape and the shape's `boundElements` pointing at the label, so the text is a property of the box.
- A connector that joins two shapes is bound to their edges. Each endpoint snaps to the point on a box's border facing the other box, plus a small gap, with `startBinding` and `endBinding` and the shapes' `boundElements` recording the bond, so arrows attach at the boxes' edges rather than plunging into their centres.
- A connector's own label is bound to the connector, so Excalidraw places it at the line's midpoint over a masked background, near the line and never crossing it.

The harness then assembles the complete `.excalidraw.md` envelope: frontmatter, the plugin's switch-to-view banner, a `## Text Elements` search index, and a `%%`-commented `## Drawing` block holding the scene as uncompressed `json`. The plugin reads both `json` and `compressed-json`, and plain JSON keeps the vault canonical-as-plain-text.

The file is saved alongside the original as `<slug>.excalidraw.md` and embedded as `![[<slug>.excalidraw]]`, with the `.md` dropped so Obsidian renders the drawing rather than the raw JSON note. The original photo is always kept.

Three live-verify lessons are designed out here: a `label`-shorthand or minimal-property scene renders as empty boxes, a `.md`-suffixed embed shows raw JSON, and centre-routed arrows with overlaid labels read as broken.

A model-free OpenCV "scan cleanup" for documents was prototyped and dropped. In practice the de-warp and threshold pass produced misaligned, lower-value output, and the faithful structured-markdown transcription above is the genuinely useful document artefact. No `-scan.png` is produced and `opencv-python-headless` is not a dependency.

### The reconstruction is strictly best-effort

It runs only after the primary analysis succeeds, and reuses the same image bytes already read for the analyse call, so there is no second read.

It is wrapped so that any failure, be it unparseable model output, empty elements or a budget trip raising `BudgetExceededError`, degrades to "no derived asset". The capture is never deferred or lost for an enhancement, and the original asset is always saved and filed.

### Two env-configurable model knobs

These are ADR-style knobs rather than module constants.

`analyse_model` (`THOTH_ANALYSE_MODEL`) selects the model for the folded analyse, kind and transcription call, letting the owner drop to a Haiku for cheap document A/B work. `diagram_model` (`THOTH_DIAGRAM_MODEL`) selects the model for the spatial-reasoning Excalidraw reconstruction.

Both default to `None`, which falls back to `config.anthropic_model` (Sonnet) via the injected `LLM`, so both calls remain on the daily budget guard.

### `ASSET_SLUG_RE` is relaxed to allow compound extensions

The pattern becomes `^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)+$`, so `<slug>.excalidraw.md` validates while `..`, leading dots, uppercase and spaces stay forbidden.

## Consequences

- A binary image capture is now handled by what it is. A whiteboard photo gains an editable `.excalidraw.md` the owner can reopen and rework, a document photo gains a structured-markdown transcription in its body, and a screenshot or snapshot keeps the lean flat analysis. The derived Excalidraw asset is embedded with `![[…]]` next to the original and reaches curate via `RawCaptureResult.asset_paths`.
- The cost ceiling is unchanged for non-diagram images, at one vision call as before, and rises by exactly one extra reconstruction call for diagrams, bounded by the same daily spend guard (issue #16).
- Idempotency and durability are preserved end to end. The derived Excalidraw asset routes through the same byte-SHA-keyed `_store_asset` path, so a byte-identical re-ingest skips the original *and* the derivation. Because the derivation sits after the durable raw hold and the primary analysis, an enhancement failure can never defer or lose a capture.
- This cross-references ADR-0006, the transient base64 to the vision API that this call builds on, and builds on issue #16, the daily budget guard the second call is charged against.
