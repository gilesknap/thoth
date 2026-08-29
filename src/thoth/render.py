"""The single, dependency-free formatter for a vault-file reference in Slack ``mrkdwn``.

Every place thoth names a vault page **to Slack** renders it through
:func:`render_vault_ref`, so the on-Slack format lives in exactly one place (issue #53):
a Q&A ``Sources:`` list, a capture confirmation, a save confirmation, a daily or weekly
digest line. The format is the concise, Slack-native one, a single clickable
``<obsidian-uri|title>`` link and nothing more, since issue #63 dropped the trailing
vault-relative path as noise next to a clickable title that no reader wants to copy.

There is deliberately **no** ``[[wikilink]]`` and no category label, because a wikilink
is dead, un-clickable text in Slack. A wikilink is still correct *vault body* content,
written elsewhere, and this module covers Slack output only.

This module imports nothing from the rest of ``thoth``, and nothing from
:mod:`thoth.slack_app` or :mod:`thoth.summary`, which both import *it*, so it can be the
shared leaf with no risk of an import cycle, and the home of :class:`SlackPoster`, the
one ``chat.postMessage`` protocol that every Slack output surface shares.
"""

from __future__ import annotations

from typing import Any, Protocol


class SlackPoster(Protocol):
    """The ``chat.postMessage`` slice of the Slack web client.

    The one poster protocol that every Slack output surface shares.
    :class:`thoth.summary.SlackPoster` and :class:`thoth.alerts.AlertPoster` re-export
    it, and :class:`thoth.slack_app.SlackClientLike` extends it. The real Bolt
    ``WebClient`` and a test fake both satisfy it, so none of those consumers imports a
    Slack SDK.
    """

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post ``text`` to ``channel`` through the Slack ``chat.postMessage`` API."""
        ...


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    Emits ``<obsidian-uri|title>``, a single clickable ``title`` linked to the
    harness-built ``obsidian://`` deep link, taking ``obsidian_uri`` verbatim from the
    caller and never fabricating a link. The same shape serves a web citation, so pass
    the URL as ``obsidian_uri`` and the page title as ``title``.

    The visible label falls back to ``path``, and then to ``obsidian_uri``, when
    ``title`` is empty or blank, so the link can never render as ``<uri|>``, an
    invisible, unclickable label in Slack (issue #67).

    Args:
        obsidian_uri: The link target for the clickable label: an ``obsidian://`` deep
            link for a vault page, or a plain URL for a web citation.
        title: The human-readable label for the link.
        path: The vault-relative path. Issue #63 dropped it as a trailing suffix, but
            it is still the label when ``title`` is blank (issue #67).

    Returns:
        A single ``mrkdwn`` link of the form ``<obsidian_uri|label>``, where ``label``
        is ``title`` when present, else ``path``, else ``obsidian_uri``.
    """
    label = title.strip() or path.strip() or obsidian_uri
    return f"<{obsidian_uri}|{label}>"
