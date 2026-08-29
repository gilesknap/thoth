# 13. Lean universal schema: view-critical facets are properties, not tags

Date: 2026-06-11

## Status

Accepted

Amends the media wording of [ADR 0005](0005-collapse-knowledge-life-admin-into-flat-folders.md): a media item is now an `action` with `kind: media` rather than an action *tagged* `media`.

The dashboard design below, meaning the Work, Personal and All view variants and the 5-section index, is refined by [ADR 0014](0014-split-action-dashboards-and-resolve-relative-due-dates.md), and the schema itself is unchanged.

The `kind: task|media|errand` facet introduced below is retired by [ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md), where `media` becomes its own `type`, `errand` folds into `action`, and the dashboards filter on `type` instead of folder plus `kind`. The rest of the lean schema, meaning properties not tags, the single `status` lifecycle and bare priority labels, stands.

The "view-critical facets are properties, not tags" rule is clarified by the [amendment at the foot](#adr0013-grouping-amendment) from 2026-06-13. The rule governs what a view *filters or sorts* on, and a view may *group* on a tag-derived key, because grouping buckets every page and an absent tag falls into a visible `uncategorized` group, so it cannot reintroduce the silent-incompleteness failure this ADR exists to prevent.

## Context

The index page's Bases dashboards were broken in two independent ways, confirmed against the live vault of 267 pages:

1. **Documents missing from views.** The view filters used flat tags such as `tags.contains("media")` and `tags.contains("personal")`, while the documented and LLM-applied taxonomy was faceted, as `action-kind/media` and `sensitivity/personal`. Nested tags never match a flat `contains`, so the Media views and the whole personal dashboard were silently near-empty. `status` drift, with `open`, `scheduled` and `in-progress` alongside the documented vocabulary, dropped further actions from status-filtered views.
2. **Sparse columns.** The per-type optional properties were almost never populated: `aliases` 0/23, `people` 1/39, `location` 1/39, `media_type` 0/36, `creator` 0/36, `recurrence` 0/36 and `project` 1/36, while `memory_date` at 3/39 was what memories.base *sorted* by. The common core of `title`, `type`, `created`, `updated`, `source` and `tags`, plus `summary` on reference pages, was about 100% filled.

The lesson is that anything a view filters or sorts on must be a frontmatter property the pipeline actively populates and lint enforces. A tag convention the LLM may or may not follow, or an optional field nothing fills, silently breaks the dashboard.

Live data also showed personal-ness is not action-specific, because entities and memories carried `sensitivity/personal` too.

## Decision

- **View-critical facets become frontmatter properties, and tags become purely descriptive topic labels.** There is a new action property `kind: task|media|errand`, and a new universal boolean `personal` on every content type. The taxonomy drops its Type, Actionable and Sensitivity facets, and the prompts instruct the model never to duplicate type, kind or personal as a tag.
- **A universal set** on the four content types: `title`, `type`, `created`, `updated`, `source`, `tags`, `summary` and `personal`. `summary` extends to actions, because the dashboards' Summary column shows it. `inbox/` holds are exempt machinery, carrying only `title`, `type`, `created`, `updated`, `source` and `sha256`, with no tags. `personal` defaults to `false` at write time, and the work-default views filter `personal != true` so that a missing value counts as work.
- **Lean extensions only**, each actively populated by curate and enforced by lint. Actions get `kind`, `status`, `due_date?` and `priority?`, plus `media_type` and `url` when `kind: media`. Memories get `memory_date`, falling back to `created`. Dropped are `aliases`, `people`, `location`, `project`, `recurrence` and `creator`. Alias *resolution* stays in the lint wikilink checks, because it is Obsidian-native and a human may still add one.
- **A single status vocabulary**, `todo | in_progress | done | cancelled`, for every action regardless of kind. Media-ness is carried by `kind` and never by parallel status values, so `to_consume`, `consuming` and `consumed` are retired.
- **Priority is a bare severity label**, `Urgent | High | Medium | Low`, rather than the old sort-prefixed `1 - Urgent` through `4 - Low`. The numbers only existed to make a Bases ASC string sort fall in severity order, and that ordering now lives in a `prio_rank` formula in `actions.base`, running `Urgent=0` through `Low=3` with unknown last, with every view sorting by `formula.prio_rank`. The stored value stays a clean label, the sort key lives in one place, and the curate prompt no longer emits magic numbers.
- **Three lifecycle bases** mirroring ACTIONABLE, CURATED and machinery: `actions.base`, `reference.base` and `triage.base`, replacing the seven per-folder files. Each action-backed section ships Work, Personal and All view variants switched in place via the embed's view dropdown, and `index.md` becomes a 5-section attention dashboard of Imminent, Inbox, Actions, Media and Recent, with the reference layer as a link line.
- The vocabularies live in `thoth.vault` as the single source. The curate prompt renders them, the file-plan validator enum-checks them so the repair loop self-corrects, and lint enforces the full universal set. The validator does not hard-require `personal` or `summary`, because `write_page` defaults `personal` and `summary` arrives via the page-level plan field, so lint owns those.
- No Metadata-Menu preset config ships. The SPEC claim that one would be preserved was false and is removed.

## Consequences

- Bases filters compare properties such as `kind == "media"` and `personal != true`, which the write path defaults, the curate pass fills and lint guards, so a page can no longer satisfy the pipeline yet be invisible to every dashboard.
- The summary scans key media off `kind`, and the open-actions scan excludes `kind: media` so that backlog items do not flood the daily digest or `pkm_actions` now that they share the `todo` lifecycle.
- The live vault needs a one-off, never-committed migration covering the status map, tags to properties, dropped-key deletion and the spine and bases swap, run against a clone and verified with `thoth lint` before pushing.
- There are fewer knobs. Anything we later want a view to filter on must first earn a property, a curate slot and a lint slot, which is the point.

(adr0013-grouping-amendment)=

## Amendment: grouping may bucket on tags (2026-06-13)

The reference browse layer, meaning the `notes`, `entities` and `memories` bases, groups its card views on a tag-derived key. Notes ▸ *By Topic* groups on the first `domain/*` tag and Entities ▸ *By Kind* on `entity-kind/*`, so around 150 reference pages become browsable by subject.

This does not weaken the rule above. The failure that rule prevents is a page being silently dropped from a view when an unreliable, LLM-applied tag is missing, which is what made the live Media and personal dashboards near-empty.

Grouping cannot cause that failure:

- The base filters on a property the pipeline enforces (`type == "note"`), so every page of that type is in the view.
- The tag only chooses which *bucket* a page sits in. A page with no matching tag is not hidden, and falls into a visible `uncategorized` group.

So the operative rule is sharpened rather than broken:

> View **membership and sort order** must key on enforced frontmatter properties and never
> on tags, because a missing tag would silently drop or mis-order a page. A view **may
> group** on a tag-derived display bucket, since an absent tag yields a visible
> `uncategorized` group rather than an invisible page.

Concretely, `tags.contains(...)` in a base `filters:` block stays forbidden, still guarded by `test_no_base_filters_on_tags`, while deriving a group key from tags via a `formula:` consumed by `groupBy:` is permitted.
