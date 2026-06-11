"""Pure helpers that read routing facts off a raw Slack ``message`` event."""

from __future__ import annotations

from typing import Any

# A free-text message whose body, once stripped, begins with one of these prefixes is
# routed to ingest-as-text rather than query (an explicit "save this thought" signal).
_CAPTURE_PREFIXES: tuple[str, ...] = ("capture:", "note:", "save:")


def _channel(event: dict[str, Any]) -> str:
    """Return the event's channel id (where a reply is posted; the gate target)."""
    value = event.get("channel")
    return value if isinstance(value, str) else ""


def _conversation_key(event: dict[str, Any]) -> str:
    """Return the conversation thread key for the event: ``thread_ts or ts``.

    The per-conversation state key (issue #61), and the ``thread_ts`` the bot
    replies under: a reply *inside* a thread carries the thread root's ``thread_ts``
    (so a follow-up keys to the same conversation as the top-level one), while a
    top-level message has only its own ``ts`` (which becomes the thread root once
    the bot replies under it). No fallback to the bare channel -- that would
    reintroduce the cross-topic collision issue #61 exists to remove.
    """
    thread_ts = event.get("thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        return thread_ts
    ts = event.get("ts")
    return ts if isinstance(ts, str) else ""


def _should_handle(event: dict[str, Any]) -> bool:
    """Drop bot messages and echoes, and every subtype EXCEPT ``file_share``.

    A plain top-level message and an in-thread reply both have no subtype, so both
    are handled. The bot's own in-thread replies carry ``bot_id`` and are dropped
    here, so the daemon never loops on them. Channel subtypes -- edits/deletes
    (``message_changed`` / ``message_deleted``), joins/leaves (``channel_join`` when
    the bot is invited), and the thread-also-to-channel rebroadcast
    (``thread_broadcast``) -- are all dropped. The one subtype kept is
    ``file_share``: a channel file upload arrives as a ``message`` with that subtype
    carrying the **full** ``files`` objects (download URL + name) and a usable
    ``channel`` -- that is the event :meth:`~thoth.slack_app.Handlers.handle_message`
    ingests an upload from. Slack also emits a separate ``file_shared`` event, but it
    embeds only a ``{"id": ...}`` stub (no URL, no conversation ``channel`` to reply
    in), so the appliance ignores it (see :func:`~thoth.slack_app.build_app`) and
    there is no cross-handler double-processing.
    """
    if event.get("bot_id"):
        return False
    subtype = event.get("subtype")
    if subtype and subtype != "file_share":
        return False
    return True


def _event_key(event: dict[str, Any]) -> str:
    """Pick the most stable redelivery key Slack offers for this event."""
    for key in ("event_id", "client_msg_id", "file_id", "ts"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _looks_like_url(text: str) -> bool:
    """Return ``True`` iff the whole message is a single ``http(s)`` URL."""
    if " " in text or "\n" in text:
        return False
    return text.startswith("http://") or text.startswith("https://")


def _capture_body(text: str) -> str | None:
    """Return the body of an explicitly-prefixed capture message, else ``None``.

    A leading ``capture:``/``note:``/``save:`` marker is an explicit "save this
    thought" signal: the marker is stripped and the (possibly empty) remainder
    returned. Text without a marker yields ``None`` -- it is not an explicit
    capture and routes elsewhere.
    """
    lowered = text.lower()
    for prefix in _CAPTURE_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip()
    return None
