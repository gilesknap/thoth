"""Pure helpers that read routing facts off a raw Slack ``message`` event."""

from __future__ import annotations

from typing import Any

# A stripped body starting with one of these is an explicit "save this thought"
# signal, routed to ingest-as-text rather than query
_CAPTURE_PREFIXES: tuple[str, ...] = ("capture:", "note:", "save:")


def _channel(event: dict[str, Any]) -> str:
    """Returns the event's channel id, where a reply is posted and the gate target."""
    value = event.get("channel")
    return value if isinstance(value, str) else ""


def _conversation_key(event: dict[str, Any]) -> str:
    """Returns the conversation thread key for the event, ``thread_ts or ts``.

    This is the per-conversation state key (issue #61) and the ``thread_ts`` the bot
    replies under. A reply inside a thread carries the thread root's ``thread_ts``, so a
    follow-up keys to the same conversation as the top-level message, which has only its
    own ``ts`` until the bot replies under it. There is no fallback to the bare channel,
    which would reintroduce the cross-topic collision issue #61 exists to remove.
    """
    thread_ts = event.get("thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        return thread_ts
    ts = event.get("ts")
    return ts if isinstance(ts, str) else ""


def _should_handle(event: dict[str, Any]) -> bool:
    """Drops bot messages and echoes, and every subtype except ``file_share``.

    A plain top-level message and an in-thread reply both have no subtype, so both are
    handled, and the bot's own replies carry ``bot_id`` and are dropped here so the
    daemon never loops on them. Edits, deletes, joins and the thread-to-channel
    rebroadcast are all dropped.

    The one subtype kept is ``file_share``, a channel upload arriving as a ``message``
    that carries the full ``files`` objects and a usable ``channel``, which is the event
    an upload is ingested from. Slack also emits a separate ``file_shared``, but it
    embeds only an id stub with no URL and no conversation to reply in, so the appliance
    ignores it and there is no cross-handler double-processing.
    """
    if event.get("bot_id"):
        return False
    subtype = event.get("subtype")
    if subtype and subtype != "file_share":
        return False
    return True


def _event_key(event: dict[str, Any]) -> str:
    """Picks the most stable redelivery key Slack offers for this event."""
    for key in ("event_id", "client_msg_id", "file_id", "ts"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _looks_like_url(text: str) -> bool:
    """True only when the whole message is a single ``http(s)`` URL."""
    if " " in text or "\n" in text:
        return False
    return text.startswith("http://") or text.startswith("https://")


def _capture_body(text: str) -> str | None:
    """Returns the body of an explicitly-prefixed capture message, else ``None``.

    A leading ``capture:``, ``note:`` or ``save:`` marker is stripped and the remainder,
    possibly empty, returned. Text without a marker is not an explicit capture and
    routes elsewhere.
    """
    lowered = text.lower()
    for prefix in _CAPTURE_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip()
    return None
