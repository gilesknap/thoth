"""The 13 SPEC section 11 maintenance checks as a pure vault scan.

This package is the appliance's deterministic maintenance pass (SPEC section 11 and the
Appendix "Lint checks" table). It is a pure programmatic markdown scan over a real
:class:`thoth.vault.Vault`, with no network, no LLM and no subprocess. Checks 1 to 12
are each a method returning ``list[Finding]``, :meth:`LintEngine.run` aggregates them
into a :class:`LintReport` grouped and counted by :class:`Severity`, and check 13
(:meth:`LintEngine.record`) appends exactly one ``log.md`` entry carrying the count.

1.  **Orphan pages** -- a curated page with no inbound link. Life-admin pages are
    exempt, since the Bases dashboards surface them.
2.  **Broken links** -- a target resolving to no page, honouring ``aliases``. The
    highest severity.
3.  **Summary gloss** -- a non-empty one-line ``summary:`` on every content page (issue
    #72, ADR 0008), the rebuildable home of the gloss that replaced the old
    agent-maintained ``index.md`` catalog.
4.  **Frontmatter validation** -- required fields present, ``type`` valid, type-specific
    fields present, ``personal`` a real boolean, and every ``status``, ``priority`` and
    ``media_type`` value inside the vault vocabularies.
5.  **Stale content** -- a knowledge page older than :data:`STALE_DAYS`, an open action
    past its ``due_date``, and a media item still cold after :data:`MEDIA_STALE_DAYS`.
6.  **Contradictions** -- a page with ``contested: true`` or a non-empty
    ``contradictions:`` list.
7.  **Source drift** -- a ``raw/`` page whose recomputed body sha256 differs from its
    stored one.
8.  **Quality signals** -- ``confidence: low``, and a single-source page carrying no
    ``confidence`` at all.
9.  **Page size** -- a body over :data:`PAGE_SIZE_LIMIT` lines.
10. **Tag audit** -- a tag in use that is absent from ``SCHEMA.md``'s taxonomy.
11. **Image hygiene** -- an orphan binary in ``raw/assets/``, a page embedding a missing
    asset, and a surviving per-image sidecar.
12. **Log rotation** -- a ``log.md`` over :data:`LOG_ROTATE_LIMIT` entries.
13. **Report and log** -- group by severity and append one ``log.md`` line.

Every folder, type and slug constant and every status, priority and media_type
vocabulary is imported from :mod:`thoth.vault`, so the closed-surface contract stays
single-sourced (ADR 0013). The only injected non-determinism is ``today``, so the stale,
overdue and media-cold windows are reproducible under a frozen clock. Only the standard
library plus ``frontmatter``, ``yaml`` and import-light ``thoth`` modules are imported
at module level, so importing this package at pytest collection is always CI-safe.
"""

from thoth._time import LONDON
from thoth.vault import ACTIONABLE_DIRS, CURATED_DIRS

from .checks_freshness import (
    LOG_ROTATE_LIMIT,
    MEDIA_STALE_DAYS,
    PAGE_SIZE_LIMIT,
    STALE_DAYS,
)
from .checks_metadata import (
    MEDIA_TYPE_VOCAB,
    PRIORITY_VOCAB,
    STATUS_VOCAB,
    TYPE_REQUIRED_FIELDS,
)
from .engine import EXCLUDED_DIRS, SPINE_FILES, LintEngine
from .model import Finding, LintError, LintReport, Severity
from .parse import (
    extract_embeds,
    extract_links,
    extract_wiki_embeds,
    extract_wiki_links,
    parse_taxonomy_tags,
)

__all__ = [
    "LONDON",
    "CURATED_DIRS",
    "ACTIONABLE_DIRS",
    "SPINE_FILES",
    "EXCLUDED_DIRS",
    "PAGE_SIZE_LIMIT",
    "LOG_ROTATE_LIMIT",
    "STALE_DAYS",
    "MEDIA_STALE_DAYS",
    "TYPE_REQUIRED_FIELDS",
    "STATUS_VOCAB",
    "PRIORITY_VOCAB",
    "MEDIA_TYPE_VOCAB",
    "Severity",
    "Finding",
    "LintReport",
    "LintError",
    "LintEngine",
    "parse_taxonomy_tags",
    "extract_links",
    "extract_embeds",
    "extract_wiki_links",
    "extract_wiki_embeds",
]

# CURATED_DIRS and ACTIONABLE_DIRS are thoth.vault's canonical folder vocabulary (ADR
# 0005), re-exported here so lint consumers derive the same list rather than restate it.
# A curated page is a lifecycle-free reference page under CURATED_DIRS, which the
# orphan, index-completeness, page-size and quality-signal checks scope to. The
# ACTIONABLE_DIRS pages are exempt from those two because Bases dashboards surface them,
# but they still carry the frontmatter contract and summary gloss and get the overdue
# and cold-media checks instead
