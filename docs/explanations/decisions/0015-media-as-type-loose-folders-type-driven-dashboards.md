# 15. Media is a type, dashboards filter on type, folders go loose

Date: 2026-06-13

## Status

Accepted

Refines [ADR 0013](0013-lean-universal-schema-properties-not-tags.md) (retires the
`kind` facet it introduced) and [ADR 0014](0014-split-action-dashboards-and-resolve-relative-due-dates.md)
(its class bases now filter on the `type` property, not the folder). Loosens the
strict folder-by-type contract of
[ADR 0005](0005-collapse-knowledge-life-admin-into-flat-folders.md): the folder is now a
browsing convenience, not the thing the dashboards read.

## Context

ADR 0013 made a media item an `action` carrying `kind: media`, and ADR 0014 built the
six class dashboards by filtering each `.base` on the **folder** plus that `kind`
(`file.inFolder("actions")` + `kind != "media"` for todos, `kind == "media"` for the
queue). Two frictions surfaced in daily use:

1. **Recategorising an item took two coordinated edits.** The `type`/`kind` properties
   and the folder a file lives in encoded the *same* fact twice. Changing a page's
   property (the natural Obsidian gesture — edit the frontmatter) did **not** move it
   between dashboards, because the bases keyed off `file.inFolder(...)`. To actually move
   an item you had to edit the property *and* drag the file. The SPEC's own framing (ADR
   0005: "actionable is a property a page *has*, read straight off the frontmatter, not a
   tribe it must be filed into") had drifted from the implementation.

2. **Moving a todo to the media queue was a two-property change.** A task becoming a
   to-consume item meant editing `kind: task` → `kind: media` while leaving `type:
   action` — two facets describing one move, with `kind` existing only to split one
   folder three ways (`task`/`media`/`errand`), where `errand` and `task` were never even
   distinguished by a dashboard.

The redundancy was the root cause: folder, `type`, and `kind` carried overlapping
information, so every recategorisation had to keep all three in sync by hand.

## Decision

**Make the frontmatter `type` the single source of truth for what a page is and which
dashboard it appears in. Promote media to its own `type`, retire `kind`, and let the
folder go loose.**

- **`media` becomes a content `type`.** `TYPE_ENUMERATION` is now `entity, note, memory,
  action, media` (plus the `inbox` machinery type). A to-consume item that was `type:
  action` + `kind: media` is simply `type: media`. It keeps the shared `status`
  lifecycle and its `media_type`/`url` fields.
- **`kind` is deleted.** `ACTION_KIND_VOCAB` and every consumer (the curate contract, the
  file-plan validator, lint check 4, the summary scans, the `.base` columns) drop it.
  `errand` is retired with it — an errand is just a `type: action` (no dashboard ever
  separated errands from tasks, so nothing is lost).
- **The dashboards filter on `type`, not the folder.** `actions.base` is `type ==
  "action"` + `personal != true`; `personal.base` is `type == "action"` + `personal ==
  true`; `media.base` is `type == "media"`; `reference.base`'s three views are `type ==
  "note"/"entity"/"memory"`. Recategorising is now a **single `type` edit** and the
  dashboards re-sort instantly, no file move required.
- **The folder becomes a loose browsing convenience.** Thoth still *writes* each page to
  its canonical folder (so a fresh capture lands tidily and the file tree stays
  navigable), but nothing behavioural depends on where a file sits — a manual move never
  hides a page from its dashboard. `inbox/` and `raw/` stay folder-strict: they are
  pipeline machinery, not content classes.
- **Media gets its own `media/` folder again.** ADR 0005 folded the old `media/` into
  `actions/`; with media now a `type`, it gets a matching folder (one folder loosely per
  class). `FOLDER_TYPE_CONTRACT` gains `media -> media`, `ACTIONABLE_DIRS` becomes
  `(actions, media)`, and the seed creates an empty `media/`.

## Consequences

- The mental model and the implementation re-converge on ADR 0005's stated intent: a
  page's behaviour is read off its frontmatter, and the folder is just where it happens
  to sit. Moving an item between any two dashboards is one property edit.
- `kind` leaves the whole pipeline: the classify prompt offers `media` as a type, the
  curate contract and `validate_file_plan` require `status` (not `kind`) on `action` and
  `media` pages, lint check 4 enforces the `media` type's `status`, and the daily-digest
  scans split todos (`actions/`, `type != media`) from the media queue (`media/`, `type
  == media`).
- `media` joins `INDEXED_DIRS` (it was already indexed inside `actions/`); reference
  recall still scopes to `REFERENCE_TYPES` (`entity`/`note`/`memory`), which excludes both
  `action` and `media`.
- This is a breaking change to the frontmatter/dashboard contract: the live vault needs a
  one-off, never-committed migration — strip `kind`; rewrite `type: action` + `kind:
  media` to `type: media` and move those files to `media/`; drop the `kind` column from
  the bases; re-seed the six `.base` files, `SCHEMA.md`, and `index.md`. Verified with
  `thoth lint` against a clone before pushing.
- The cost the redundancy was buying — a file's folder always matching its type — is now
  only *eventually* true (thoth files canonically; a human may move a file and the
  dashboards still work). That is the deliberate trade: type is authoritative, folder is
  cosmetic.
