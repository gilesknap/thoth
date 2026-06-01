# 11. Retain a synthetic page-level record so fact-light pages stay recallable

Date: 2026-06-01

## Status

Accepted

## Context

Hindsight's `memory retain` is the appliance's only path into the semantic index, and
it performs **LLM fact-extraction**: a retained page is split into atomic
world/experience/opinion facts. It exposes no verbatim/embed/token-chunk retain mode
(ADR 0007) — phrasing the retained text is the only lever over what gets stored.

Fact-extraction works for fact-rich pages, but a large class of personal-vault content
has **no discrete extractable facts**: photo/`memory` pages, terse notes, lists,
bookmarks, descriptive snapshots. For those, retain stores **zero units**, so the page
is **completely absent from semantic recall** — `recall` can never return it, regardless
of the query. Only the lexical `grep` fallback can reach such a page, and only on exact
token overlap (so "what pets do I have?" cannot find a page that says *dog* /
*Labradoodle*). This undercuts the core promise: ask the vault in natural language and
get the right page back. Observed live during the old-vault import: a richly described
dog photo produced 0 units and was unrecallable, while only two fact-rich pages held any
units at all.

## Decision

Retain **one synthetic page-level record per page**, built only from material thoth
already has — the classify/curate `title` + `summary` + `entities`/`concepts` +
frontmatter `tags` — phrased as plain **declarative assertions about the page** ("This
page is about X. It concerns A, B. It is tagged …"). That is precisely the shape a
fact-extractor keeps as a fact rather than discarding as "no facts".

This record **complements** fact-extraction rather than replacing it: the record is
*prepended to the page body* and the whole blob is retained, so a fact-rich body still
yields its extra facts while a fact-light body still lands its one page-record. It is a
single compact block per page, so per-page index cost stays **bounded** (no fact
fan-out). The construction is a shared helper (`thoth.hindsight.page_record_text`) used
by both retain paths — capture (`ingest._retain_facts`) and full reindex
(`reindex_from_vault`) — so a rebuild stores the same record capture does. Reindex has no
classification, so its record omits the `entities`/`concepts` lines (title + summary +
tags still anchor what the page is about).

Phrasing the retained text — not a CLI flag — is the mechanism because Hindsight offers
no non-extraction retain mode to bypass extraction with. This is **Direction 1** of the
issue; the `fact_type` hint (Direction 2) and a separate embedding index (Direction 4)
are not pursued, since the page-record meets the acceptance bar within the existing
backend.

## Consequences

- **Every curated page contributes ≥1 recallable unit**, so no page is silently absent
  from semantic recall — the fact-light class (photos, terse notes, bookmarks) becomes
  recallable by what it is *about*, bridging the vocabulary gap lexical `grep` cannot.
- Per-page index cost is bounded to one extra block; the fact-rich path is unchanged
  (the record is additive).
- The two retain paths share one record builder, so capture and reindex stay in lockstep.
- This rephrases what reaches the extractor; whether the extractor actually keeps the
  declarative record as a unit is a **real-service behaviour** that the injected-fakes
  suite cannot prove. It must be verified live on the appliance (recall the dog photo by
  a natural-language query), per the gold-standard bar in the `thoth-testing` skill.
