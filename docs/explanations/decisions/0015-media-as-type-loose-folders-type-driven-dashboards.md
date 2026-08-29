# 15. Media is a type, dashboards filter on type, folders go loose

Date: 2026-06-13

## Status

Accepted

Refines [ADR 0013](0013-lean-universal-schema-properties-not-tags.md) by retiring the `kind` facet it introduced, and [ADR 0014](0014-split-action-dashboards-and-resolve-relative-due-dates.md) because its class bases now filter on the `type` property rather than the folder.

It also loosens the strict folder-by-type contract of [ADR 0005](0005-collapse-knowledge-life-admin-into-flat-folders.md), so the folder is now a browsing convenience rather than the thing the dashboards read.

## Context

ADR 0013 made a media item an `action` carrying `kind: media`, and ADR 0014 built the six class dashboards by filtering each `.base` on the folder plus that `kind`, using `file.inFolder("actions")` with `kind != "media"` for todos and `kind == "media"` for the queue.

Two frictions surfaced in daily use:

1. **Recategorising an item took two coordinated edits.** The `type` and `kind` properties and the folder a file lives in encoded the same fact twice. Changing a page's property, which is the natural Obsidian gesture of editing the frontmatter, did not move it between dashboards, because the bases keyed off `file.inFolder(...)`. To actually move an item you had to edit the property *and* drag the file. The SPEC's own framing in ADR 0005, that actionable is a property a page has, read straight off the frontmatter rather than a tribe it must be filed into, had drifted from the implementation.
2. **Moving a todo to the media queue was a two-property change.** A task becoming a to-consume item meant editing `kind: task` to `kind: media` while leaving `type: action`, which is two facets describing one move. `kind` existed only to split one folder three ways into `task`, `media` and `errand`, and `errand` and `task` were never even distinguished by a dashboard.

The redundancy was the root cause. Folder, `type` and `kind` carried overlapping information, so every recategorisation had to keep all three in sync by hand.

## Decision

**Make the frontmatter `type` the single source of truth for what a page is and which dashboard it appears in. Promote media to its own `type`, retire `kind`, and let the folder go loose.**

- **`media` becomes a content `type`.** `TYPE_ENUMERATION` is now `entity, note, memory, action, media`, plus the `inbox` machinery type. A to-consume item that was `type: action` with `kind: media` is simply `type: media`, keeping the shared `status` lifecycle and its `media_type` and `url` fields.
- **`kind` is deleted.** `ACTION_KIND_VOCAB` and every consumer drop it, meaning the curate contract, the file-plan validator, lint check 4, the summary scans and the `.base` columns. `errand` is retired with it, because an errand is just a `type: action` and no dashboard ever separated errands from tasks, so nothing is lost.
- **The dashboards filter on `type` rather than the folder.** `actions.base` is `type == "action"` with `personal != true`, `personal.base` is `type == "action"` with `personal == true`, `media.base` is `type == "media"`, and `reference.base`'s three views are `type == "note"`, `"entity"` and `"memory"`. Recategorising is now a single `type` edit, the dashboards re-sort instantly, and no file move is required.
- **The folder becomes a loose browsing convenience.** Thoth still *writes* each page to its canonical folder, so a fresh capture lands tidily and the file tree stays navigable, but nothing behavioural depends on where a file sits and a manual move never hides a page from its dashboard. `inbox/` and `raw/` stay folder-strict, because they are pipeline machinery rather than content classes.
- **Media gets its own `media/` folder again.** ADR 0005 folded the old `media/` into `actions/`, and with media now a `type` it gets a matching folder, keeping one folder loosely per class. `FOLDER_TYPE_CONTRACT` gains `media -> media`, `ACTIONABLE_DIRS` becomes `(actions, media)`, and the seed creates an empty `media/`.

## Consequences

- The mental model and the implementation re-converge on ADR 0005's stated intent: a page's behaviour is read off its frontmatter, and the folder is just where it happens to sit. Moving an item between any two dashboards is one property edit.
- `kind` leaves the whole pipeline. The classify prompt offers `media` as a type, the curate contract and `validate_file_plan` require `status` rather than `kind` on `action` and `media` pages, lint check 4 enforces the `media` type's `status`, and the daily-digest scans split todos (`actions/`, `type != media`) from the media queue (`media/`, `type == media`).
- `media` joins `INDEXED_DIRS`, having already been indexed inside `actions/`. Reference recall still scopes to `REFERENCE_TYPES` of `entity`, `note` and `memory`, which excludes both `action` and `media`.
- This is a breaking change to the frontmatter and dashboard contract. The live vault needs a one-off, never-committed migration: strip `kind`, rewrite `type: action` plus `kind: media` to `type: media` and move those files to `media/`, drop the `kind` column from the bases, and re-seed the six `.base` files, `SCHEMA.md` and `index.md`. Verify with `thoth lint` against a clone before pushing.
- The cost the redundancy was buying, a file's folder always matching its type, is now only *eventually* true, since thoth files canonically but a human may move a file and the dashboards still work. That is the deliberate trade: type is authoritative and folder is cosmetic.
