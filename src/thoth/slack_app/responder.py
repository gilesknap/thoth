"""The placeholder-then-edit reply seam over the Slack web client (issue #34)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from thoth.render import SlackPoster


class SlackClientLike(SlackPoster, Protocol):
    """The slice of the Bolt web client used by the handlers.

    Extends the shared :class:`thoth.render.SlackPoster` with the in-place edit used
    by the placeholder flow.
    """

    def chat_update(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, ts: str, text: str, **kwargs: Any
    ) -> Any:
        """Edit a previously-posted message in place (Slack ``chat.update``)."""
        ...


# Placeholder lines shown the instant a slow request is received (issue #34, Slice B),
# so a multi-second capture/answer is not a dead pause. They are edited in place (via
# chat.update) with the final render once the work completes.
_INGEST_PLACEHOLDER: str = ":hourglass_flowing_sand: Filing…"
_ASK_PLACEHOLDER: str = ":mag: Looking…"


class Responder:
    """The reply seam for one message: an immediate placeholder, then a final edit.

    A multi-second capture/answer (a ``git pull`` -> classify -> extract -> curate ->
    Hindsight retain+probe -> commit+push chain, easily 5-15s) shows nothing until done
    if the handler only ``say()``s once at the end. This object (issue #34, Slice B)
    posts an immediate placeholder via the Slack web client, remembers its message
    ``ts``, and edits that same message in place with the final render (``chat.update``)
    -- so the user sees "Filing…" within ~1s and it resolves to the report, with no
    second message.

    Every reply is posted **in the conversation thread** (issue #61): the placeholder
    carries ``thread_ts`` and the bare-``say`` fallback threads its reply too, so a
    reply lands under the originating top-level message, not at channel top level. The
    in-place edit (``chat.update``) targets the placeholder's own ``ts`` and so stays in
    the thread automatically.

    It degrades cleanly: when no web ``client`` or ``channel`` is available (the
    text-only/test paths that pass only a bare ``say``), :meth:`progress` posts nothing
    and :meth:`finish` falls back to a single ``say(text)`` (still threaded) -- the
    exact pre-#34 behaviour. So the placeholder+update is best-effort UX over the
    existing single-``say`` contract, never a hard dependency on the client.
    """

    def __init__(
        self,
        say: Callable[..., None],
        *,
        client: SlackClientLike | None = None,
        channel: str = "",
        thread_ts: str = "",
    ) -> None:
        """Build a responder over a ``say`` callable and an optional web client+channel.

        Args:
            say: The Bolt ``say`` callable that posts a reply to the conversation; it
                accepts an optional ``thread_ts`` keyword so a reply can be threaded.
            client: The Slack web client used to post + edit the placeholder; ``None``
                disables the placeholder (the single-``say`` fallback).
            channel: The conversation id the placeholder is posted to / edited in; an
                empty id also disables the placeholder.
            thread_ts: The thread root to post replies under (``thread_ts or ts`` of the
                originating message, issue #61). Empty means post at channel top level
                (the test/edge paths); production always supplies it.
        """
        self._say = say
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: str | None = None

    def _emit(self, text: str) -> None:
        """Post a fresh reply via the bare ``say``, threading it when set."""
        self._say(text, **self._thread_kwargs())

    def _thread_kwargs(self) -> dict[str, str]:
        """The ``thread_ts`` kwargs for a client post, or ``{}`` at top level."""
        return {"thread_ts": self._thread_ts} if self._thread_ts else {}

    def say(self, text: str) -> None:
        """Post ``text`` as a plain threaded reply (early conflict/error/refusal)."""
        self._emit(text)

    def progress(self, placeholder: str) -> None:
        """Post an immediate placeholder (best-effort); remember its ts for the edit.

        Posts into the conversation thread (``thread_ts``) so the working signal appears
        under the originating message. With no client/channel, or if the post fails for
        any reason, this no-ops and a later :meth:`finish` falls back to a single
        ``say`` -- the placeholder must never be able to swallow the real reply.

        The ts is read by duck-typing ``response.get("ts")`` rather than requiring a
        ``dict``: the real ``slack_sdk`` ``WebClient`` returns a ``SlackResponse`` (a
        dict-*like* object that is **not** a ``dict`` subclass), so an ``isinstance(...,
        dict)`` guard would silently drop the ts against the live client -- leaving
        :meth:`update`/:meth:`finish` with no placeholder to edit and degrading every
        in-place edit to a separate message.
        """
        if self._client is None or not self._channel:
            return
        try:
            response = self._client.chat_postMessage(
                channel=self._channel, text=placeholder, **self._thread_kwargs()
            )
            ts = response.get("ts")
        except Exception:  # noqa: BLE001 - placeholder is best-effort UX, never fatal
            return
        if isinstance(ts, str) and ts:
            self._ts = ts

    def update(self, text: str) -> None:
        """Edit the placeholder in place with intermediate progress (best-effort).

        Used to stream per-phase progress (issue #137) into the same "Filing…"
        message as ingest moves through its passes -- the placeholder ts captured by
        :meth:`progress` is re-edited via ``chat.update`` so the user sees a live
        phase line without any extra messages.

        Unlike :meth:`finish`, an intermediate update **never** falls back to a fresh
        ``say``: with no client/channel/ts (a client-less/test path, or the placeholder
        post failed) it no-ops, and a failed edit is swallowed. An intermediate update
        must never be able to spam the thread or break ingest -- only the placeholder
        edit, best-effort.
        """
        if self._client is None or not self._channel or self._ts is None:
            return
        try:
            self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
        except Exception:  # noqa: BLE001 - intermediate progress is best-effort, never fatal
            return

    def finish(self, text: str) -> None:
        """Deliver the final reply: edit the placeholder in place, else a fresh ``say``.

        When a placeholder ts was captured the message is edited via ``chat.update`` (so
        the "Filing…" line becomes the report; the edit stays in-thread by targeting
        that ts). When there is no placeholder -- no client, the post failed, or a
        client-less path -- it falls back to a threaded ``say(text)``, the single-reply
        pre-#34 behaviour. A failed edit also falls back to ``say`` so the user always
        gets the reply.
        """
        if self._client is not None and self._channel and self._ts is not None:
            try:
                self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
            except Exception:  # noqa: BLE001 - fall back to a fresh post on any edit error
                self._emit(text)
            return
        self._emit(text)
