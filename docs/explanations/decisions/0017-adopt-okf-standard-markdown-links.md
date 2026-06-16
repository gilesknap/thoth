# 17. Adopt the Open Knowledge Format: standard markdown links, not wikilinks

Date: 2026-06-16

## Status

Accepted

Refines the link convention assumed by
[ADR 0008](0008-page-summary-frontmatter-static-index.md) (cross-linked pages) and
[ADR 0012](0012-blend-grep-and-semantic-retrieval-rrf.md) (the wikilink graph-hop is one
of the three retrieval sources). The vault page model of
[ADR 0013](0013-lean-universal-schema-properties-not-tags.md) /
[ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md) is unchanged.

## Context

Google Cloud published [Open Knowledge Format (OKF) v0.1](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)
on 2026-06-12 — a vendor-neutral spec for agent-consumable knowledge bases: a directory
of markdown files with YAML frontmatter, **standard markdown inter-links**, and one
mandatory frontmatter field (`type`). The thoth vault is already structurally
OKF-compatible — plain markdown, git-backed, YAML frontmatter, every page already carries
`type`. The one gap was **link style**: thoth wrote Obsidian `[[wiki links]]` and
`![[embeds]]`, where OKF requires `[text](relative/path.md)` and `![alt](path)`.

Obsidian follows standard markdown links and builds its graph from them identically to
wiki links, so adopting the standard form costs nothing in the Obsidian UX (issue #189).
Two Obsidian features have **no** standard-markdown equivalent, though: a Bases view
embed (`![[file.base#View]]`, where `#View` selects a named view, not a heading) and an
Excalidraw drawing embed (`![[name.excalidraw]]`, where the plugin keys on that basename
to render the drawing rather than the raw scene JSON). Neither is an OKF artefact — OKF
governs only the markdown concept documents and the links between them — so neither can
or should be rewritten.

## Decision

**Adopt OKF standard markdown links as the vault's link convention, and migrate the
existing vault to match.**

- **Inter-page links** become `[Title](folder/slug.md)`; **image embeds** become
  `![](../raw/assets/asset.ext)` (a page-relative, percent-encoded path). This is the
  form `useMarkdownLinks: true` + `newLinkFormat: "relative"` makes Obsidian itself emit,
  so both Obsidian and thoth now write the same OKF-conformant links.
- **The curate producer is switched to emit markdown links.** The file-plan contract
  (`file_plan_contract_text`), the `submit_file_plan` tool schema, `SCHEMA.md` (a live
  curate system-prompt input), and the persona's image-embed instruction all now ask for
  `[text](path.md)` / `![alt](path)`. The asset-embed harness (`_append_embeds`) emits a
  page-relative markdown image embed. The vestigial `wikilinks` plan array — a >=2 count
  the validator enforced but never wrote to disk — is **retired**: links live in the body,
  the prompt still asks for >=2, and lint enforces the graph after the fact.
- **The readers are made markdown-aware.** The lint link-graph extractors
  (`extract_links` / `extract_embeds`) and the retrieval graph-hop (`_follow_wikilinks`)
  now parse the markdown form *and* still recognise a residual `[[wikilink]]`, reducing
  every target to its bare slug stem (vault slugs are unique). This dual-parse makes the
  code correct whether a page is migrated or not — there is no flag day between the code
  merge and the vault migration.
- **A new lint check (14) enforces the convention.** Any `[[wikilink]]` or wiki *image*
  embed in a scanned content page is a `STYLE` finding, so a regression back to wiki
  syntax is caught.
- **Documented exceptions keep their Obsidian form** (they are not OKF artefacts and have
  no portable markdown equivalent): Bases `.base` view embeds (the `index.md` dashboards),
  Excalidraw `.excalidraw` drawing embeds, anything inside a fenced/inline code span, and
  the whole of `raw/` (immutable source clips the agent never edits, whose links point at
  a defunct prior-vault structure). Lint check 14 exempts `.base`/`.excalidraw` embeds and
  never scans `raw/`, the spine files, or `_bases/`.

## Consequences

- **The vault is now an OKF bundle.** Every `.md`↔`.md` link and image embed in the
  curated layer is standard markdown; every page has `type`. An OKF-aware agent can
  traverse the graph directly.
- **The migration is a pure syntax transform, verified to preserve the graph.** Linting
  the same vault before and after migration with the new code: image-hygiene findings are
  identical (131→131), orphans and broken-links *drop slightly* (72→69, 418→386) because
  the migration's slugify resolved case-mismatched dangling links (e.g.
  `[[Diamond Light Source]]` → `[…](diamond-light-source.md)`) to real pages, and the
  link-style check goes 1239→0. No orphan-flood, no silent loss of the retrieval
  graph-hop — the failure modes a naive write-only change would have caused.
- **Obsidian writes OKF links going forward too.** `.obsidian/app.json` sets
  `useMarkdownLinks: true` + `newLinkFormat: "relative"`, so links the owner adds by hand
  match what thoth emits. (Per the clean-slate stance, the live vault was migrated on a
  branch as a one-off; nothing here is a committed migration shim.)
- **OKF tolerates broken links** ("a link whose target does not exist is not malformed"),
  which matches thoth's existing behaviour: dangling concept-stub links survive the
  migration as broken markdown links and are still surfaced by lint check 2 — exactly as
  they were as broken wikilinks.
- **Deferred:** the MCP/Slack *citation handles* (the `[[slug]]` shown beside an
  `obsidian://` link in tool output) still emit wiki syntax. They are chat-output handles,
  not vault content, so they do not affect OKF compliance; converting them safely needs
  the Slack `mrkdwn` rendering layer and is left as a follow-up.
