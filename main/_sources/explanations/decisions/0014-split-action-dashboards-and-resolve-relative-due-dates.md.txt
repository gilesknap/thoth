# 14. Split action dashboards by personal, surface undated actions, resolve relative due dates

Date: 2026-06-13

## Status

Accepted

Refines the dashboard half of
[ADR 0013](0013-lean-universal-schema-properties-not-tags.md): the **Work · Personal ·
All view variants** and the 5-section index it introduced are replaced by the two-base
split and 6-section dashboard described here. The lean universal schema (properties not
tags, bare priority labels, `prio_rank`) is unchanged.

## Context

Three problems surfaced in live use of the ADR 0013 dashboard:

1. **Too many overlapping views.** `actions.base` carried ten views — Imminent / Open /
   Media each triplicated across Work · Personal · All via the embed's view dropdown.
   The triplication overlapped confusingly and buried the one view a glance needed.
2. **Personal todos were not surfaced.** They were a dropdown variant of a
   work-centric base, never a destination of their own.
3. **Undated actions vanished — the load-bearing bug.** A Slack capture, *"urgent todo
   monday: investigate the HIGH field autosave failure…"*, was filed `priority: Urgent`
   with **no `due_date`**. It fell out of every dated view and sat invisible in a
   collapsed Open list. Root cause was twofold: the curate model is **never told what
   day it is** (only `created`/`updated` are stamped in code, by `write_page`; `due_date`
   is the one date the model must supply), so it could not turn "monday" into a concrete
   date and safely omitted it; and no view explicitly surfaced open-but-undated work.

The timezone driving every date computation was also hard-coded as `Europe/London` in a
leaf module rather than being configurable.

## Decision

- **One base per item class; views differ only by date window.** Six bases replace the
  old three:
  - `actions` — open **work** todos (`kind != "media"`, `personal != true`).
  - `personal` — open **personal** todos (`personal == true`).
  - `media` — the consume queue (`kind == "media"`, work **and** personal, with a
    `personal` column since the distinction is minor for leisure media).
  - `inbox` — unfiled captures (a single view).
  - `recent` — vault-wide activity by `file.mtime`.
  - `reference` — the curated Notes / Entities / Memories layer (a link line).

  The class filters (`kind`, `personal`, and open-only `status`) live in each base's
  **top-level filter**, so the views genuinely differ by nothing but the date window:
  `actions` / `personal` / `media` each ship `7 Days` / `30 Days` / `All`, and `recent`
  ships `7 Days` / `30 Days` / `60 Days`. The ten-view Work·Personal·All triplication
  and the cross-base filter overlap are gone.
- **Bounded windows always include undated items, expired first.** A `7 Days` / `30
  Days` window matches `due_date <= now() + N days` **or** `due_date.isEmpty()`, so an
  undated todo shows in every window (a standing nag to add a date). `isEmpty()` is
  load-bearing: a *missing* `due_date` (the common case — the key is absent, not blank)
  is **not** matched by `== ""`, which is exactly what hid undated todos from the
  bounded windows in the first live test. A `date_bucket` formula (`overdue = 0`,
  `upcoming = 1`, `undated = 2`, also keyed on `isEmpty()`) is the primary sort, then
  `due_date`, then `prio_rank` — so overdue actions lead, real upcoming deadlines
  follow (soonest first), and undated items trail as a priority-ordered backlog
  (urgent first). This fixes the lost-action bug: an `Urgent` capture with no
  `due_date` can no longer be invisible, while concrete near-term deadlines still
  outrank the undated tail. `All` carries no date filter (every open item of the
  class).
- **The curate prompt states today's date and resolves relative deadlines.** It now
  leads with `Today's date is <YYYY-MM-DD> (<timezone>)` and the file-plan contract
  instructs the model to resolve a relative/natural deadline ("monday", "tomorrow",
  "next week") in the captured text into a concrete `due_date`, while never guessing a
  date when the text gives none. This is a prompt fix, not a date-parsing regex,
  consistent with the shape-via-prompt convention.
- **Timezone is configuration.** `THOTH_TIMEZONE` (default `Europe/London`) resolves to
  a validated `ZoneInfo` on `Config`; a bogus name fails fast at startup. The curate
  date anchor reads it.
- **`index.md` is one callout per base**, each embedding a default window (work and
  personal `7 Days` expanded; media `All`, inbox, and recent collapsed). Each named
  view is the canonical default — the view dropdown restores its filters/sort/columns,
  and a hard reset is re-seeding the `.base` file (Obsidian Bases has no per-embed reset
  button to author).

## Consequences

- Every open action lands in its class base and is visible in the `All` window (and in
  the bounded windows when due soon or undated), so a high-priority capture can no
  longer be invisible to the dashboard — even with no date.
- With the prompt fix, captures that *state* a deadline now usually arrive dated and
  land in the near windows directly; an undated item in a window is a deliberate "add a
  date" prompt rather than a leak.
- Media is a single queue spanning work and personal (its date windows mostly collapse,
  since media is rarely dated); the `personal` column keeps the distinction visible.
- `recent`'s windows order by recency (`file.mtime` DESC), not the `date_bucket` rule —
  it has no `due_date`, so the expired/undated semantics do not apply there.
- `BASE_NAMES` becomes `actions, personal, media, inbox, recent, reference` (`triage`
  is split into `inbox` + `recent`); an existing vault is updated by copying the changed
  `.base` files and `index.md` in (seed never clobbers existing files).
