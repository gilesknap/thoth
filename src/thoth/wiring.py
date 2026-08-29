"""Shared construction of the ingest and query collaborator graph.

Both production entry points, the Slack daemon plus the ``thoth capture`` and ``thoth
ask`` CLI on one side and :func:`thoth.mcp_server.run` on the other, need the same
graph: a vault, an LLM, an extractor, a Hindsight client, a git sync, an ingestor and a
query engine. :func:`build_collaborators` is the single place that shape is wired, so
the two callers cannot drift. The MCP wiring once dropped ``schema_md``, which left
curate blind to the live schema.

The heavy imports happen inside the function body at call time, so importing this module
stays light and a test that patches a collaborator on its defining module is picked up.
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
        vault: The path-confined read/write facade, the only disk surface
        git: The deterministic git sync wrapper
        ingestor: The constructed ingest pipeline
        query_engine: The vault-only retrieval engine
    """

    vault: Vault
    git: GitSync
    ingestor: Ingestor
    query_engine: QueryEngine


def build_collaborators(
    config: Config, *, guard: Any, markers: MarkerStore | None = None
) -> Collaborators:
    """Wires the full collaborator graph from ``config``.

    Args:
        config: The frozen runtime config
        guard: The budget guard shared by the LLM and Hindsight, so one daily cap covers
            both spenders. Built by the caller: the Slack and CLI side attaches an
            alerter, while the MCP side blocks silently
        markers: Optional liveness marker store threaded into the ingestor (issue #15),
            where ``None``, the MCP default, disables marker recording

    Returns:
        The constructed :class:`Collaborators`
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
    # SCHEMA.md rides in as the curate-call system_extra so pages are filed to the live
    # per-type schema. This wiring used to drop schema_md, which left the curate model
    # filing blind and the vault empty
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
