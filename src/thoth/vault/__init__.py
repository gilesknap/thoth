"""The closed, path-confined read and write surface over the Obsidian vault.

This package is the security core of the appliance (SPEC section 3). The LLM never
gets a shell or arbitrary file access, so every byte that reaches the vault passes
through the helpers here. They:

(a) confine a path to the resolved vault root, rejecting anything that resolves
outside it, catching an absolute path, a ``..`` segment and a symlink escape *before*
any disk is touched; (b) validate the folder-by-type contract and the slug and
asset-filename grammar; (c) read and write YAML frontmatter through
``python-frontmatter`` and ``pyyaml``; (d) stamp the required ``created`` and
``updated`` fields, plus ``ingested`` and ``sha256`` for raw; (e) make append-only,
deduplicated edits to ``log.md``; (f) move a binary asset into ``raw/assets/``, never
as base64; and (g) redact a secret-looking string from body and frontmatter before
filing.

The package is pure filesystem and fully unit-testable on a temporary vault. It reuses
the frozen :class:`thoth.config.Config` for the vault root and name, and delegates the
single canonical ``obsidian://`` link encoding to :meth:`Config.obsidian_uri`. The
confinement check lives here, so there is exactly one encoder and one confiner.

Module level imports only the standard library, ``frontmatter`` and ``yaml``, so
importing this package is always CI-safe.

This package is also the single canonical source of the page-type, source and folder
vocabulary (issue #19), which :mod:`thoth.vault.contract` documents.

The submodules split the surface by responsibility. :mod:`thoth.vault.contract` holds
the vocabulary constants and the slug grammar, :mod:`thoth.vault.redact` the secret
redaction, and :mod:`thoth.vault.core` the errors, the page records and the
:class:`Vault` facade. Everything is re-exported here, so ``thoth.vault`` remains the
one import path.
"""

from .contract import (
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
