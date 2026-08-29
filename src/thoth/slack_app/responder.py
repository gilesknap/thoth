"""The placeholder-then-edit reply seam over the Slack web client (issue #34)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from thoth.render import SlackPoster


class SlackClientLike(SlackPoster, Protocol):
    """The slice of the Bolt web client the handlers use.

    Extends the shared poster with the in-place edit the placeholder flow needs.
    """

    def chat_update(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, ts: str, text: str, **kwargs: Any
    ) -> Any:
        """Edits a previously-posted message in place."""
        ...


# Placeholder lines shown the instant a slow request arrives (issue #34), so a
# multi-second capture is not a dead pause. They are edited in place with the final
# render once the work completes
_INGEST_PLACEHOLDER: str = ":hourglass_flowing_sand: Filing…"
_ASK_PLACEHOLDER: str = ":mag: Looking…"


class Responder:
    """The reply seam for one message: an immediate placeholder, then a final edit.

    A capture runs pull, classify, extract, curate, retain and push, easily 5 to 15
    seconds, and shows nothing until done if the handler only speaks once at the end. So
    this posts an immediate placeholder, remembers its timestamp, and edits that same
    message with the final render (issue #34).

    The user sees a working signal within a second and it resolves in place, with no
    second message.

    Every reply is posted in the conversation thread (issue #61), so it lands under the
    originating message rather than at channel top level. The in-place edit targets the
    placeholder's own timestamp and stays in the thread automatically.

    It degrades cleanly. With no client or channel, which is the text-only test path,
    the placeholder posts nothing and the final reply falls back to a single threaded
    say. So this is best-effort UX over the existing contract, never a hard dependency
    on the client.
    """

    def __init__(
        self,
        say: Callable[..., None],
        *,
        client: SlackClientLike | None = None,
        channel: str = "",
        thread_ts: str = "",
    ) -> None:
        """Builds a responder over a say callable and an optional client and channel.

        Args:
            say: The Bolt callable posting a reply, accepting an optional thread.
            client: Web client used to post and edit the placeholder. None disables
                the placeholder and falls back to a single say.
            channel: Conversation id the placeholder is posted to. Empty also
                disables it.
            thread_ts: Thread root to reply under (issue #61). Empty posts at channel
                top level, which is the test path, and production always supplies it.
        """
        self._say = say
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: str | None = None

    def _emit(self, text: str) -> None:
        """Posts a fresh reply through the bare say, threading it when set."""
        self._say(text, **self._thread_kwargs())

    def _thread_kwargs(self) -> dict[str, str]:
        """The thread kwargs for a client post, or empty at top level."""
        return {"thread_ts": self._thread_ts} if self._thread_ts else {}

    def say(self, text: str) -> None:
        """Posts text as a plain threaded reply, for an early error or refusal."""
        self._emit(text)

    def progress(self, placeholder: str) -> None:
        """Posts an immediate placeholder, best-effort, remembering its timestamp.

        It posts into the conversation thread so the working signal appears under the
        originating message. With no client or channel, or on any failure, this no-ops
        and a later finish falls back to a single say, because the placeholder must
        never swallow the real reply.

        The timestamp is read by duck-typing rather than requiring a dict. The real
        client returns a dict-like object that is not a dict subclass, so an isinstance
        guard would silently drop it against the live client and degrade every in-place
        edit to a separate message.
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
        """Edits the placeholder in place with intermediate progress, best-effort.

        Streams per-phase progress (issue #137) into the same message as ingest moves
        through its passes, so the user sees a live phase line with no extra messages.
        Unlike the final reply, an intermediate update never falls back to a fresh say.
        With no placeholder it no-ops and a failed edit is swallowed, because an
        intermediate update must never spam the thread or break an ingest.
        """
        if self._client is None or not self._channel or self._ts is None:
            return
        try:
            self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
        except Exception:  # noqa: BLE001 - intermediate progress is best-effort, never fatal
            return

    def finish(self, text: str) -> None:
        """Delivers the final reply, editing the placeholder or posting fresh.

        With a captured timestamp the message is edited in place, so the working line
        becomes the report and the edit stays in-thread. With no placeholder, or on a
        failed edit, it falls back to a threaded say, so the user always gets the reply.
        """
        if self._client is not None and self._channel and self._ts is not None:
            try:
                self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
            except Exception:  # noqa: BLE001 - fall back to a fresh post on any edit error
                self._emit(text)
            return
        self._emit(text)
