"""The closed, path-confined read/write surface over the Obsidian vault.

This package is the security core of the appliance (SPEC section 3): the LLM never
gets a shell or arbitrary file access, so every byte that reaches the vault passes
through the helpers here. They (a) confine paths to the resolved vault root, rejecting
anything that resolves outside it (absolute paths, ``..`` segments, and symlink
escapes are all caught *before* any disk is touched); (b) validate the folder-by-type
contract and the slug/asset-filename grammar; (c) read and write YAML frontmatter via
``python-frontmatter`` + ``pyyaml``; (d) stamp the required ``created``/``updated``
(and ``ingested``/``sha256`` for raw) fields; (e) make append-only, deduplicated edits
to ``log.md``; (f) move binary assets into ``raw/assets/`` (never
base64); and (g) redact secret-looking strings from body and frontmatter before
filing.

The package is pure filesystem and fully unit-testable on a temporary vault. It reuses
the frozen :class:`thoth.config.Config` for the vault root and name, and delegates the
single canonical ``obsidian://`` link encoding to :meth:`Config.obsidian_uri`; the
confinement check lives here so there is exactly one encoder and one confiner.

Only the standard library plus ``frontmatter`` and ``yaml`` are imported at module
level, so importing this package is always CI-safe.

This package is also the single canonical source of the page-type / source / folder
vocabulary (issue #19): the classify prompt (:mod:`thoth.ingest`), the lint folder walks
(:mod:`thoth.lint`), the summary scans (:mod:`thoth.summary`) and the file-plan
validator (:mod:`thoth.llm`) all import these constants rather than restating them.

The submodules split the surface by responsibility: :mod:`thoth.vault.contract` (the
vocabulary constants and the slug grammar), :mod:`thoth.vault.redact` (secret
redaction), and :mod:`thoth.vault.core` (the errors, the page records, and the
:class:`Vault` facade). Everything is re-exported here, so ``thoth.vault`` remains the
one import path.
"""

from .contract import (
    ACTION_KIND_VOCAB,
    ACTION_STATUS_VOCAB,
    ACTIONABLE_DIRS,
    ASSET_SLUG_RE,
    CONTENT_COMMON_FIELDS,
    CURATED_DIRS,
    FOLDER_TYPE_CONTRACT,
    INBOX_REQUIRED_FIELDS,
    INBOX_TYPE,
    MEDIA_TYPE_VOCAB,
    PRIORITY_VOCAB,
    RAW_SUBDIRS,
    REFERENCE_TYPES,
    REQUIRED_COMMON_FIELDS,
    SEED_DIRS,
    SLUG_RE,
    SUMMARY_TYPES,
    TYPE_ENUMERATION,
    VALID_SOURCES,
    VALID_TYPES,
)
from .core import (
    Page,
    PathConfinementError,
    SchemaError,
    SeedResult,
    SlugError,
    Vault,
    VaultError,
)
from .redact import redact_secrets

__all__ = [
    "ACTION_KIND_VOCAB",
    "ACTION_STATUS_VOCAB",
    "ACTIONABLE_DIRS",
    "ASSET_SLUG_RE",
    "CONTENT_COMMON_FIELDS",
    "CURATED_DIRS",
    "FOLDER_TYPE_CONTRACT",
    "INBOX_REQUIRED_FIELDS",
    "INBOX_TYPE",
    "MEDIA_TYPE_VOCAB",
    "PRIORITY_VOCAB",
    "RAW_SUBDIRS",
    "REFERENCE_TYPES",
    "REQUIRED_COMMON_FIELDS",
    "SEED_DIRS",
    "SLUG_RE",
    "SUMMARY_TYPES",
    "TYPE_ENUMERATION",
    "VALID_SOURCES",
    "VALID_TYPES",
    "Page",
    "PathConfinementError",
    "SchemaError",
    "SeedResult",
    "SlugError",
    "Vault",
    "VaultError",
    "redact_secrets",
]
