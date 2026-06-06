# 8. Page-frontmatter `summary` is the canonical gloss; index.md is static

Date: 2026-06-01

## Status

Accepted

## Context

`index.md` (the Home page, seeded from `src/thoth/templates/index.md`) carried a
`## Knowledge catalog` section: a per-page `- [[link]] — summary` list maintained by the
curate LLM (an `index_entries` array in the file-plan) and written by
`Vault.append_index`. Those one-line summaries were **authored content that lived ONLY
in `index.md`** — page frontmatter held only `title, type, created, updated, source,
tags`, with no summary field.

That violated the project's #1 principle (recorded throughout the SPEC and ADR-0004):
*the vault is canonical; everything else is a disposable, rebuildable projection.* The
catalog summaries could not be regenerated from the vault, so `index.md` was secretly a
partial source-of-truth. It also drifted, cost prompt surface, and forced `lint.py` to
police catalog completeness and a `Total pages: N` count that nothing kept current.

The reference types that earned a catalog entry — `entity`, `note`, `memory`
(`REFERENCE_TYPES`, the lifecycle-free types per ADR-0005) — are exactly the pages a
one-line gloss makes sense for; `action` pages are surfaced by the Bases dashboards, not
a summary.

## Decision

**Move the one-line summary onto the page itself, and make `index.md` a tight, fully
static set of Bases dashboards that agents never read or write.**

- Add an optional one-line `summary:` frontmatter field, scoped to the reference types
  (`SUMMARY_TYPES` = `REFERENCE_TYPES` = `entity`/`note`/`memory`). It is authored by the
  curate LLM at write time (a per-page `summary` string in the file-plan, replacing the
  `index_entries` array) and routed into the page frontmatter by `ingest.py`. It
  round-trips through `vault.py` read/write like any other frontmatter field.
- The summary is now **canonical and rebuildable** (it lives on the page) and
  **grep-able**: `query.py`'s grep scans the whole file including frontmatter, so the
  existing grep pass transparently absorbs what the catalog pass used to do. `query.py`
  drops its pass-1 `index.md` catalog parse; the cost order is now grep → wikilink
  traversal → Hindsight recall.
- `index.md` becomes static: just the `# 🏠` title and the live `.base` dashboard embeds.
  The top blockquote (`Total pages …`, `Agents: read SCHEMA.md …`) and the whole
  `## Knowledge catalog` section are removed. No code reads or writes it.
- `Vault.append_index` / `INDEX_SECTIONS`, the curate `index_entries` contract, and the
  ingest catalog-derivation machinery are removed.
- `lint.py` drops the `Total pages: N` check and the catalog-completeness check, and adds
  the replacement invariant: a reference page missing a non-empty `summary:` is flagged
  (same severity tier the catalog-completeness check used), preserving the "every
  reference page is glossed" guarantee on the page.

## Consequences

- The gloss is part of the canonical vault page, regenerable on a reindex, and found by
  the cheapest retrieval pass; no separate catalog pass or agent-maintained list exists.
- `index.md` cannot drift, costs no prompt surface, and needs no completeness lint.
- Per the project's throwaway-vault rule there is **no migration**: existing pages
  without a `summary` simply do not have one until they are re-curated; the lint
  invariant surfaces them.
- Cross-references ADR-0004 (index-all + tag-scoped recall) and ADR-0005 (the
  reference/actionable axis the `SUMMARY_TYPES` scope follows).
