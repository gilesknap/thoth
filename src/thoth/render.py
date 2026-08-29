"""The single, dependency-free formatter for a vault-file reference in Slack ``mrkdwn``.

Every place thoth names a vault page to Slack, from a Q&A ``Sources:`` list to a digest
line, renders it through :func:`render_vault_ref`, so the format lives in exactly one
place (issue #53). That format is a single clickable ``<obsidian-uri|title>`` and
nothing more. The trailing vault-relative path was dropped in issue #63, since it is
noise next to a clickable title and is never the thing the reader wants to copy, and
there is deliberately no ``[[wikilink]]``, which is dead un-clickable text in Slack.

This module imports nothing from the rest of ``thoth``, so it can be the shared leaf
with no risk of an import cycle. That same leaf role makes it the home of
:class:`SlackPoster`, the one ``chat.postMessage`` protocol every Slack output surface
shares.
"""

from __future__ import annotations

from typing import Any, Protocol


class SlackPoster(Protocol):
    """The ``chat.postMessage`` slice of the Slack web client.

    Re-exported as :class:`thoth.summary.SlackPoster` and
    :class:`thoth.alerts.AlertPoster`, and extended by
    :class:`thoth.slack_app.SlackClientLike`. The real Bolt ``WebClient`` and a test
    fake both satisfy it, so none of those consumers imports a Slack SDK.
    """

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post ``text`` to ``channel`` (the Slack ``chat.postMessage`` API)."""
        ...


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    Emits ``<obsidian-uri|title>`` with no trailing path or label (issue #63). The same
    shape serves a web citation, passing the URL as ``obsidian_uri``. The link target is
    taken verbatim from the caller, so this function never fabricates one.

    The visible label falls back to ``path`` and then ``obsidian_uri``, so it can never
    render as ``<uri|>``, which is invisible and unclickable in Slack (issue #67).

    Args:
        obsidian_uri: The link target, a deep link or a plain URL
        title: The human-readable label for the link
        path: The vault-relative path, used as the label when title is blank

    Returns:
        A single ``mrkdwn`` link of the form ``<obsidian_uri|label>``
    """
    label = title.strip() or path.strip() or obsidian_uri
    return f"<{obsidian_uri}|{label}>"
