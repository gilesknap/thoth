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
        """Appends a ``log.md`` block for every file touched (SPEC step 5).

        A page's gloss is its own ``summary:`` frontmatter, routed in at write time, so
        there is no separate catalog to maintain here. ``index.md`` is a static set of
        dashboards (ADR 0008), which leaves the append-only log entry as the only
        navigation edit.
        """
        try:
            self._vault.append_log("ingest", self._log_subject(plan), page_paths)
        except VaultError as exc:
            raise IngestError(f"log update failed: {exc}") from exc

    @staticmethod
    def _log_subject(plan: dict[str, Any]) -> str:
        """Builds the log subject from the plan's log block or first page title."""
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
        """Retains each curated page into Hindsight and probes that it landed.

        The body is read back from the already-durable vault file and retained keyed by
        its path, then a probe confirms recall returns it. A Hindsight failure is
        surfaced, so a page is never silently lost (SPEC steps 6 and 7).

        A daily-budget trip during retain is not an error. The page is already on disk
        and committed, so indexing defers to the next reindex, which re-retains every
        changed page. The capture is filed and never lost, just not yet searchable
        (issue #16).

        Raises:
            IngestError: if a retain call fails, though the page is still on disk.
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
                # Cap reached mid-ingest. The page is durable on disk and the next
                # reindex will index it, so stop retaining rather than fail (#16)
                return
            except HindsightError as exc:
                raise IngestError(
                    f"hindsight retain failed for {rel} (page is filed on disk): {exc}"
                ) from exc
            # Best-effort "did it land?" probe, where a false does not abort the ingest
            try:
                self._hindsight.probe(rel, cls.title)
            except HindsightError:
                pass

    @staticmethod
    def _retain_facts(frontmatter: dict[str, object], body: str) -> str:
        """Composes the fact text retained for a page."""
        title = frontmatter.get("title")
        header = f"{title}\n\n" if isinstance(title, str) and title else ""
        return f"{header}{body}".strip()

    # ---- pass 7: commit ----------------------------------------------------------

    @staticmethod
    def _capture_commit_paths(
        report: IngestReport, *, holding_raw: str | None
    ) -> list[str]:
        """Enumerates every vault path this capture touched, for staging (#85).

        Staging only the capture's own paths rather than everything is the orphan fix,
        so this list must be exhaustive or it trades one orphan for another. The set is
        dynamic per capture:

        * ``report.page_paths`` -- the curated pages the file-plan wrote, a create or an
          update, including other pages the plan touched. A capture may rewrite an
          entity page while filing a note.
        * ``report.raw_paths`` -- the immutable raw sidecar, when one was written.
        * ``report.asset_paths`` -- all saved assets, a multi-image batch and any
          derived ``.excalidraw.md`` artifact, not just the first.
        * ``holding_raw`` -- the superseded ``inbox/`` hold removed on success. A
          deletion that must be staged, or the orphaned hold lingers untracked.
        * ``log.md`` -- the shared activity log every capture appends to.

        Returned de-duplicated and order-preserving, so an asset listed twice is staged
        once.
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
        """Stages or commits this capture's paths and folds the outcome in.

        This is the shared git tail of the three commit methods. With ``do_commit``
        false, exactly these paths are staged under the working-tree lock for the
        caller's batched commit.

        Otherwise the commit, rebase and push run under that lock, and a real push
        records the push marker (issue #15) once it is released. A conflict is surfaced
        on the report rather than raised, so content stays filed locally and nothing is
        ever force-pushed.
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
            # A non-empty commit ran the rebase and push to completion, so the remote is
            # current and the push marker can be recorded (issue #15)
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
        """Commits this capture's explicit paths, surfacing a conflict on the report.

        The paths are the exact set this capture touched, staged and committed
        atomically, so the commit can never sweep a concurrent capture's untracked asset
        and orphan its embed (issue #85). With ``do_commit`` false, the batch path
        stages exactly these paths and leaves the commit to the caller, which then
        commits the staged index for the whole batch.

        Returns:
            The report with the commit outcome populated.

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
        """Commits the hold removal for a skip-on-unchanged run (issue #95).

        Mirrors the deferred path's git handling but keeps the unchanged report's own
        message rather than synthesising a filed line. A benign nothing-to-commit leaves
        the report uncommitted, and a real push records the marker. The only path this
        run touched is the superseded hold deletion, so exactly that is staged (issue
        #85).
        """
        # The only working-tree change is the hold deletion, so stage exactly that
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
        """Commits the durable hold and reports deferred curation (SPEC section 6).

        The item is already on disk as a hold and the LLM was unavailable, so classify
        and curate are skipped. Committing is best-effort: a conflict or git failure is
        surfaced on the report rather than raised, because the capture is already
        durable locally. The deferred report lets the reply say the raw was saved and
        curation deferred, so a later sweep re-curates the held item.

        Args:
            holding: The durable pre-LLM holding write.
            exc: The failure that triggered the deferral.
            do_commit: False appends the log but leaves the commit to the batch
                caller, exactly as the non-deferred path does.

        Returns:
            A deferred report naming the held raw page.
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
        # The hold page, its assets and log.md are the only paths this deferred capture
        # touched, so stage exactly those (issue #85), de-duplicated with log.md last
        commit_paths = [*raw_paths, *asset_paths, "log.md"]

        # The log append plus stage or commit is the narrow tree-mutating section, so
        # the append is handed over as pre_commit. The shared log.md append and the
        # commit run under one lock hold and never race a concurrent capture (issue
        # #85), while the push marker is still recorded after the lock is released
        def _append_deferred_log() -> None:
            try:
                self._vault.append_log("ingest", "deferred capture", raw_paths)
            except VaultError:
                # Navigation is best-effort here, the durable hold is what matters
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
        """Assembles the report with harness-built links and wikilinks.

        Every link is built from a confined path, so the model cannot fabricate a link
        to a page that does not exist. The titles run parallel to the page paths, both
        ordered from the plan, so the Slack renderer can label every link (issue #53).
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
        """Builds a per-page title list parallel to the page paths (issue #53).

        Titles come from each plan page's frontmatter, falling back to a slug-derived
        title when one is missing or blank. A page with no matching plan entry still
        gets a slug-derived title, so the renderer never indexes off the end.
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
        """Returns the page paths curate wrote."""
        written = plan.get("_written")
        return list(written) if isinstance(written, list) else []

    def _page_tags(self, page_paths: list[str]) -> list[str]:
        """Collects the curated pages' tags for the success log (issue #52).

        An unreadable page, or an absent or ill-typed tags value, is skipped, so the
        observability line never raises or blocks a good ingest.
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
