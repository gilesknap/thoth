"""Shared construction of the ingest/query collaborator graph.

Both production entry points -- ``thoth.__main__._build_graph`` (the Slack daemon and
the ``thoth capture``/``thoth ask`` CLI) and :func:`thoth.mcp_server.run` (the MCP
server) -- need the same graph: a :class:`~thoth.vault.Vault`, an
:class:`~thoth.llm.LLM`, an :class:`~thoth.extract.Extractor`, a
:class:`~thoth.hindsight.Hindsight`, a :class:`~thoth.git_sync.GitSync`, an
:class:`~thoth.ingest.Ingestor` and a :class:`~thoth.query.QueryEngine`.
:func:`build_collaborators` is the single place that shape is wired, so the two
callers cannot drift (the MCP wiring once dropped ``schema_md``, leaving curate blind
to the live schema).

The heavy imports happen inside the function body, at call time, so importing this
module stays light and tests that patch a collaborator on its defining module (for
example ``thoth.git_sync.GitSync`` or ``thoth.hindsight.Hindsight``) are picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from thoth.config import Config
    from thoth.git_sync import GitSync
    from thoth.ingest import Ingestor
    from thoth.query import QueryEngine
    from thoth.state import MarkerStore
    from thoth.vault import Vault

__all__ = ["Collaborators", "build_collaborators"]


@dataclass(frozen=True, slots=True)
class Collaborators:
    """The constructed collaborator graph returned by :func:`build_collaborators`.

    Attributes:
        vault: The path-confined read/write vault facade (the only disk surface).
        git: The deterministic git sync wrapper.
        ingestor: The constructed ingest pipeline.
        query_engine: The vault-only retrieval engine.
    """

    vault: Vault
    git: GitSync
    ingestor: Ingestor
    query_engine: QueryEngine


def build_collaborators(
    config: Config, *, guard: Any, markers: MarkerStore | None = None
) -> Collaborators:
    """Wire the full collaborator graph from ``config``.

    Args:
        config: The frozen runtime config.
        guard: The :class:`~thoth.budget.BudgetGuard` (or a no-op stand-in) shared by
            the LLM (classify/analyse/curate) and Hindsight (retain), so one daily cap
            covers both spenders. Built by the caller -- the Slack/CLI side attaches an
            alerter, the MCP side blocks silently.
        markers: Optional liveness :class:`~thoth.state.MarkerStore` threaded into the
            ingestor (issue #15). ``None`` (the MCP default) disables marker recording.

    Returns:
        The constructed :class:`Collaborators`.
    """
    from .extract import Extractor
    from .git_sync import GitSync
    from .hindsight import Hindsight
    from .ingest import Ingestor
    from .llm import LLM
    from .query import QueryEngine
    from .vault import Vault

    vault = Vault(config)
    llm = LLM(config, guard=guard)
    extractor = Extractor(config)
    hindsight = Hindsight(config, guard=guard)
    git = GitSync(config)
    # Pass SCHEMA.md as the curate-call system_extra so curated pages are filed to the
    # live per-type schema; without it the curate model files blind (this wiring used to
    # drop schema_md, leaving the vault empty when paired with a schema-less prompt).
    ingestor = Ingestor(
        config,
        vault,
        llm,
        extractor,
        hindsight,
        git,
        schema_md=vault.schema_md(),
        markers=markers,
    )
    query_engine = QueryEngine(config, vault, hindsight, llm)
    return Collaborators(
        vault=vault, git=git, ingestor=ingestor, query_engine=query_engine
    )
