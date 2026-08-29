# 17. Adopt the Open Knowledge Format: standard markdown links, not wikilinks

Date: 2026-06-16

## Status

Accepted

Refines the link convention assumed by [ADR 0008](0008-page-summary-frontmatter-static-index.md), for cross-linked pages, and [ADR 0012](0012-blend-grep-and-semantic-retrieval-rrf.md), where the wikilink graph-hop is one of the three retrieval sources.

The vault page model of [ADR 0013](0013-lean-universal-schema-properties-not-tags.md) and [ADR 0015](0015-media-as-type-loose-folders-type-driven-dashboards.md) is unchanged.

## Context

Google Cloud published [Open Knowledge Format (OKF) v0.1](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/) on 2026-06-12. It is a vendor-neutral spec for agent-consumable knowledge bases: a directory of markdown files with YAML frontmatter, standard markdown inter-links, and one mandatory frontmatter field, `type`.

The thoth vault is already structurally OKF-compatible, being plain markdown, git-backed, with YAML frontmatter and a `type` on every page. The one gap was link style, because thoth wrote Obsidian `[[wiki links]]` and `![[embeds]]` where OKF requires `[text](relative/path.md)` and `![alt](path)`.

Obsidian follows standard markdown links and builds its graph from them identically to wiki links, so adopting the standard form costs nothing in the Obsidian UX (issue #189).

Two Obsidian features have no standard-markdown equivalent, though. A Bases view embed is `![[file.base#View]]`, where `#View` selects a named view rather than a heading, and an Excalidraw drawing embed is `![[name.excalidraw]]`, where the plugin keys on that basename to render the drawing rather than the raw scene JSON.

Neither is an OKF artefact, because OKF governs only the markdown concept documents and the links between them, so neither can or should be rewritten.

## Decision

**Adopt OKF standard markdown links as the vault's link convention, and migrate the existing vault to match.**

- **Inter-page links** become `[Title](folder/slug.md)`, and **image embeds** become `![](../raw/assets/asset.ext)`, a page-relative, percent-encoded path. This is the form `useMarkdownLinks: true` plus `newLinkFormat: "relative"` makes Obsidian itself emit, so both Obsidian and thoth now write the same OKF-conformant links.
- **The curate producer is switched to emit markdown links.** The file-plan contract (`file_plan_contract_text`), the `submit_file_plan` tool schema, `SCHEMA.md` as a live curate system-prompt input, and the persona's image-embed instruction all now ask for `[text](path.md)` and `![alt](path)`. The asset-embed harness (`_append_embeds`) emits a page-relative markdown image embed. The vestigial `wikilinks` plan array, a `>=2` count the validator enforced but never wrote to disk, is retired: links live in the body, the prompt still asks for two or more, and lint enforces the graph after the fact.
- **The readers are made markdown-aware.** The lint link-graph extractors `extract_links` and `extract_embeds`, and the retrieval graph-hop `_follow_wikilinks`, now parse the markdown form *and* still recognise a residual `[[wikilink]]`, reducing every target to its bare slug stem, since vault slugs are unique. That dual-parse makes the code correct whether a page is migrated or not, so there is no flag day between the code merge and the vault migration.
- **A new lint check, number 14, enforces the convention.** Any `[[wikilink]]` or wiki *image* embed in a scanned content page is a `STYLE` finding, so a regression back to wiki syntax is caught.
- **Documented exceptions keep their Obsidian form**, because they are not OKF artefacts and have no portable markdown equivalent. Those are Bases `.base` view embeds in the `_bases/index.md` dashboards, Excalidraw `.excalidraw` drawing embeds, anything inside a fenced or inline code span, and the whole of `raw/`, which is immutable source clips the agent never edits whose links point at a defunct prior-vault structure. Lint check 14 exempts `.base` and `.excalidraw` embeds, and never scans `raw/`, the spine files or `_bases/`.

## Consequences

- **The vault is now an OKF bundle.** Every `.md` to `.md` link and image embed in the curated layer is standard markdown, and every page has `type`, so an OKF-aware agent can traverse the graph directly.
- **The migration is a pure syntax transform, verified to preserve the graph.** Linting the same vault before and after migration with the new code, image-hygiene findings are identical at 131 against 131, while orphans and broken links drop slightly, from 72 to 69 and from 418 to 386, because the migration's slugify resolved case-mismatched dangling links such as `[[Diamond Light Source]]` into `[…](diamond-light-source.md)` against real pages. The link-style check goes from 1239 to 0. There is no orphan-flood and no silent loss of the retrieval graph-hop, which are the failure modes a naive write-only change would have caused.
- **Obsidian writes OKF links going forward too.** `.obsidian/app.json` sets `useMarkdownLinks: true` and `newLinkFormat: "relative"`, so links the owner adds by hand match what thoth emits. Per the clean-slate stance, the live vault was migrated on a branch as a one-off, and nothing here is a committed migration shim.
- **OKF tolerates broken links**, since "a link whose target does not exist is not malformed", which matches thoth's existing behaviour. Dangling concept-stub links survive the migration as broken markdown links and are still surfaced by lint check 2, exactly as they were as broken wikilinks.
- **Deferred:** the MCP and Slack *citation handles*, the `[[slug]]` shown beside an `obsidian://` link in tool output, still emit wiki syntax. They are chat-output handles rather than vault content, so they do not affect OKF compliance, and converting them safely needs the Slack `mrkdwn` rendering layer, so it is left as a follow-up.
