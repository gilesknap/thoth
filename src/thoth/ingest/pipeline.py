"""The composed :class:`Ingestor` running the bounded passes (SPEC section 6)."""

from __future__ import annotations

import time
from collections.abc import Callable

from thoth.git_sync import GitSyncError
from thoth.state import MARKER_CAPTURE

from ._shared import (
    _TYPE_FOLDER,
    Capture,
    Classification,
    IngestError,
    IngestReport,
    LLMUnavailableError,
    RawCaptureResult,
    _Analysed,
    _Holding,
    logger,
)
from .analyse import _AnalysePass, _cleanup_fetched
from .classify import _ClassifyPass
from .curate import _CuratePass
from .finalise import _FinalisePass
from .raw_capture import _RawCapturePass


class Ingestor(
    _AnalysePass, _ClassifyPass, _RawCapturePass, _CuratePass, _FinalisePass
):
    """Orchestrates the bounded-pass ingest with all collaborators injected."""

    # ---- the full pipeline -------------------------------------------------------

    def ingest(
        self,
        capture: Capture,
        *,
        commit: bool = True,
        as_is: bool = False,
        on_phase: Callable[[str], None] | None = None,
    ) -> IngestReport:
        """Runs the bounded passes and returns a structured report.

        Capture durability is decoupled from the classify call (issue #14). The inbound
        item is extracted and persisted to a durable hold before any LLM call, so an
        outage can never lose a capture. Classify and curate then run as a best-effort
        second stage, and if the LLM is unavailable the held raw is already safe, the
        hold is committed, and a deferred report is returned for a later sweep.

        On success the superseded hold is removed and the remaining passes run. The
        validation gate is preserved: a rejected plan still raises and only a transport
        failure defers. A rebase conflict at commit is surfaced on the report, with
        content filed locally and nothing force-pushed.

        Args:
            capture: The inbound item to ingest.
            commit: False defers the git work to a batch caller, which pulls once up
                front, so staged changes accumulate for one batched commit. The
                deferred path honours it too.
            as_is: True is the low-touch import mode (ADR 0010). Classify still runs
                for routing, but curate is skipped and the page is filed once with
                the original body verbatim, then indexed through the same retain
                pass.
            on_phase: Optional best-effort progress callback (issue #137), fired with
                a short label before each user-meaningful pass. The Slack handler
                threads it through to edit the placeholder live, and other callers
                leave it None. It fires only on transitions and a raising callback is
                swallowed, so progress reporting can never abort an ingest.

        Returns:
            The report describing every file touched.

        Raises:
            IngestError: on an extraction, validation, or non-conflict git failure. An
                LLM-availability failure is reported as deferred rather than raised.
        """
        started = time.monotonic()

        def phase(label: str) -> None:
            """Fires the progress callback guarded, so it never breaks an ingest."""
            if on_phase is None:
                return
            try:
                on_phase(label)
            except Exception:  # noqa: BLE001 - progress reporting is best-effort, never fatal
                pass

        # A batch caller pulled once up front, so skip the per-call orient and let the
        # staged changes accumulate for its commit. The orient pull rewrites the whole
        # working tree, so it runs under the narrow lock (issue #85), but only the pull
        # and not the slow LLM passes, so concurrent captures still overlap
        if commit:
            with self._git.capture_lock:
                self._orient()
        holding = self.persist_inbound(capture, as_is=as_is)
        extracted_body = (
            holding.prefetched.body if holding.prefetched is not None else None
        )
        analysed = _Analysed(analysis=None)
        try:
            phase(
                "reading image "
                f"({self._config.analyse_model or self._config.anthropic_model})"
            )
            analysed = self.analyse(capture)
            analysis = analysed.analysis
            phase(f"classifying ({self._config.anthropic_model})")
            classification = self.classify(
                capture,
                analysis=analysis,
                extracted_body=extracted_body,
            )
            raw = self.capture_raw(
                capture,
                classification,
                prefetched=holding.prefetched,
                fetched=analysed.fetched,
                derived=analysed,
            )
            # Skip-on-unchanged short-circuit (issue #95). When the source was
            # byte-identical to an existing raw page, which because the path embeds the
            # slug means classify reproduced the prior routing, and the curated page is
            # already on disk, the curate work is pure churn. A re-run would re-spend
            # the call and bump the updated date for no content change, so skip it and
            # let a re-run of an interrupted import cost nothing for the parts already
            # done
            logger.debug(
                "capture_raw: disposition=%s raw_path=%s assets=%d",
                raw.disposition,
                raw.raw_path,
                len(raw.asset_paths),
            )
            curated = self._unchanged_curated(raw, classification)
            if curated is not None:
                logger.debug(
                    "dedup short-circuit: unchanged, already curated at %s "
                    "(skipping curate/navigation/retain)",
                    curated,
                )
                return self._skip_unchanged(holding, classification, curated, commit)
            if as_is:
                # Low-touch import (ADR 0010), so skip curate and file the original
                # body verbatim into the classify-chosen folder with no second call
                plan = self._file_as_is(
                    capture,
                    classification,
                    raw,
                    extracted_body=extracted_body,
                )
            else:
                phase(f"curating ({self._config.anthropic_model})")
                candidates = self.fetch_candidates(classification)
                plan = self.curate(
                    capture,
                    classification,
                    raw,
                    candidates,
                    analysis=analysis,
                    extracted_body=extracted_body,
                )
        except LLMUnavailableError as exc:
            # The pass deferred, so capture_raw never consumed the analyse binary.
            # Clean up its temp file here rather than leak it, since the inbound item is
            # already durable in inbox/
            _cleanup_fetched(analysed.fetched)
            # Operator-readable line (issue #52). A deferral is a partial success,
            # since the raw item is durable, so say so rather than leave the degraded
            # path silent
            held = holding.result.raw_path or "inbox"
            logger.info(
                "ingest deferred: held %s (LLM unavailable: %s) in %.0fms",
                held,
                exc,
                (time.monotonic() - started) * 1000,
            )
            return self._commit_deferred(holding, exc, do_commit=commit)

        # Curation succeeded, so the hold is superseded by the curated and raw pages
        if holding.result.raw_path is not None:
            self._vault.remove_page(holding.result.raw_path)

        page_paths = self._written_page_paths(plan)
        # Retain reads the already-durable pages off disk and never touches the working
        # tree, so it runs outside the lock, keeping the locked section down to the
        # sub-second log append, stage, commit and push
        phase("indexing")
        self._retain_pages(page_paths, classification)

        report = self._build_report(capture, classification, raw, page_paths, plan)
        # The exact paths this capture touched, covering the curated pages including any
        # other page the plan updated, the raw sidecar, every saved asset, the
        # superseded hold as a deletion, and the shared log. So the commit stages only
        # its own work and never sweeps a concurrent capture's asset (#85)
        commit_paths = self._capture_commit_paths(
            report, holding_raw=holding.result.raw_path
        )
        # The narrow tree-mutating section (issue #85). Append the shared log, stage
        # exactly this capture's paths, commit, rebase and push, all under the lock so
        # two concurrent captures never collide. The LLM passes above ran unlocked
        with self._git.capture_lock:
            self._apply_navigation(plan, page_paths)
            committed = self._commit(
                report, classification, do_commit=commit, paths=commit_paths
            )
        # Record the capture marker only on a clean ingest, so a wedged sync leaves both
        # markers stale and silence becomes the heartbeat's diagnostic (issue #15). The
        # push marker is recorded inside _commit on an actual push
        if not committed.conflict:
            self._record_marker(MARKER_CAPTURE)
        # Operator-readable success line (issue #52) naming the curated paths, the
        # routed type, the tags and the duration, so a successful capture is not silent
        # on the happy path. A conflict is already surfaced through the report
        logger.info(
            "ingest filed: %s type=%s tags=%s in %.0fms%s",
            ", ".join(page_paths) or "(no curated page)",
            classification.page_type,
            self._page_tags(page_paths),
            (time.monotonic() - started) * 1000,
            " [CONFLICT: unpushed]" if committed.conflict else "",
        )
        return committed

    def _unchanged_curated(
        self, raw: RawCaptureResult, cls: Classification
    ) -> str | None:
        """Returns the existing curated path when this is a no-op re-run.

        The skip-on-unchanged short-circuit (issue #95) is taken only when both
        conditions hold, so it never skips genuine work:

        * the raw pass reported the source byte-identical to an existing raw page.
          Because that path embeds the slug, a match guarantees classify reproduced the
          prior run's slug; a drifted slug would have created a fresh raw page instead.
        * a curated page already exists at the classify-routed ``<folder>/<slug>.md``.

        A type with no content folder, or a missing curated page, returns ``None`` and
        the caller falls through to the normal pass. The short-circuit is purely an
        optimisation and conservative by construction: a false negative just re-runs
        curate, and it never wrongly skips an absent page.
        """
        if raw.disposition != "skipped_unchanged":
            return None
        folder = _TYPE_FOLDER.get(cls.page_type)
        if folder is None:
            return None
        rel = f"{folder}/{cls.slug}.md"
        return rel if self._vault.page_exists(rel) else None

    def _skip_unchanged(
        self,
        holding: _Holding,
        cls: Classification,
        curated_path: str,
        do_commit: bool,
    ) -> IngestReport:
        """Terminal path for a no-op re-run whose content is already curated (#95).

        Removes the superseded hold written this run, exactly as the success path does,
        then returns an unchanged report without running curate, the navigation log, or
        retain. So neither the curated page's date nor the log is churned and no budget
        is re-spent for content already on disk, while the page stays searchable from
        its original retain. The hold deletion is committed, or staged for a batch
        caller, just like a normal success, and the capture marker is recorded on a
        clean run since the pipeline ran healthily.
        """
        hold_rel = holding.result.raw_path
        if hold_rel is not None:
            self._vault.remove_page(hold_rel)
        report = IngestReport(
            page_paths=[],
            raw_paths=[],
            asset_paths=[],
            obsidian_links=[],
            wikilinks=[],
            unchanged=True,
            message=f"Unchanged; already curated at {curated_path} (skipped).",
        )
        committed = self._commit_unchanged(
            report, cls, do_commit=do_commit, hold_rel=hold_rel
        )
        if not committed.conflict:
            self._record_marker(MARKER_CAPTURE)
        return committed

    # ---- pass 0: orient ----------------------------------------------------------

    def _orient(self) -> None:
        """Pulls the vault so writes land on current state (SPEC step 0)."""
        try:
            self._git.pull()
        except GitSyncError as exc:
            raise IngestError(f"vault pull failed before ingest: {exc}") from exc
