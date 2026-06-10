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
shared leaf with no risk of an import cycle. The same leaf role makes it the home of
:class:`SlackPoster`, the one ``chat.postMessage`` protocol every Slack output surface
shares.
"""

from __future__ import annotations

from typing import Any, Protocol


class SlackPoster(Protocol):
    """The ``chat.postMessage`` slice of the Slack web client.

    The one poster protocol shared by every Slack output surface: re-exported as
    :class:`thoth.summary.SlackPoster` and :class:`thoth.alerts.AlertPoster`, and
    extended by :class:`thoth.slack_app.SlackClientLike`. The real Bolt ``WebClient``
    and a test fake both satisfy it, so none of those consumers imports a Slack SDK.
    """

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post ``text`` to ``channel`` (the Slack ``chat.postMessage`` API)."""
        ...


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    Emits ``<obsidian-uri|title>`` -- a single clickable ``title`` linking to the
    harness-built ``obsidian://`` deep link, with no trailing path or label (issue #63:
    title-only is the chosen default). The same shape serves a web citation (pass the
    URL as ``obsidian_uri`` and the page title as ``title``). The ``obsidian_uri`` is
    taken verbatim from the caller; this function never fabricates a link.

    The visible label falls back to ``path`` and then ``obsidian_uri`` when ``title`` is
    empty or blank, so the link can never render as ``<uri|>`` -- an invisible,
    unclickable label in Slack (issue #67).

    Args:
        obsidian_uri: The link target for the clickable label (an ``obsidian://`` deep
            link for a vault page, or a plain URL for a web citation).
        title: The human-readable label for the link.
        path: The vault-relative path, no longer rendered as a trailing suffix (issue
            #63), but used as the visible label when ``title`` is blank (issue #67).

    Returns:
        A single ``mrkdwn`` link of the form ``<obsidian_uri|label>``, where ``label``
        is ``title`` if present, else ``path``, else ``obsidian_uri``.
    """
    label = title.strip() or path.strip() or obsidian_uri
    return f"<{obsidian_uri}|{label}>"
