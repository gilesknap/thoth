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
    """Lazily imports ``slack_bolt``, builds the App and registers the handlers.

    ``slack_bolt`` is imported inside this function so module import stays CI-safe. The
    handler graph, along with the fail-fast required-config checks including the
    dedicated ``SLACK_CAPTURE_CHANNEL`` the daemon listens and replies in (issue #61),
    is built by :func:`~thoth.slack_app.handlers._build_handlers`, factored out so that
    wiring is testable without ``slack_bolt``.

    The returned app delegates the ``message`` listener, which also carries file uploads
    as a ``file_share`` subtype, to those handlers, and binds the bare ``file_shared``
    stub to a no-op.

    The app is not started, since :func:`run` does that. Free-text questions take the
    vault-only query path.

    Args:
        config: The frozen runtime config, providing the bot token and capture channel
        ingestor: The constructed ingest pipeline
        query_engine: The constructed retrieval engine

    Returns:
        The configured ``slack_bolt.App``, typed ``Any`` to keep the import optional
    """
    from slack_bolt import App

    handlers, bot_token = _build_handlers(config, ingestor, query_engine)
    app = App(token=bot_token)

    @app.event("message")
    def _on_message(
        event: dict[str, Any], client: Any, say: Callable[..., None]
    ) -> None:
        handlers.handle_message(event, say, client=client)

    # Slack emits a separate file_shared event per upload, but it embeds only an id
    # stub with no download URL and no channel to reply in, so uploads are ingested from
    # the message/file_share event above. This no-op listener exists only so Bolt does
    # not log each one as an unhandled request
    @app.event("file_shared")
    def _on_file_shared(event: dict[str, Any]) -> None:
        return None

    return app


def run(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
) -> None:
    """Builds the app and blocks serving over Socket Mode, the daemon entry point.

    Lazily imports ``SocketModeHandler``, builds the app via :func:`build_app` and calls
    ``handler.start()``, which blocks forever. This is the production entry point
    (``thoth slack``) and is never unit-tested live, since CI has no Slack socket; the
    testable logic all lives on :class:`~thoth.slack_app.Handlers`.

    The blocking serve is wrapped by :func:`serve_with_alerting` so an unhandled daemon
    exception reaches the errors-to-Slack target before the process exits and systemd
    restarts it (issue #15), or a crash loop would be silent. The alert is best-effort
    and the original exception is always re-raised, so systemd still sees the non-zero
    exit.

    Args:
        config: The frozen runtime config, providing both Slack tokens
        ingestor: The constructed ingest pipeline
        query_engine: The constructed retrieval engine
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
    """Runs a blocking daemon loop, alerting on an unhandled exception.

    The top-level supervision seam (issue #15), factored out of :func:`run` so it is
    unit-testable without a real Slack socket. It invokes ``serve`` and, if that raises,
    posts an unhandled-exception alert best-effort and re-raises the original, so the
    process still exits non-zero and systemd restarts and rate-limits it.

    A clean shutdown is not an incident, so ``KeyboardInterrupt`` and ``SystemExit``,
    which is how ``systemctl stop`` and a deploy restart unwind the loop, re-raise
    silently. An alert on every routine restart would only train the operator to ignore
    them.

    Args:
        serve: The blocking daemon entry, for example ``SocketModeHandler(...).start``
        alerter: The errors-to-Slack alerter
    """
    try:
        serve()
    except (KeyboardInterrupt, SystemExit):
        # A clean stop is not a crash, so exit quietly with no alert
        raise
    except BaseException as exc:  # noqa: BLE001 - report ANY real crash, then re-raise
        alerter.alert_exception("slack daemon", exc)
        raise
