# Vault Schema

## Domain
Personal knowledge management for one user: everything captured — research/reference
knowledge, todos, a to-consume backlog, and personal memories — lands in one of four
flat, equal folders. The only behavioural distinction is whether a page is *actionable*
(carries `status`/`due`), read straight off the frontmatter, not a folder family.
The vault is the single source of truth. Hindsight indexes it; it is never the store.

## Layers (4 flat content folders + machinery, ADR 0005)
- raw/      Immutable sources. The agent READS but NEVER edits these.
- entities/  Reference. Nouns: people, orgs, products, models, devices. `type: entity`.
- notes/     Reference. Everything written, differentiated by a `tags:` value
             (concept / comparison / query). `type: note`.
- memories/  Reference. Personal memories/milestones. `type: memory`.
- actions/   Actionable (`status`/`due`). Todos AND the to-consume queue; a media item
             is an `action` tagged `media`. `type: action`.
- inbox/     Machinery: durable pre-curate holding pages. `type: inbox`.
- index.md (Home) / SCHEMA.md / log.md   Navigational + structural backbone.

## Conventions
- File names: lowercase, hyphens, no spaces, no dates (dates live in frontmatter).
- Every page starts with YAML frontmatter (see Frontmatter).
- Link with [[wikilinks]]; every reference page needs >= 2 outbound links.
- Bump `updated` on every edit. Add every new page to index.md. Append every action to log.md.
- Images: embed inline with ![[asset.ext]] on the owning page AND describe them there.
  Binaries live in raw/assets/. No per-image sidecar files. Never base64.
- Provenance: on pages synthesising 3+ sources, append ^[raw/articles/source.md] to
  paragraphs whose claims trace to one source.

## Frontmatter
Common (every page): title, type, created, updated, source, tags.
type is one of: entity, note, memory, action (plus the inbox machinery type).
Actionable pages (`type: action`) additionally carry `status` (and usually `due_date`);
a media-queue item is an `action` tagged `media` with status to_consume/consuming/consumed.

## raw/ Frontmatter
---
source_url: https://example.com/article   # if applicable
ingested: YYYY-MM-DD
sha256: <hex digest of the body below the closing --->
---
Compute sha256 over the body only. On re-ingest of the same URL: recompute, compare,
skip if identical, flag drift + update if changed.

## Tag Taxonomy
Add a tag HERE before using it (prevents sprawl). Seed set:
- Type: entity, note, memory, action
- Note kind: concept, comparison, query, reference, how-to
- Domain (user-specific): embedded-systems, controls, accelerator, software, ai-ml, home
- People/Orgs: person, org, product, model
- Actionable: task, media, recurring, errand
- Quality: contested, prediction, controversy

## Page Thresholds
- CREATE a page when an entity/concept appears in 2+ sources OR is central to one.
- ADD to an existing page when a source mentions something already covered.
- DON'T create pages for passing mentions or out-of-scope detail.
- SPLIT a page over ~200 lines into sub-topics with cross-links.
- ARCHIVE fully-superseded pages to _archive/ and drop them from index.md.
- Actions and memories are created on demand (one capture = one action/memory page)
  and do NOT need the 2-source threshold.

## Update Policy
On conflict: prefer newer dates; if genuinely contradictory, record both with dates
and sources, set `contradictions:` / `contested: true`, and flag in the lint report.
Never silently overwrite.
