"""Passes 5-8: navigation log, Hindsight retain, the git commit, and the report."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from thoth.budget import BudgetExceededError
from thoth.git_sync import GitSyncError, VaultConflictError
from thoth.hindsight import HindsightError
from thoth.state import MARKER_PUSH
from thoth.vault import VaultError

from ._shared import (
    Capture,
    Classification,
    IngestError,
    IngestReport,
    LLMUnavailableError,
    RawCaptureResult,
    _Holding,
    _IngestorBase,
)


class _FinalisePass(_IngestorBase):
    """Passes 5-8: log append, retain/probe, explicit-paths commit, and report."""

    # ---- pass 5: navigation ------------------------------------------------------

    def _apply_navigation(self, plan: dict[str, Any], page_paths: list[str]) -> None:
        """Append a ``log.md`` block for every file touched (SPEC step 5).

        A reference page's one-line gloss is its own ``summary:`` frontmatter (routed in
        at write time by :meth:`_write_planned_page`), so there is no separate
        ``index.md`` catalog to maintain here -- ``index.md`` is a static set of Bases
        dashboards (ADR 0008). The only navigation edit left is the append-only
        ``log.md`` entry recording the touched paths.
        """
        try:
            self._vault.append_log("ingest", self._log_subject(plan), page_paths)
        except VaultError as exc:
            raise IngestError(f"log update failed: {exc}") from exc

    @staticmethod
    def _log_subject(plan: dict[str, Any]) -> str:
        """Build the log subject from the plan's log block or its first page title."""
        log = plan.get("log")
        if isinstance(log, dict):
            subject = log.get("subject")
            if isinstance(subject, str) and subject.strip():
                return subject
        pages = plan.get("pages")
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            title = pages[0].get("frontmatter", {}).get("title")
            if isinstance(title, str) and title.strip():
                return title
        return "capture"

    # ---- pass 6: retain ----------------------------------------------------------

    def _retain_pages(self, page_paths: list[str], cls: Classification) -> None:
        """Retain each curated page into Hindsight and probe that it landed.

        The page body is read back from the (already durable) vault file and retained
        keyed by its vault path; a ``probe`` confirms recall returns the path. A
        Hindsight failure is surfaced (the vault write is already durable), so the page
        is never silently lost (SPEC steps 6-7 ordering).

        A daily-budget trip (:class:`~thoth.budget.BudgetExceededError`) during retain
        is **not** an error: the curated page is already on disk and committed, so
        indexing is simply deferred to the next reindex (which re-retains every changed
        page). The remaining pages are left unindexed too and the pass returns cleanly
        -- the capture is filed, never lost, just not yet searchable (issue #16).

        Raises:
            IngestError: if a retain call fails (the page is still on disk).
        """
        for rel in page_paths:
            try:
                page = self._vault.read_page(rel)
            except VaultError:
                continue
            facts = self._retain_facts(page.frontmatter, page.body)
            try:
                self._hindsight.retain(rel, facts, tags=[cls.page_type, rel])
            except BudgetExceededError:
                # Cap reached mid-ingest: the page is durable on disk and will be
                # indexed by the next reindex; stop retaining rather than fail (#16).
                return
            except HindsightError as exc:
                raise IngestError(
                    f"hindsight retain failed for {rel} (page is filed on disk): {exc}"
                ) from exc
            # Best-effort 'did it land?' probe; a False does not abort the ingest.
            try:
                self._hindsight.probe(rel, cls.title)
            except HindsightError:
                pass

    @staticmethod
    def _retain_facts(frontmatter: dict[str, object], body: str) -> str:
        """Compose the fact text retained for a page (title line + body)."""
        title = frontmatter.get("title")
        header = f"{title}\n\n" if isinstance(title, str) and title else ""
        return f"{header}{body}".strip()

    # ---- pass 7: commit ----------------------------------------------------------

    @staticmethod
    def _capture_commit_paths(
        report: IngestReport, *, holding_raw: str | None
    ) -> list[str]:
        """Enumerate EVERY vault path this capture touched, for explicit staging (#85).

        Staging only the capture's own paths (rather than ``git add -A``) is the orphan
        fix, so this list must be exhaustive or it trades one orphan for another. The
        set is dynamic per capture:

        * ``report.page_paths`` -- the curated page(s) the file-plan wrote, which can be
          a *create* or an *update* of an existing page, including OTHER pages the plan
          touched (a capture may rewrite an entity page while filing a note); these are
          the plan's ``_written`` paths.
        * ``report.raw_paths`` -- the immutable raw sidecar (``raw/articles/<slug>.md``
          / ``raw/papers/<slug>.md``) when one was written.
        * ``report.asset_paths`` -- ALL N saved assets (a multi-image batch and any
          derived ``.excalidraw.md`` artifact, issues #84/#124/#68), not just the first.
        * ``holding_raw`` -- the superseded ``inbox/<sha>.md`` hold removed on success;
          a *deletion* that must be staged or the orphaned hold lingers untracked.
        * ``log.md`` -- the shared activity log every capture appends to (vault root).

        Returned de-duplicated and order-preserving (an asset already listed elsewhere
        is not staged twice).
        """
        ordered: list[str] = [
            *report.page_paths,
            *report.raw_paths,
            *report.asset_paths,
        ]
        if holding_raw is not None:
            ordered.append(holding_raw)
        ordered.append("log.md")
        return list(dict.fromkeys(path for path in ordered if path))

    def _finalise_git(
        self,
        report: IngestReport,
        subject: str,
        paths: list[str] | None,
        *,
        do_commit: bool,
        conflict_message: Callable[[VaultConflictError], str],
        staged_message: str | None,
        success_message: str | None,
        swallow_stage_error: bool,
        swallow_git_error: bool,
        pre_commit: Callable[[], None] | None = None,
    ) -> IngestReport:
        """Stage or commit this capture's ``paths``; fold the outcome into ``report``.

        The shared git tail of :meth:`_commit` / :meth:`_commit_unchanged` /
        :meth:`_commit_deferred`. ``pre_commit`` (when given) is a tree-mutating step
        (e.g. the deferred path's ``log.md`` append) that must share the single
        critical section with the stage/commit (issue #85); it runs as the first
        statement under the working-tree lock on both paths. With ``do_commit=False``
        exactly ``paths`` is staged under the lock for the caller's batched commit; a
        stage failure is swallowed or raised per ``swallow_stage_error`` and
        ``staged_message`` (when given) rewrites the report's message. Otherwise the
        commit/rebase/push runs under the lock: a
        :class:`~thoth.git_sync.VaultConflictError` is surfaced on the report via
        ``conflict_message`` (content stays filed locally; never a ``--force``), a
        :class:`~thoth.git_sync.GitSyncError` is swallowed or raised per
        ``swallow_git_error``, and a real push records the push liveness marker (issue
        #15) once the lock is released. ``success_message`` (when given) replaces the
        report's message on the committed path.
        """
        if not do_commit:
            with self._git.capture_lock:
                if pre_commit is not None:
                    pre_commit()
                if paths:
                    try:
                        self._git.stage(paths)
                    except GitSyncError as exc:
                        if not swallow_stage_error:
                            raise IngestError(f"stage failed: {exc}") from exc
            if staged_message is None:
                return report
            return replace(
                report, committed=False, conflict=False, message=staged_message
            )
        with self._git.capture_lock:
            if pre_commit is not None:
                pre_commit()
            try:
                result = self._git.commit(subject, paths=paths)
            except VaultConflictError as conflict:
                return replace(
                    report,
                    committed=False,
                    conflict=True,
                    message=conflict_message(conflict),
                )
            except GitSyncError as exc:
                if swallow_git_error:
                    return report
                raise IngestError(f"commit failed: {exc}") from exc
        if result.committed:
            # A non-empty vault-commit ran the rebase + push to completion, so the
            # remote is now current -- record the push liveness marker (issue #15).
            self._record_marker(MARKER_PUSH)
        return replace(
            report,
            committed=result.committed,
            conflict=False,
            message=success_message if success_message is not None else report.message,
        )

    def _commit(
        self,
        report: IngestReport,
        cls: Classification,
        *,
        do_commit: bool = True,
        paths: list[str] | None = None,
    ) -> IngestReport:
        """Commit this capture's explicit ``paths``; surface a conflict on the report.

        ``paths`` is the exact set of vault-relative paths this capture touched (curated
        page(s), raw sidecar, assets, the superseded inbox/ hold, and ``log.md``). It is
        staged and committed atomically (``git add -- <paths>`` in ``vault-commit``), so
        the commit can never sweep a different, concurrent capture's untracked asset and
        orphan its embedded ``![[asset]]`` (issue #85).

        ``do_commit=False`` (the ``thoth capture`` batch path, issue #80) defers the
        commit to the caller: it stages exactly ``paths`` here (so the batched commit
        later picks them up without an ``add -A`` that could sweep an unrelated
        capture's file), appends nothing more, and returns the report unchanged
        (``committed=False``). The caller commits+pushes the whole batch via
        :meth:`thoth.git_sync.GitSync.commit` (no ``paths`` -> commit the staged index).

        Returns:
            The report with ``committed``/``conflict``/``message`` populated.

        Raises:
            IngestError: on a non-conflict git failure.
        """
        return self._finalise_git(
            report,
            cls.title or "capture",
            paths,
            do_commit=do_commit,
            conflict_message=lambda conflict: (
                "VAULT CONFLICT: content is filed locally but the push was "
                "refused; resolve in Obsidian. Paths: "
                f"{', '.join(report.page_paths)} ({conflict})"
            ),
            staged_message=(
                f"Filed {len(report.page_paths)} page(s) (batch commit pending)."
            ),
            success_message=f"Filed {len(report.page_paths)} page(s).",
            swallow_stage_error=False,
            swallow_git_error=False,
        )

    def _commit_unchanged(
        self,
        report: IngestReport,
        cls: Classification,
        *,
        do_commit: bool,
        hold_rel: str | None,
    ) -> IngestReport:
        """Commit the holding-removal for a skip-on-unchanged run (issue #95, task D).

        Mirrors :meth:`_commit_deferred`'s git handling but preserves the ``unchanged``
        report's message instead of synthesising a "Filed N page(s)" line: a conflict is
        surfaced on the report (the removal is local; never a ``--force``), a benign
        "nothing to commit" leaves ``committed=False``, and a real push records the push
        marker. ``do_commit=False`` defers the commit to the batch caller. The ONLY path
        this run touched is the superseded inbox/ hold deletion, so exactly that is
        staged (issue #85) -- never an ``add -A`` that could sweep a concurrent capture.
        """
        # The only working-tree change is the hold deletion; stage exactly that.
        commit_paths = [hold_rel] if hold_rel is not None else []
        return self._finalise_git(
            report,
            cls.title or "unchanged capture",
            commit_paths,
            do_commit=do_commit,
            conflict_message=lambda conflict: (
                f"{report.message} (holding removal not pushed -- vault "
                f"conflict; resolve in Obsidian: {conflict})"
            ),
            staged_message=None,
            success_message=None,
            swallow_stage_error=True,
            swallow_git_error=False,
        )

    def _commit_deferred(
        self, holding: _Holding, exc: LLMUnavailableError, *, do_commit: bool = True
    ) -> IngestReport:
        """Commit the durable holding page; report deferred curation (SPEC section 6).

        The inbound item is already on disk (``inbox/`` holding page); the LLM was
        unavailable, so classify/curate are skipped. The holding page is logged and
        committed (best-effort -- a conflict or git failure is surfaced on the report,
        not raised, since the capture is already durable locally), and a ``deferred``
        report is returned so the Slack/MCP reply can say "saved raw, curation deferred"
        and a later reindex/sweep re-curates the held item.

        ``do_commit=False`` (the ``thoth capture`` batch path, issue #80) keeps the
        navigation log append (the hold is staged in the working tree) but leaves the
        git commit to the caller's batched commit -- exactly like the non-deferred
        :meth:`_commit`, so the deferred path is covered too.

        Args:
            holding: The durable pre-LLM holding write.
            exc: The :class:`LLMUnavailableError` that triggered the deferral.
            do_commit: When ``False``, append the log but defer the git commit to the
                caller's batch commit.

        Returns:
            A ``deferred`` :class:`IngestReport` naming the held raw page.
        """
        rel = holding.result.raw_path
        raw_paths = [rel] if rel is not None else []
        asset_paths = list(holding.result.asset_paths)
        report = IngestReport(
            page_paths=[],
            raw_paths=raw_paths,
            asset_paths=asset_paths,
            obsidian_links=[],
            wikilinks=[],
            deferred=True,
            message=(
                f"Saved raw, curation deferred ({exc}). The item is held durably "
                "in inbox/ but is not re-curated automatically -- re-run the capture "
                "to curate it once capacity is available."
            ),
        )
        # The hold page, its assets, and log.md are the only paths this deferred capture
        # touched; stage exactly those (issue #85). De-duplicated, log.md last.
        commit_paths = [*raw_paths, *asset_paths, "log.md"]

        # The log append + stage/commit is the narrow tree-mutating section: hand the
        # append to :meth:`_finalise_git` as ``pre_commit`` so the shared log.md append
        # and the commit/rebase run under ONE working-tree lock hold and never race a
        # concurrent capture (issue #85) -- while the push liveness marker is still
        # recorded after the lock is released.
        def _append_deferred_log() -> None:
            try:
                self._vault.append_log("ingest", "deferred capture", raw_paths)
            except VaultError:
                # Navigation is best-effort here; the durable hold is what matters.
                pass

        return self._finalise_git(
            report,
            "deferred capture",
            commit_paths,
            do_commit=do_commit,
            conflict_message=lambda conflict: (
                "Saved raw locally, curation deferred (LLM unavailable), but "
                f"the push was refused; resolve in Obsidian. ({conflict})"
            ),
            staged_message=None,
            success_message=None,
            swallow_stage_error=True,
            swallow_git_error=True,
            pre_commit=_append_deferred_log,
        )

    # ---- pass 8: report ----------------------------------------------------------

    def _build_report(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        page_paths: list[str],
        plan: dict[str, Any],
    ) -> IngestReport:
        """Assemble the report with harness-built ``obsidian://`` links and wikilinks.

        Every link is built by :meth:`thoth.vault.Vault.obsidian_uri` from a confined
        path, so the model cannot fabricate a link to a page that does not exist. The
        per-page ``titles`` run parallel to ``page_paths`` (both are ordered from
        ``plan["pages"]``, whose ``_written`` paths are ``page_paths``): each title is
        the page's ``frontmatter.title``, falling back to the slug-derived title when
        absent or empty, so the Slack renderer can label every link (issue #53).
        """
        links = [self._vault.obsidian_uri(rel) for rel in page_paths]
        wikilinks = [f"[[{PurePosixPath(rel).stem}]]" for rel in page_paths]
        titles = self._page_titles(plan, page_paths)
        raw_paths = [raw.raw_path] if raw.raw_path is not None else []
        return IngestReport(
            page_paths=list(page_paths),
            raw_paths=raw_paths,
            asset_paths=list(raw.asset_paths),
            obsidian_links=links,
            wikilinks=wikilinks,
            titles=titles,
        )

    @staticmethod
    def _page_titles(plan: dict[str, Any], page_paths: list[str]) -> list[str]:
        """Build a per-page title list parallel to ``page_paths`` (issue #53).

        Reads ``frontmatter.title`` from each ``plan["pages"]`` entry (ordered the same
        as ``page_paths``), falling back to the slug-derived title from the written path
        when a plan title is missing or blank. Pages without a matching plan entry
        (uneven lengths) still get a slug-derived title so the renderer never indexes
        off the end.
        """
        pages = plan.get("pages")
        plan_pages = pages if isinstance(pages, list) else []
        titles: list[str] = []
        for index, rel in enumerate(page_paths):
            title = ""
            if index < len(plan_pages):
                page = plan_pages[index]
                if isinstance(page, dict):
                    frontmatter = page.get("frontmatter")
                    if isinstance(frontmatter, dict):
                        title = str(frontmatter.get("title") or "").strip()
            if not title:
                title = PurePosixPath(rel).stem.replace("-", " ").title()
            titles.append(title)
        return titles

    def _written_page_paths(self, plan: dict[str, Any]) -> list[str]:
        """Return the page paths written by :meth:`curate` (the ``_written`` key)."""
        written = plan.get("_written")
        return list(written) if isinstance(written, list) else []

    def _page_tags(self, page_paths: list[str]) -> list[str]:
        """Collect the curated pages' frontmatter tags for the success log (issue #52).

        Reads each curated page's ``tags`` and returns the de-duplicated,
        order-preserving union; an unreadable page or a missing/ill-typed ``tags`` value
        is skipped so the observability line never raises or blocks a good ingest.
        """
        seen: list[str] = []
        for path in page_paths:
            try:
                page = self._vault.read_page(path)
            except VaultError:
                continue
            tags = page.frontmatter.get("tags")
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, str) and tag and tag not in seen:
                    seen.append(tag)
        return seen
