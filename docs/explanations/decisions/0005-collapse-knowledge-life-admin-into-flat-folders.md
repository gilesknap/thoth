# 5. Collapse the knowledge / life-admin split into flat equal folders

Date: 2026-05-31

## Status

Accepted

Partly superseded by [ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md). The strict folder-by-type contract below is loosened, so the folder becomes a browsing convenience and the dashboards filter on the `type` property, and `media` is split back out of `actions/` into its own `type` and `media/` folder.

## Context

The vault has always partitioned every non-`raw` page into two *families* by its frontmatter `type` (`thoth.vault`):

- **Knowledge** (`KNOWLEDGE_TYPES` = `entity, concept, comparison, query, summary`) lives in `entities/`, `concepts/`, `comparisons/` and `queries/`.
- **Life-admin** (`LIFE_ADMIN_TYPES` = `action, media, memory, inbox`) lives in `actions/`, `media/`, `memories/`, `people/` and `inbox/`.

That single partition fans out across the codebase. `ingest.py` hangs a `life_admin` dict on every `Classification`, the navigation pass gives knowledge pages an `index.md` catalog entry but life-admin pages none, `lint.py` and `summary.py` walk `KNOWLEDGE_DIRS` and `LIFE_ADMIN_DIRS` as two separate sweeps, and recall is scoped by the family tag.

The split was doing three jobs, and two of them are now obsolete or mis-named:

1. **Different indexing, now dead.** ADR-0004 made the reindex embed *all* content and scope recall by tag at query time, so the "knowledge gets a vector, life-admin does not" rationale that originally justified the families is gone.
2. **Recall precision, alive but not a family concern.** Keeping "what do I know about X?" from surfacing "TODO: read the X paper" is real, but ADR-0004 already handles it with a per-query tag filter. It does not need a two-tribe god-split in `vault.py`.
3. **Actionable lifecycle, alive and the only real distinction.** Actions and the media queue carry `status` and `due`, so they can be open, overdue or done, and they want a date-sorted Bases dashboard plus overdue lint. A concept cannot be overdue.

So the axis that actually earns its keep is reference against actionable, not knowledge against personal information. Actionable is a property a page *has*, because it carries `status` and `due`, readable straight off the frontmatter, rather than a tribe it must be filed into.

The two-family framing also mis-shelves content. `people/` pages are already `type: entity`, which is knowledge wearing a separate folder, and `memory` pages are durable, link-worthy, lifecycle-free personal reference knowledge that behaves like a note.

Capture is in any case already type-free for the user, because the Haiku intent gate and the curate classifier pick the type and the user never names a family. The split is internal complexity the author was reading in the source, not a decision the system imposes.

## Decision

**Delete the knowledge and life-admin families. Reduce the eight content folders to four flat, equal folders. Derive the one surviving behaviour, the actionable lifecycle, from page frontmatter rather than from a type-family.**

Content folders collapse from 8 to 4:

| Folder | Lifecycle? | Holds | Absorbs |
|---|---|---|---|
| `entities/` | no (reference) | nouns: people, orgs, products, models, devices | `people/` |
| `notes/` | no (reference) | everything written, differentiated by a `tags:` value | `concept`, `comparison`, `query` |
| `memories/` | no (reference) | personal memories and milestones, kept as its own folder for Obsidian browsing | nothing |
| `actions/` | **yes** (`status`/`due`) | todos and the to-consume queue | `media/`, since a media item is an `action` tagged `media` |

- `KNOWLEDGE_TYPES`, `LIFE_ADMIN_TYPES`, `KNOWLEDGE_DIRS` and `LIFE_ADMIN_DIRS` are removed. The only behavioural fork left is whether the page carries `status` and `due`, in which case it appears in the actionable dashboards and gets overdue checks.
- `summary` stops being a content type. It survives only as the label on the spine `index.md` Home page, and could be renamed `home`. Nothing the user captures is a `summary`.
- Machinery is unchanged: `inbox/`, `raw/{articles,papers,transcripts,assets}/`, `_bases/`, `_meta/`, `_archive/`, `.obsidian/`, and the `index.md`, `SCHEMA.md` and `log.md` spine.
- Recall scoping from ADR-0004 continues by tag. With families gone, tags carry the reference or actionable intent directly, so knowledge Q&A excludes `action` for example.

## Consequences

- The mental model matches the implementation. You throw anything in, it becomes an Obsidian page with frontmatter, links and a semantic index entry, it lands in one of four equal folders the classifier picks, and you find it again without thinking about which kind it was.
- The single `vault.py` partition and its fan-out through `ingest.py`, `lint.py` and `summary.py` collapse to a frontmatter-property check, which removes a layer of branching from the spine.
- Todos and the consume-queue keep their overdue nudges, because the Bases dashboards and lint key off `due` and `status`, which are unchanged.
- This is a breaking change to the folder and type contract. `FOLDER_TYPE_CONTRACT`, `TYPE_ENUMERATION`, the curate file-plan validator (`thoth.llm`), the classify prompt, the Bases definitions and the seed templates all change. Existing vault content under `people/`, `concepts/`, `comparisons/`, `queries/` and `media/` needs a one-time migration to move files, rewrite `type` and add `tags`, and the `index.md` catalog sections are regenerated.
- `media` loses its dedicated folder, so the existing `media`-backlog logic in `summary.py` re-targets `actions/` filtered by the `media` tag and the `to_consume` status.
- `raw/` is untouched, consistent with ADR-0004.
- The per-page catalog entry this ADR gave the reference types was later superseded by ADR-0008. The one-line gloss moved from the agent-maintained `index.md` catalog onto the page itself as a `summary:` frontmatter field, which is canonical and rebuildable, and `index.md` became a static set of Bases dashboards.
