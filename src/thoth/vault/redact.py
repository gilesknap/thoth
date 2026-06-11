"""Secret redaction applied to body and frontmatter before filing (SPEC section 12)."""

from __future__ import annotations

import re

# Token-shaped patterns. Each is conservative: it matches a recognisable provider
# prefix followed by a run of token characters, or a labelled secret assignment, or a
# long opaque hex/base64 blob. Ordinary prose and short words never match.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider-prefixed API keys: sk-..., sk-ant-..., ghp_/gho_/ghs_..., xoxb-/xapp-...
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Bearer <token> authorization headers.
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{10,}"),
    # key=VALUE / key: VALUE for a sensitive key name.
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b"
        r"\s*[:=]\s*\S{6,}"
    ),
    # Long opaque hex blob (e.g. a 32+ char digest used as a credential).
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # Long opaque base64-ish blob (mixed case + digits, no spaces).
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings with a fixed ``[REDACTED]`` marker.

    Masks provider-prefixed API keys (``sk-...``, ``ghp_...``), AWS access key ids
    (``AKIA...``), ``Bearer <token>`` headers, ``key=VALUE`` assignments for a
    sensitive key set, and long opaque hex/base64 blobs. The match is conservative so
    ordinary prose and short words are left untouched. Applied to body and frontmatter
    before filing (SPEC section 12). Never raises; a non-string input is returned
    unchanged.
    """
    if not isinstance(text, str):
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


# Writer-controlled structural fields: generated from validated inputs (dates and the
# body digest), never user free-text, so they are exempt from redaction. The sha256
# digest in particular is a 64-char hex string the long-hex-blob rule would else mask.
_NEVER_REDACT_FIELDS: frozenset[str] = frozenset(
    {"created", "updated", "ingested", "sha256"}
)


def _redact_frontmatter(meta: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``meta`` with secrets redacted from string values.

    Recurses into list and dict values; non-string scalars (dates, ints, bools) are
    preserved as-is so frontmatter typing and date stamping are not disturbed.
    Writer-controlled structural fields (:data:`_NEVER_REDACT_FIELDS`) are passed
    through verbatim so the generated ``sha256`` digest is not mistaken for a secret.
    """
    return {
        key: (value if key in _NEVER_REDACT_FIELDS else _redact_value(value))
        for key, value in meta.items()
    }


def _redact_value(value: object) -> object:
    """Redact secrets from a frontmatter value, recursing into lists and dicts."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value
