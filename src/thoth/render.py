"""The single, dependency-free formatter for a vault-file reference in Slack ``mrkdwn``.

Every place thoth names a vault page **to Slack** renders it through
:func:`render_vault_ref`, so the on-Slack format lives in exactly one place (issue #53).
Those places are a Q&A ``Sources:`` list, a capture confirmation, a save confirmation
and a daily or weekly digest line.

The format is the concise, Slack-native one: a single clickable ``<obsidian-uri|title>``
link and nothing more. Issue #63 dropped the trailing vault-relative path. A title on
its own is the chosen default, because the path is noise next to a clickable title and
is never the thing the reader wants to copy. The output deliberately carries **no**
``[[wikilink]]`` and no category label, because a wikilink is dead, un-clickable text in
Slack. A wikilink stays correct content for a *vault body*, which other modules write.
This module covers Slack output only.

This module imports nothing from the rest of ``thoth``. It also imports nothing from
:mod:`thoth.slack_app` or :mod:`thoth.summary`, and both of those import *it*, so this
module can be the shared leaf with no risk of an import cycle. The same leaf role makes
it the home of :class:`SlackPoster`, the one ``chat.postMessage`` protocol that every
Slack output surface shares.
"""

from __future__ import annotations

from typing import Any, Protocol


class SlackPoster(Protocol):
    """The ``chat.postMessage`` slice of the Slack web client.

    Every Slack output surface shares this one poster protocol.
    :class:`thoth.summary.SlackPoster` and :class:`thoth.alerts.AlertPoster` re-export
    it, and :class:`thoth.slack_app.SlackClientLike` extends it. The real Bolt
    ``WebClient`` and a test fake both satisfy it, so none of those consumers imports a
    Slack SDK.
    """

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post ``text`` to ``channel`` (the Slack ``chat.postMessage`` API)."""
        ...


def render_vault_ref(*, obsidian_uri: str, title: str, path: str) -> str:
    """Render one vault-file reference as a concise Slack ``mrkdwn`` line (issue #53).

    The function emits ``<obsidian-uri|title>``, a single clickable ``title`` that links
    to the harness-built ``obsidian://`` deep link. It adds no trailing path and no
    label, because issue #63 chose a title on its own as the default. The same shape
    serves a web citation: pass the URL as ``obsidian_uri`` and the page title as
    ``title``. The function takes ``obsidian_uri`` verbatim from the caller and never
    fabricates a link.

    The visible label falls back to ``path``, and then to ``obsidian_uri``, when
    ``title`` is empty or blank. The fallback stops the link rendering as ``<uri|>``,
    which Slack shows as an invisible, unclickable label (issue #67).

    Args:
        obsidian_uri: The link target for the clickable label. Pass an ``obsidian://``
            deep link for a vault page, or a plain URL for a web citation.
        title: The human-readable label for the link.
        path: The vault-relative path. Issue #63 removed it as a trailing suffix, but it
            still supplies the visible label when ``title`` is blank (issue #67).

    Returns:
        A single ``mrkdwn`` link of the form ``<obsidian_uri|label>``. ``label`` is
        ``title`` when present, else ``path``, else ``obsidian_uri``.
    """
    label = title.strip() or path.strip() or obsidian_uri
    return f"<{obsidian_uri}|{label}>"
