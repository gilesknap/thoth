"""The 13 SPEC section 11 / Appendix maintenance checks as a pure vault scan.

This package is the appliance's deterministic maintenance pass (SPEC section 11 and the
Appendix "Lint checks" table). It is a *pure programmatic markdown scan* over a real
:class:`thoth.vault.Vault`: no network, no LLM, no subprocess. Each of checks 1-12 is a
method returning ``list[Finding]``; :meth:`LintEngine.run` aggregates them into a
:class:`LintReport` grouped and counted by :class:`Severity`; check 13
(:meth:`LintEngine.record`) appends **exactly one** ``log.md`` entry via
:meth:`thoth.vault.Vault.append_log` carrying the issue count.

The 13 checks (SPEC Appendix table):

1.  **Orphan pages** -- curated knowledge pages with zero inbound ``[[wikilinks]]``
    (life-admin pages are exempt; Bases surface them).
2.  **Broken wikilinks** -- ``[[target]]`` references that resolve to no page, honouring
    ``aliases`` frontmatter. Highest severity.
3.  **Summary gloss** -- every reference page (``entity``/``note``/``memory``) carries a
    non-empty one-line ``summary:`` frontmatter gloss (issue #72 / ADR 0008): the
    canonical, rebuildable home of the per-page gloss that replaced the old
    agent-maintained ``index.md`` catalog.
4.  **Frontmatter validation** -- required common fields present, ``type`` valid,
    type-specific required fields present, and ``status`` / ``priority`` /
    ``media_type`` values within the Metadata-Menu vocabularies.
5.  **Stale content** -- a knowledge page whose ``updated`` is older than
    :data:`STALE_DAYS`; an ``action`` past its ``due_date`` and not done/cancelled; a
    ``media`` ``to_consume`` older than :data:`MEDIA_STALE_DAYS`.
6.  **Contradictions** -- every page with ``contested: true`` or a non-empty
    ``contradictions:`` list.
7.  **Source drift** -- a ``raw/`` page whose recomputed body sha256 differs from its
    stored ``sha256`` frontmatter.
8.  **Quality signals** -- ``confidence: low`` pages and single-source pages with no
    ``confidence``.
9.  **Page size** -- curated pages whose body exceeds :data:`PAGE_SIZE_LIMIT` lines.
10. **Tag audit** -- every tag in use must appear in ``SCHEMA.md``'s
    ``## Tag Taxonomy`` section.
11. **Image hygiene** -- orphan binaries in ``raw/assets/`` with no embed anywhere,
    pages embedding a missing asset, and surviving per-image sidecar ``.md`` files.
12. **Log rotation** -- a ``log.md`` with more than :data:`LOG_ROTATE_LIMIT` entries.
13. **Report + log** -- group by severity and append one ``log.md`` line.

All folder / type / slug contract constants are imported from :mod:`thoth.vault` so the
closed-surface contract stays single-sourced; the Metadata-Menu vocabularies (which the
vault writer does not enforce) are defined here from the SPEC frontmatter table. The
only injected non-determinism is ``today`` (a :class:`~datetime.date`) so the
stale / overdue / media-cold windows are reproducible under a frozen clock.

Only the standard library plus ``frontmatter`` / ``yaml`` and import-light ``thoth``
modules (the frozen :class:`thoth.config.Config` / :class:`thoth.vault.Vault`, the
shared time/field helpers, and :mod:`thoth.summary` for the media-status vocabulary)
are imported at module level, so importing this package at pytest collection is always
CI-safe.
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
from .parse import extract_embeds, extract_wikilinks, parse_taxonomy_tags

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
    "extract_wikilinks",
    "extract_embeds",
]

# CURATED_DIRS / ACTIONABLE_DIRS are the canonical folder vocabulary owned by
# thoth.vault (ADR 0005); they are imported above and re-exported here so the __all__
# surface and lint consumers derive the same list instead of restating it.
# "Curated page" means a lifecycle-free reference page in one of the CURATED_DIRS
# folders (entities/notes/memories): the orphan, index-completeness, page-size and
# quality-signal checks scope to these. The ACTIONABLE_DIRS pages (actions/, which also
# holds the media queue as actions tagged 'media') are exempt from the orphan /
# index-completeness checks (Bases dashboards surface them) but still carry the common
# frontmatter contract and get the overdue / cold-media checks instead.
