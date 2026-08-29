# 14. Split action dashboards by personal, surface undated actions, resolve relative due dates

Date: 2026-06-13

## Status

Accepted

Refines the dashboard half of [ADR 0013](0013-lean-universal-schema-properties-not-tags.md). The Work, Personal and All view variants and the 5-section index it introduced are replaced by the two-base split and 6-section dashboard described here, and the lean universal schema of properties not tags, bare priority labels and `prio_rank` is unchanged.

Refined by [ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md), where the six bases below now filter on the `type` property rather than `file.inFolder(...)` plus `kind`, with `media` promoted to its own `type`. The base set, the views and the date-window logic are otherwise as described here.

## Context

Three problems surfaced in live use of the ADR 0013 dashboard:

1. **Too many overlapping views.** `actions.base` carried ten views, with Imminent, Open and Media each triplicated across Work, Personal and All via the embed's view dropdown. The triplication overlapped confusingly and buried the one view a glance needed.
2. **Personal todos were not surfaced.** They were a dropdown variant of a work-centric base, never a destination of their own.
3. **Undated actions vanished, which was the load-bearing bug.** A Slack capture, *"urgent todo monday: investigate the HIGH field autosave failure…"*, was filed `priority: Urgent` with no `due_date`. It fell out of every dated view and sat invisible in a collapsed Open list.

The root cause of the third was twofold. The curate model is never told what day it is, since only `created` and `updated` are stamped in code by `write_page` while `due_date` is the one date the model must supply, so it could not turn "monday" into a concrete date and safely omitted it. And no view explicitly surfaced open-but-undated work.

The timezone driving every date computation was also hard-coded as `Europe/London` in a leaf module rather than being configurable.

## Decision

### One base per item class, with views differing only by date window

Six bases replace the old three:

- `actions` holds open work todos (`kind != "media"`, `personal != true`).
- `personal` holds open personal todos (`personal == true`).
- `media` is the consume queue (`kind == "media"`, work *and* personal, with a `personal` column since the distinction is minor for leisure media).
- `inbox` holds unfiled captures, as a single view.
- `recent` is vault-wide activity by `file.mtime`.
- `reference` is the curated Notes, Entities and Memories layer, as a link line.

The class filters, meaning `kind`, `personal` and the open-only `status`, live in each base's top-level filter, so the views genuinely differ by nothing but the date window. `actions`, `personal` and `media` each ship `7 Days`, `30 Days` and `All`, and `recent` ships `7 Days`, `30 Days` and `60 Days`. The ten-view Work, Personal and All triplication and the cross-base filter overlap are gone.

### Bounded windows always include undated items, expired first

A `7 Days` or `30 Days` window matches `due_date <= now() + N days` **or** `due_date.isEmpty()`, so an undated todo shows in every window as a standing nag to add a date.

`isEmpty()` is load-bearing. A *missing* `due_date`, which is the common case because the key is absent rather than blank, is not matched by `== ""`, and that is exactly what hid undated todos from the bounded windows in the first live test.

A `date_bucket` formula, with `overdue = 0`, `upcoming = 1` and `undated = 2`, also keyed on `isEmpty()`, is the primary sort, then `due_date`, then `prio_rank`. So overdue actions lead, real upcoming deadlines follow soonest first, and undated items trail as a priority-ordered backlog with urgent first.

That fixes the lost-action bug, because an `Urgent` capture with no `due_date` can no longer be invisible, while concrete near-term deadlines still outrank the undated tail. `All` carries no date filter, so it holds every open item of the class.

### The curate prompt states today's date and resolves relative deadlines

It now leads with `Today's date is <YYYY-MM-DD> (<timezone>)`, and the file-plan contract instructs the model to resolve a relative or natural deadline in the captured text, such as "monday", "tomorrow" or "next week", into a concrete `due_date`, while never guessing a date when the text gives none.

This is a prompt fix rather than a date-parsing regex, consistent with the shape-via-prompt convention.

### Timezone is configuration

`THOTH_TIMEZONE`, defaulting to `Europe/London`, resolves to a validated `ZoneInfo` on `Config`, and a bogus name fails fast at startup. The curate date anchor reads it.

### `index.md` is one callout per base

Each callout embeds a default window: work and personal at `7 Days` expanded, and media at `All`, inbox and recent collapsed.

Each named view is the canonical default, so the view dropdown restores its filters, sort and columns, and a hard reset is re-seeding the `.base` file, because Obsidian Bases has no per-embed reset button to author.

## Consequences

- Every open action lands in its class base and is visible in the `All` window, and in the bounded windows when due soon or undated, so a high-priority capture can no longer be invisible to the dashboard even with no date.
- With the prompt fix, captures that *state* a deadline now usually arrive dated and land in the near windows directly, so an undated item in a window is a deliberate "add a date" prompt rather than a leak.
- Media is a single queue spanning work and personal, and its date windows mostly collapse since media is rarely dated, while the `personal` column keeps the distinction visible.
- `recent`'s windows order by recency (`file.mtime` DESC) rather than by the `date_bucket` rule, because it has no `due_date` so the expired and undated semantics do not apply there.
- `BASE_NAMES` becomes `actions, personal, media, inbox, recent, reference`, with `triage` split into `inbox` and `recent`. An existing vault is updated by copying the changed `.base` files and `index.md` in, because the seed never clobbers existing files.
