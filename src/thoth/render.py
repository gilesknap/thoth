"""The single, dependency-free formatter for a vault-file reference in Slack ``mrkdwn``.

Every place thoth names a vault page **to Slack** -- a Q&A ``Sources:`` list, a capture
confirmation, a save confirmation, a daily/weekly digest line -- renders it through
:func:`render_vault_ref` so the on-Slack format lives in exactly one place (issue #53).
The format is the concise, Slack-native one: a single clickable
``<obsidian-uri|title>`` link and nothing more. The trailing vault-relative path was
dropped (issue #63) -- title-only is the chosen default, as the path is noise next to a
clickable title and is never the thing the reader wants to copy. There is deliberately
**no** ``[[wikilink]]`` and no category label -- a wikilink is dead, un-clickable text
in Slack (it remains correct *vault body* content, written elsewhere; this module is
only about Slack output).

This module imports nothing from the rest of ``thoth`` (and nothing from
:mod:`thoth.slack_app` / :mod:`thoth.summary`, which both import *it*) so it can be the
shared leaf with no risk of an import cycle.
"""

from __future__ import annotations


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    Emits ``<obsidian-uri|title>`` -- a single clickable ``title`` linking to the
    harness-built ``obsidian://`` deep link, with no trailing path or label (issue #63:
    title-only is the chosen default). The same shape serves a web citation (pass the
    URL as ``obsidian_uri`` and the page title as ``title``). The ``obsidian_uri`` is
    taken verbatim from the caller; this function never fabricates a link.

    Args:
        obsidian_uri: The link target for the clickable label (an ``obsidian://`` deep
            link for a vault page, or a plain URL for a web citation).
        title: The human-readable label for the link.
        path: Accepted for call-site compatibility but no longer rendered (issue #63
            dropped the trailing path); kept so existing callers need not change.

    Returns:
        A single ``mrkdwn`` link of the form ``<obsidian_uri|title>``.
    """
    return f"<{obsidian_uri}|{title}>"
