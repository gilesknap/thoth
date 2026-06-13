# Vault Schema

## Domain
Personal knowledge management for one user: everything captured — research/reference
knowledge, todos, a to-consume backlog, and personal memories — lands in one of five
flat, equal folders. The only behavioural distinction is whether a page is *actionable*
(carries `status`/`due`), read straight off the frontmatter `type`, not a folder family.
The vault is the single source of truth. Hindsight indexes it; it is never the store.

## Layers (5 flat content folders + machinery, ADR 0005, 0015)
The folder is a loose browsing convenience — Thoth files each page in its canonical
folder, but the Bases dashboards filter on the `type` property, so moving a file between
folders never hides it. `inbox/` and `raw/` stay folder-strict machinery.
- raw/      Immutable sources. The agent READS but NEVER edits these.
- entities/  Reference. Nouns: people, orgs, products, models, devices. `type: entity`.
- notes/     Reference. Everything written, differentiated by a `tags:` value
             (concept / comparison / query). `type: note`.
- memories/  Reference. Personal memories/milestones. `type: memory`.
- actions/   Actionable (`status`/`due`). Todos and errands. `type: action`.
- media/     Actionable (`status`/`due`). The to-consume queue (books/films/...).
             `type: media`.
- inbox/     Machinery: durable pre-curate holding pages. `type: inbox`.
- index.md (Home) / SCHEMA.md / log.md   Navigational + structural backbone.

## Conventions
- File names: lowercase, hyphens, no spaces, no dates (dates live in frontmatter).
- Every page starts with YAML frontmatter (see Frontmatter).
- Link with [[wikilinks]]; every reference page needs >= 2 outbound links.
- Bump `updated` on every edit. Append every action to log.md. (index.md is static — never edit it.)
- Images: embed inline with ![[asset.ext]] on the owning page AND describe them there.
  Binaries live in raw/assets/. No per-image sidecar files. Never base64.
- Provenance: on pages synthesising 3+ sources, append ^[raw/articles/source.md] to
  paragraphs whose claims trace to one source.

## Frontmatter (ADR 0013, 0015)
Universal (every content page): title, type, created, updated, source, tags,
summary, personal.
type is one of: entity, note, memory, action, media (plus the inbox machinery type).
`summary` is one crisp line saying what the page is about — its canonical,
rebuildable gloss (no separate index catalog). `personal` is a real boolean that
keys off the **subject, not whether it is a chore**: true when the item concerns
the owner's private life (home, family, friends, hobbies, personal admin,
books/films to watch), false for work / technical / professional / general
knowledge. A task being an errand does not make it personal — a work errand (e.g.
booking a meeting room) is `personal: false`. It is the property the
Work·Personal dashboard views filter on.

Action and media pages (`type: action`, `type: media`) additionally carry:
- status: todo | in_progress | done | cancelled   (one lifecycle for both types)
- due_date: YYYY-MM-DD (optional), priority: Urgent | High | Medium | Low (optional)
Media pages (`type: media`) also carry, when known:
- media_type: book | film | tv | podcast | article | video | music
- url

Memory pages (`type: memory`) carry memory_date: YYYY-MM-DD — when the memory
happened (falls back to `created` when omitted).

Inbox holding pages (`type: inbox`) are machinery, not content: title, type,
created, updated, source, sha256 only — no tags, no summary, no personal.

## raw/ Frontmatter
---
source_url: https://example.com/article   # if applicable
ingested: YYYY-MM-DD
sha256: <hex digest of the body below the closing --->
---
Compute sha256 over the body only. On re-ingest of the same URL: recompute, compare,
skip if identical, flag drift + update if changed.

## Tag Taxonomy
Tags are descriptive topic labels ONLY — never duplicate `type` or
`personal` as a tag (those are frontmatter properties the views filter on).
Add a tag HERE before using it (prevents sprawl). Seed set:
- Note kind: concept, comparison, query, reference, how-to
- Domain (user-specific): embedded-systems, controls, accelerator, software, ai-ml, home
- People/Orgs: person, org, product, model
- Quality: contested, prediction, controversy

## Page Thresholds
- CREATE a page when an entity/concept appears in 2+ sources OR is central to one.
- ADD to an existing page when a source mentions something already covered.
- DON'T create pages for passing mentions or out-of-scope detail.
- SPLIT a page over ~200 lines into sub-topics with cross-links.
- ARCHIVE fully-superseded pages to _archive/.
- Actions and memories are created on demand (one capture = one action/memory page)
  and do NOT need the 2-source threshold.

## Update Policy
On conflict: prefer newer dates; if genuinely contradictory, record both with dates
and sources, set `contradictions:` / `contested: true`, and flag in the lint report.
Never silently overwrite.
