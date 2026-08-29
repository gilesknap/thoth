"""The 13 SPEC section 11 and Appendix maintenance checks, as a pure vault scan.

This package is the appliance's deterministic maintenance pass (SPEC section 11 and
the Appendix "Lint checks" table). It is a *pure programmatic markdown scan* over a
real :class:`thoth.vault.Vault`, with no network, no LLM and no subprocess. Each of
checks 1-12 is a method returning ``list[Finding]``, and :meth:`LintEngine.run`
aggregates them into a :class:`LintReport` grouped and counted by :class:`Severity`.
Check 13, :meth:`LintEngine.record`, appends **exactly one** ``log.md`` entry through
:meth:`thoth.vault.Vault.append_log`, carrying the issue count.

The 13 checks (SPEC Appendix table):

1.  **Orphan pages**: a curated knowledge page with zero inbound ``[[wikilinks]]``. A
    life-admin page is exempt, because Bases surface it.
2.  **Broken wikilinks**: a ``[[target]]`` reference resolving to no page, honouring
    ``aliases`` frontmatter. Highest severity.
3.  **Summary gloss**: every content page, all four types including ``action`` since
    ADR 0013, carries a non-empty one-line ``summary:`` frontmatter gloss (issue #72,
    ADR 0008). That gloss is the canonical, rebuildable home that replaced the old
    agent-maintained ``index.md`` catalog.
4.  **Frontmatter validation**: the required fields are present, a content page
    against :data:`~thoth.vault.CONTENT_COMMON_FIELDS` and an inbox hold against
    :data:`~thoth.vault.INBOX_REQUIRED_FIELDS`, the ``type`` is valid, the
    type-specific required fields are present, ``personal`` is a real boolean, and
    the ``status``, ``kind``, ``priority`` and ``media_type`` values sit within the
    vault vocabularies.
5.  **Stale content**: a knowledge page whose ``updated`` is older than
    :data:`STALE_DAYS`, an ``action`` past its ``due_date`` and neither done nor
    cancelled, or a ``kind: media`` action still ``todo`` and older than
    :data:`MEDIA_STALE_DAYS`.
6.  **Contradictions**: every page with ``contested: true`` or a non-empty
    ``contradictions:`` list.
7.  **Source drift**: a ``raw/`` page whose recomputed body sha256 differs from its
    stored ``sha256`` frontmatter.
8.  **Quality signals**: a ``confidence: low`` page, and a single-source page with no
    ``confidence``.
9.  **Page size**: a curated page whose body exceeds :data:`PAGE_SIZE_LIMIT` lines.
10. **Tag audit**: every tag in use must appear in ``SCHEMA.md``'s
    ``## Tag Taxonomy`` section.
11. **Image hygiene**: an orphan binary in ``raw/assets/`` with no embed anywhere, a
    page embedding a missing asset, and a surviving per-image sidecar ``.md`` file.
12. **Log rotation**: a ``log.md`` with more than :data:`LOG_ROTATE_LIMIT` entries.
13. **Report and log**: group by severity, then append one ``log.md`` line.

Every folder, type and slug contract constant, AND the ``status``, ``kind``,
``priority`` and ``media_type`` vocabularies, are imported from :mod:`thoth.vault`, so
the closed-surface contract stays single-sourced (ADR 0013). The only injected
non-determinism is ``today``, a :class:`~datetime.date`, so the stale, overdue and
media-cold windows are reproducible under a frozen clock.

Module level imports only the standard library, ``frontmatter``, ``yaml`` and
import-light ``thoth`` modules, so importing this package at pytest collection is
always CI-safe. Those thoth modules are the frozen :class:`thoth.config.Config` and
:class:`thoth.vault.Vault`, the shared time and field helpers, and
:mod:`thoth.summary` for the media-status vocabulary.
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

# CURATED_DIRS / ACTIONABLE_DIRS are the canonical folder vocabulary owned by
# thoth.vault (ADR 0005); they are imported above and re-exported here so the __all__
# surface and lint consumers derive the same list instead of restating it.
# "Curated page" means a lifecycle-free reference page in one of the CURATED_DIRS
# folders (entities/notes/memories): the orphan, index-completeness, page-size and
# quality-signal checks scope to these. The ACTIONABLE_DIRS pages (actions/, which also
# holds the media queue as actions with kind: media) are exempt from the orphan /
# index-completeness checks (Bases dashboards surface them) but still carry the common
# frontmatter contract + summary gloss and get the overdue / cold-media checks instead.
