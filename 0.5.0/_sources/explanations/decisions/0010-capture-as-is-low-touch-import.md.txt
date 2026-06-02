# 10. CLI capture: an as-is low-touch import mode and a transient budget override

Date: 2026-06-01

## Status

Accepted

## Context

`thoth capture <path>...` (issue #80) backfills content that already lives on disk —
most concretely an existing, already-clean Obsidian vault (a tree of Markdown plus
image/PDF attachments) — by walking it and feeding each file through the *existing*
`Ingestor.ingest` pipeline.

Two design questions had to be resolved before that path could be built:

1. **Re-curate vs import-as-is.** Running every existing note through the curate LLM
   gives thoth's value-add (the 4-folder classification, a `summary:`, wikilinks,
   dedup-merge), but it costs an LLM call *per file* and may reshape notes that were
   already good. For an already-clean Markdown vault a low-touch mode that files and
   indexes the page without re-authoring it is often preferable.
2. **The cost of a bulk import.** A vault import is a real spend burst: with curate it is
   two LLM calls per file (classify + curate). The #16 daily budget guard would defer the
   tail of a large import to the next day — correct for unattended capture, but a
   foot-gun for a deliberate, supervised one-shot backfill the operator *wants* to run to
   completion now.

## Decision

**Support both, default to curate.** `thoth capture` curates by default (the full
value-add). `--as-is` selects a low-touch mode with these exact semantics:

- The cheap **classify** call still runs (one routing call → `type`/`slug`/`title`,
  validated through `Vault`), so the page is routed into the flat 4-folder model.
- The expensive **curate** call is **SKIPPED**. Instead of a model-authored file-plan,
  the page is written **once** via `Vault.write_page` with the **original file body
  verbatim** as the body and a minimal derived frontmatter (`title` from classify,
  `type`/`source: import`/`tags: []`), routed into the classify-chosen folder
  (`entities`/`notes`/`memories`/`actions`). A saved asset is still embedded and any
  analysed OCR text appended, so a binary import stays searchable.
- The durable `raw/` source page and the `inbox/` holding are written exactly as today
  (durability and idempotency unchanged), then the filed page is indexed through the
  **same** Hindsight retain pass. So "files + indexes, skips curate" is literally true:
  no curate call, no reshaping, no wikilink/dedup-merge pass, no summary synthesis.
- The **flat-folder model is an invariant** (ADR 0005): `--as-is` changes only *whether
  curate runs*, never the folder model. The source folder hierarchy is **not** preserved
  and a pre-existing non-vocabulary `type` in a note's frontmatter is **not** honoured —
  the page is re-routed by classify.

**A transient `--budget N` override.** `thoth capture --budget N` overrides
`THOTH_DAILY_LLM_BUDGET` for **this run only**. It is threaded into `make_budget_guard`
and never written back to the frozen `Config`. Per the existing `BudgetGuard` rule a
non-positive limit disables the guard, so `--budget 0` is the "unlimited for this import"
escape hatch and `--budget 50` caps the run at 50 combined calls. With no flag the
configured daily budget applies unchanged. The guard still covers the analyse + classify
(and, in the default mode, curate) calls and the retain pass.

**Batched commits.** `Ingestor.ingest` grew a `commit: bool` seam: `thoth capture` pulls
the vault once up front, ingests each file with `commit=False`, and commits+pushes via
`GitSync.commit` every `--batch-size` files plus a final flush — instead of one commit
per file — to keep history sane. A `VaultConflictError` on a batch commit stops the run
loudly (content is filed locally; never `--force`).

## Consequences

- A vault import is **one cheap classify call per file** in `--as-is` (still
  budget-guarded) instead of two; pages keep their original prose; they are searchable
  immediately via the retain pass.
- A later `thoth slack` / reindex never re-curates an as-is page — it is a normal
  curated-folder page, not an inbox hold — so the import is stable.
- Re-running `thoth capture` over an unchanged tree is a no-op: the existing `raw/` and
  `inbox/` SHA-256 skip means no page is duplicated; only the analyse/classify calls are
  re-spent (and `--budget 0` can lift the cap for a deliberate re-run).
- A crash or Ctrl-C mid-run leaves an uncommitted (dirty) working tree, but the durable
  `inbox/`/`raw/` writes are on disk and a re-run is idempotent, so nothing is lost — the
  vault is simply uncommitted until the next run or a manual commit.
- `import` is added to the legal frontmatter `source` vocabulary (`VALID_SOURCES`) so an
  imported page is writable and distinguishable by provenance.
