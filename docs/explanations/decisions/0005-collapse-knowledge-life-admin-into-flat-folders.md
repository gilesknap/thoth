# 5. Collapse the knowledge / life-admin split into flat equal folders

Date: 2026-05-31

## Status

Accepted

## Context

The vault has always partitioned every non-`raw` page into two *families* by its
frontmatter `type` (`thoth.vault`):

- **Knowledge** (`KNOWLEDGE_TYPES` = `entity, concept, comparison, query, summary`) →
  `entities/ concepts/ comparisons/ queries/`
- **Life-admin** (`LIFE_ADMIN_TYPES` = `action, media, memory, inbox`) →
  `actions/ media/ memories/ people/ inbox/`

That single partition fans out across the codebase: `ingest.py` hangs a `life_admin`
dict on every `Classification`; the navigation pass gives knowledge pages an `index.md`
catalog entry but life-admin pages none; `lint.py` and `summary.py` walk `KNOWLEDGE_DIRS`
and `LIFE_ADMIN_DIRS` as two separate sweeps; recall is scoped by the family tag.

The split was doing **three** jobs. Two are now obsolete or mis-named:

1. **Different indexing — dead.** ADR-0004 made the reindex embed *all* content and
   scope recall by tag at query time. The "knowledge gets a vector, life-admin doesn't"
   rationale that originally justified the families is gone.
2. **Recall precision — alive, but not a family concern.** Keeping "what do I know about
   X?" from surfacing "TODO: read the X paper" is real, but ADR-0004 already handles it
   with a *per-query tag filter*. It does not need a two-tribe god-split in `vault.py`.
3. **Actionable lifecycle — alive, and the only real distinction.** Actions and the
   media queue carry `status`/`due`, so they can be open/overdue/done and want a
   date-sorted Bases dashboard plus overdue lint. A concept cannot be overdue.

So the axis that actually earns its keep is **reference vs actionable**, *not* knowledge
vs personal information — and "actionable" is a property a page *has* (it carries
`status`/`due`), readable straight off the frontmatter, not a tribe it must be filed
into. The two-family framing also mis-shelves content: `people/` pages are already
`type: entity` (knowledge wearing a separate folder), and `memory` pages are durable,
link-worthy, lifecycle-free *personal reference knowledge* that behaves like a note.
Capture is, in any case, already type-free for the user — the Haiku intent gate and the
curate classifier pick the type; the user never names a family. The split is internal
complexity the author was reading in the source, not a decision the system imposes.

## Decision

**Delete the knowledge/life-admin families. Reduce the eight content folders to four
flat, equal folders. Derive the one surviving behaviour (actionable lifecycle) from page
frontmatter, not from a type-family.**

Content folders collapse 8 → 4:

| Folder | Lifecycle? | Holds | Absorbs |
|---|---|---|---|
| `entities/` | no (reference) | nouns: people, orgs, products, models, devices | `people/` |
| `notes/` | no (reference) | everything written, differentiated by a `tags:` value | `concept`, `comparison`, `query` |
| `memories/` | no (reference) | personal memories/milestones, kept as its own folder for Obsidian browsing | — |
| `actions/` | **yes** (`status`/`due`) | todos and the to-consume queue | `media/` (a media item is an `action` tagged `media`) |

- `KNOWLEDGE_TYPES` / `LIFE_ADMIN_TYPES` (and `KNOWLEDGE_DIRS` / `LIFE_ADMIN_DIRS`) are
  removed. The only behavioural fork left is: *does the page carry `status`/`due`?* → it
  appears in the actionable dashboards and gets overdue checks.
- `summary` stops being a content type; it survives only as the label on the spine
  `index.md` Home page (could be renamed `home`). Nothing the user captures is a
  `summary`.
- Machinery is unchanged: `inbox/`, `raw/{articles,papers,transcripts,assets}/`,
  `_bases/ _meta/ _archive/ .obsidian/`, and the `index.md`/`SCHEMA.md`/`log.md` spine.
- Recall scoping (ADR-0004) continues by tag; with families gone, tags carry the
  reference/actionable intent directly (e.g. exclude `action` from knowledge Q&A).

## Consequences

- The mental model matches the implementation: throw anything in, it becomes an Obsidian
  page with frontmatter + links + semantic index, lands in one of four equal folders the
  classifier picks, and is found again without thinking about which "kind" it was.
- The single `vault.py` partition and its fan-out through `ingest.py`, `lint.py`, and
  `summary.py` collapse to a frontmatter-property check, removing a layer of branching
  from the spine.
- Todos and the consume-queue keep their overdue nudges: the Bases dashboards and lint
  key off `due`/`status`, which is unchanged.
- This is a breaking change to the folder/type contract: `FOLDER_TYPE_CONTRACT`,
  `TYPE_ENUMERATION`, the curate file-plan validator (`thoth.llm`), the classify prompt,
  the Bases definitions, and the seed templates all change. Existing vault content under
  `people/`, `concepts/`, `comparisons/`, `queries/`, `media/` needs a one-time
  migration (move files, rewrite `type`, add `tags`), and `index.md` catalog sections
  are regenerated.
- `media` loses its dedicated folder; existing `media`-backlog logic in `summary.py`
  re-targets `actions/` filtered by the `media` tag / `to_consume` status.
- `raw/` is untouched, consistent with ADR-0004.
- The per-page catalog entry this ADR gave the reference types was later superseded by
  ADR-0008: the one-line gloss moved from the agent-maintained `index.md` catalog onto
  the page itself as a `summary:` frontmatter field (canonical + rebuildable), and
  `index.md` became a static set of Bases dashboards.
