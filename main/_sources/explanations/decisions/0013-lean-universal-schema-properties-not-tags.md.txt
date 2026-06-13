# 13. Lean universal schema: view-critical facets are properties, not tags

Date: 2026-06-11

## Status

Accepted

Amends the media wording of
[ADR 0005](0005-collapse-knowledge-life-admin-into-flat-folders.md): a media item
is now an `action` with `kind: media`, not an action *tagged* `media`.

The dashboard design below (the Work · Personal · All view variants and the 5-section
index) is refined by
[ADR 0014](0014-split-action-dashboards-and-resolve-relative-due-dates.md); the schema
itself is unchanged.

The `kind: task|media|errand` facet introduced below is **retired** by
[ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md): `media` becomes
its own `type`, `errand` folds into `action`, and the dashboards filter on `type` instead
of folder + `kind`. The rest of the lean schema (properties not tags, the single `status`
lifecycle, bare priority labels) stands.

## Context

The index page's Bases dashboards were broken in two independent ways, confirmed
against the live vault (267 pages):

1. **Documents missing from views.** The view filters used flat tags
   (`tags.contains("media")`, `tags.contains("personal")`) while the documented —
   and LLM-applied — taxonomy was faceted (`action-kind/media`,
   `sensitivity/personal`). Nested tags never match a flat `contains`, so the
   Media views and the whole personal dashboard were silently near-empty. `status`
   drift (`open`, `scheduled`, `in-progress` alongside the documented vocab)
   dropped further actions from status-filtered views.
2. **Sparse columns.** The per-type optional properties were almost never
   populated: `aliases` 0/23, `people` 1/39, `location` 1/39, `media_type` 0/36,
   `creator` 0/36, `recurrence` 0/36, `project` 1/36 — while `memory_date` (3/39)
   was what memories.base *sorted* by. The common core
   (`title,type,created,updated,source,tags`, plus `summary` on reference pages)
   was ~100% filled.

The lesson: **anything a view filters or sorts on must be a frontmatter property
the pipeline actively populates and lint enforces** — a tag convention the LLM may
or may not follow, or an optional field nothing fills, silently breaks the
dashboard. Live data also showed personal-ness is not action-specific (entities
and memories carried `sensitivity/personal` too).

## Decision

- **View-critical facets become frontmatter properties; tags become purely
  descriptive topic labels.** New action property `kind: task|media|errand`; new
  universal boolean `personal` on every content type. The taxonomy drops its
  Type / Actionable / Sensitivity facets, and the prompts instruct the model never
  to duplicate type/kind/personal as a tag.
- **Universal set** on the four content types:
  `title, type, created, updated, source, tags, summary, personal` — `summary`
  extends to actions (the dashboards' Summary column shows it). `inbox/` holds are
  exempt machinery: `title, type, created, updated, source, sha256` only (no
  tags). `personal` defaults to `false` at write time, and the work-default views
  filter `personal != true` so a missing value counts as work.
- **Lean extensions only** — each actively populated by curate and lint-enforced:
  actions `kind, status, due_date?, priority?` (+ `media_type, url` when
  `kind: media`); memories `memory_date` (falls back to `created`). **Dropped**:
  `aliases, people, location, project, recurrence, creator`. (Alias *resolution*
  stays in the lint wikilink checks — Obsidian-native, a human may still add one.)
- **Single status vocabulary** `todo | in_progress | done | cancelled` for every
  action regardless of kind; media-ness is carried by `kind`, never by parallel
  status values (`to_consume`/`consuming`/`consumed` are retired).
- **Priority is a bare severity label** `Urgent | High | Medium | Low` — not the old
  sort-prefixed `1 - Urgent`..`4 - Low`. The numbers only existed to make a Bases
  ASC string sort fall in severity order; that ordering now lives in a `prio_rank`
  formula in `actions.base` (`Urgent=0`..`Low=3`, unknown last) and every view sorts
  by `formula.prio_rank`. The stored value stays a clean label, the sort key lives in
  one place, and the curate prompt no longer emits magic numbers.
- **Three lifecycle bases** mirroring ACTIONABLE / CURATED / machinery:
  `actions.base`, `reference.base`, `triage.base` (replacing the seven per-folder
  files). Each action-backed section ships **Work · Personal · All view variants**
  switched in place via the embed's view dropdown; `index.md` becomes a 5-section
  attention dashboard (Imminent, Inbox, Actions, Media, Recent) with the reference
  layer as a link line.
- The vocabularies live in `thoth.vault` as the single source; the curate prompt
  renders them, the file-plan validator enum-checks them (the repair loop
  self-corrects), and lint enforces the full universal set. The validator does
  **not** hard-require `personal`/`summary` (write_page defaults `personal`;
  `summary` arrives via the page-level plan field) — lint owns those.
- No Metadata-Menu preset config ships; the SPEC claim that one would be preserved
  was false and is removed.

## Consequences

- Bases filters compare properties (`kind == "media"`, `personal != true`) that
  the write path defaults, the curate pass fills, and lint guards — a page can no
  longer satisfy the pipeline yet be invisible to every dashboard.
- The summary scans key media off `kind`, and the open-actions scan excludes
  `kind: media` so backlog items don't flood the daily digest or `pkm_actions`
  now that they share the `todo` lifecycle.
- The live vault needs a one-off, never-committed migration (status map,
  tags→properties, dropped-key deletion, spine/bases swap) run against a clone
  and verified with `thoth lint` before pushing.
- Fewer knobs: anything we later want a view to filter on must first earn a
  property + curate + lint slot, which is the point.
