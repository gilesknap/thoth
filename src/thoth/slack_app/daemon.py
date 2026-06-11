"""The Socket-Mode entry points: build the Bolt app, serve it, alert on a crash."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from thoth.config import Config
from thoth.ingest import Ingestor
from thoth.query import QueryEngine

from .handlers import AlerterLike, _build_handlers


def build_app(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
) -> Any:
    """Lazily import ``slack_bolt``, build the App, and register the handlers.

    ``slack_bolt`` is imported **inside** this function so module import stays CI-safe.
    The :class:`~thoth.slack_app.Handlers` graph (and the fail-fast required-config
    checks, including the dedicated ``SLACK_CAPTURE_CHANNEL`` the daemon listens/replies
    in, issue #61) is built by :func:`~thoth.slack_app.handlers._build_handlers` --
    factored out so that wiring is testable without ``slack_bolt``. The returned app
    delegates the ``message`` listener (which also carries file uploads, as a
    ``file_share`` subtype) to those handlers. The bare ``file_shared`` event is bound
    to a no-op (it is a stub the appliance ignores -- see
    :meth:`~thoth.slack_app.Handlers.handle_message`). The app is **not** started --
    :func:`run` does that. Free-text questions take the vault-only query path.

    Args:
        config: The frozen runtime config (provides the Slack bot token + capture
            channel).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.

    Returns:
        The configured ``slack_bolt.App`` instance (typed ``Any`` to avoid a top-level
        import of the optional dependency).
    """
    from slack_bolt import App

    handlers, bot_token = _build_handlers(config, ingestor, query_engine)
    app = App(token=bot_token)

    @app.event("message")
    def _on_message(
        event: dict[str, Any], client: Any, say: Callable[..., None]
    ) -> None:
        handlers.handle_message(event, say, client=client)

    # Slack emits a separate ``file_shared`` event for every upload, but it embeds only
    # a ``{"id": ...}`` stub (no download URL) and no conversation ``channel`` to reply,
    # so uploads are ingested from the ``message``/``file_share`` event above instead.
    # This no-op listener exists solely so Bolt does not log each such event as an
    # unhandled request (Bolt auto-acks it).
    @app.event("file_shared")
    def _on_file_shared(event: dict[str, Any]) -> None:
        return None

    return app


def run(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
) -> None:
    """Build the app and block serving over Socket Mode (the daemon entry point).

    Lazily imports ``SocketModeHandler``, builds the app via :func:`build_app`, and
    calls ``handler.start()`` which blocks forever. This is the production entry point
    (``thoth slack``) and is never unit-tested live (CI has no Slack socket); the
    testable logic all lives on :class:`~thoth.slack_app.Handlers`.

    Unattended observability (issue #15): the blocking serve is wrapped by
    :func:`serve_with_alerting` so an **unhandled** daemon exception is reported to the
    errors-to-Slack target (:class:`thoth.alerts.Alerter`) before the process exits and
    systemd restarts it -- otherwise a crash loop would be silent. The alert is
    best-effort and the original exception is always re-raised so systemd still sees the
    non-zero exit.

    Args:
        config: The frozen runtime config (provides both Slack tokens).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from thoth.alerts import make_alerter

    _, app_token = config.require_slack()
    app = build_app(config, ingestor, query_engine)
    alerter = make_alerter(config)
    serve_with_alerting(
        lambda: SocketModeHandler(app, app_token).start(),
        alerter,
    )


def serve_with_alerting(serve: Callable[[], None], alerter: AlerterLike) -> None:
    """Run ``serve`` (a blocking daemon loop), alerting on an unhandled exception.

    The top-level supervision seam (issue #15), factored out of :func:`run` so it is
    unit-testable without a real Slack socket: it invokes ``serve`` and, if it raises,
    posts an unhandled-exception alert via ``alerter`` (best-effort -- the alert post
    swallows its own errors) and then **re-raises** the original exception so the
    process still exits non-zero and systemd restarts (and rate-limits) it.

    A clean shutdown is *not* an incident: ``KeyboardInterrupt`` / ``SystemExit`` (how
    ``systemctl stop`` and a deploy restart unwind the blocking loop) re-raise silently,
    so a routine stop/restart does not post an alert (which would train the operator to
    ignore them). Only genuine crashes -- any other exception -- alert.

    Args:
        serve: The blocking daemon entry (e.g. ``SocketModeHandler(...).start``).
        alerter: The errors-to-Slack alerter (a :class:`thoth.alerts.Alerter`).
    """
    try:
        serve()
    except (KeyboardInterrupt, SystemExit):
        # A clean stop (SIGTERM/Ctrl-C) is not a crash -- exit quietly, no alert.
        raise
    except BaseException as exc:  # noqa: BLE001 - report ANY real crash, then re-raise
        alerter.alert_exception("slack daemon", exc)
        raise
