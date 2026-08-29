"""Shared construction of the ingest/query collaborator graph.

Both production entry points need the same graph. They are
``thoth.__main__._build_graph``, which serves the Slack daemon and the ``thoth capture``
and ``thoth ask`` CLI, and :func:`thoth.mcp_server.run`, which serves the MCP server.
The graph holds a :class:`~thoth.vault.Vault`, an :class:`~thoth.llm.LLM`, an
:class:`~thoth.extract.Extractor`, a :class:`~thoth.hindsight.Hindsight`, a
:class:`~thoth.git_sync.GitSync`, an :class:`~thoth.ingest.Ingestor` and a
:class:`~thoth.query.QueryEngine`. :func:`build_collaborators` is the single place that
wires that shape, so the two callers cannot drift. The MCP wiring once dropped
``schema_md``, which left curate blind to the live schema.

The heavy imports happen inside the function body, at call time. Importing this module
therefore stays light, and a test that patches a collaborator on its defining module
takes effect. ``thoth.git_sync.GitSync`` and ``thoth.hindsight.Hindsight`` are two such
modules.
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
        guard: The :class:`~thoth.budget.BudgetGuard`, or a no-op stand-in. The LLM
            (classify, analyse and curate) and Hindsight (retain) share it, so one daily
            cap covers both spenders. The caller builds it. The Slack and CLI side
            attaches an alerter, and the MCP side blocks silently.
        markers: An optional liveness :class:`~thoth.state.MarkerStore` threaded into
            the ingestor (issue #15). ``None``, the MCP default, disables marker
            recording.

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
    # Pass SCHEMA.md as the curate-call system_extra, so curate files a page against the
    # live per-type schema. Without it the curate model files blind. This wiring used to
    # drop schema_md, which left the vault empty when paired with a schema-less prompt.
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
