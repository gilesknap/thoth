"""The single, dependency-free formatter for a vault-file reference in Slack ``mrkdwn``.

Every place thoth names a vault page **to Slack** -- a Q&A ``Sources:`` list, a capture
confirmation, a save confirmation, a daily/weekly digest line -- renders it through
:func:`render_vault_ref` so the on-Slack format lives in exactly one place (issue #53).
The format is the concise, Slack-native one: a clickable ``<obsidian-uri|title>`` link
followed by the plain vault-relative path. There is deliberately **no** ``[[wikilink]]``
-- a wikilink is dead, un-clickable text in Slack (it remains correct *vault body*
content, written elsewhere; this module is only about Slack output).

This module imports nothing from the rest of ``thoth`` (and nothing from
:mod:`thoth.slack_app` / :mod:`thoth.summary`, which both import *it*) so it can be the
shared leaf with no risk of an import cycle.
"""

from __future__ import annotations


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    Emits ``<obsidian-uri|title>: path`` -- a clickable ``title`` linking to the
    harness-built ``obsidian://`` deep link, then the plain vault-relative path. The
    same shape serves a web citation (pass the URL as both ``obsidian_uri`` and
    ``path``). The ``obsidian_uri`` is taken verbatim from the caller; this function
    never fabricates a link.

    Args:
        obsidian_uri: The link target for the clickable label (an ``obsidian://`` deep
            link for a vault page, or a plain URL for a web citation).
        title: The human-readable label for the link.
        path: The plain reference shown after the link (a vault-relative path, or the
            URL again for a web citation).

    Returns:
        A single ``mrkdwn`` line of the form ``<obsidian_uri|title>: path``.
    """
    return f"<{obsidian_uri}|{title}>: {path}"
