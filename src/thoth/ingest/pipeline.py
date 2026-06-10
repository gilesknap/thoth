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
        """Run the bounded passes and return a structured report.

        ``commit`` and ``as_is`` are the two seams the ``thoth capture`` backfill (issue
        #80) drives; both default to the Slack/MCP behaviour, so existing callers are
        unaffected:

        * ``commit=False`` defers the git work to the caller. The per-call orient
          (:meth:`_orient` pull) is skipped -- the batch caller pulls **once** up front
          -- and the commit pass writes/logs to disk but does **not** call
          :meth:`thoth.git_sync.GitSync.commit`, so staged changes accumulate in the
          working tree for one batched commit. The returned report has
          ``committed=False``. The deferred (LLM-unavailable) path honours it too.
        * ``as_is=True`` is the low-touch import mode (ADR 0010): the cheap classify
          call still runs (for routing only), but the expensive **curate** is SKIPPED.
          The page is filed ONCE with the original body verbatim and a minimal derived
          frontmatter into the classify-chosen folder, then indexed through the SAME
          retain pass. No file-plan, no reshaping, no wikilink/dedup-merge, no summary
          synthesis -- "files + indexes, skips curate" literally.

        Capture durability is **decoupled from the classify LLM call** (per issue #14):
        the inbound item is extracted and persisted to a durable ``inbox/`` holding page
        (idempotent on the body SHA-256) *before* any LLM call, so an Anthropic outage
        can never lose a capture. Classify/curate then run as a best-effort second
        stage; if the LLM is unavailable (a :class:`LLMUnavailableError`) the held raw
        is already safe, the holding page is committed, and a *deferred-curation* report
        is returned for a later reindex/sweep to re-curate. On success the (now
        superseded) holding page is removed and the curated/raw/navigation/retain passes
        run as before.

        The **validation gate is preserved**: a rejected plan (bad type/slug, an
        unparseable or schema-invalid output) still raises :class:`IngestError`; only a
        *transport* failure defers. A rebase conflict at commit is surfaced as
        :attr:`IngestReport.conflict` (content filed locally; no ``--force``).

        Args:
            capture: The inbound item to ingest.
            on_phase: Optional best-effort progress callback (issue #137), invoked
                with a short label (with the model where applicable) **before** each
                user-meaningful pass runs -- ``"reading image (<model>)"``,
                ``"classifying (<model>)"``, ``"curating (<model>)"`` (curate path
                only), ``"indexing"``. The Slack handler threads it through to edit
                the "Filing…" placeholder live; non-Slack callers (MCP, the ``thoth
                capture`` backfill) leave it ``None`` (a no-op). It is fired only on
                phase transitions, never in a tight loop, and a raising callback is
                swallowed so progress reporting can never break or abort an ingest.

        Returns:
            The :class:`IngestReport` describing every file touched.

        Raises:
            IngestError: on an extraction, validation, or non-conflict git failure (an
                LLM-availability failure is reported as deferred, not raised).
        """
        started = time.monotonic()

        def phase(label: str) -> None:
            """Fire the progress callback guarded (best-effort, never breaks ingest)."""
            if on_phase is None:
                return
            try:
                on_phase(label)
            except Exception:  # noqa: BLE001 - progress reporting is best-effort, never fatal
                pass

        # commit=False means a batch caller (thoth capture) pulled once up front, so
        # skip the per-call orient and let the staged changes accumulate for its commit.
        # The orient pull rewrites the whole working tree (``pull --rebase
        # --autostash``), so it runs under the narrow working-tree lock (issue #85) --
        # but only the pull, NOT the slow LLM passes that follow, so concurrent captures
        # still overlap on the expensive work and only serialise on the git steps.
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
            # Task D (issue #95): true skip-on-unchanged short-circuit. When the raw
            # source was byte-identical to an existing raw page (skipped_unchanged --
            # which, because the raw path embeds the slug, means classify reproduced the
            # prior routing) AND the curated page it produced is already on disk, the
            # classify-routed curate work is pure churn: a re-run would re-spend the
            # curate LLM call and bump the curated page's `updated:` date for no content
            # change. Skip curate (and navigation/retain) entirely so a re-run to finish
            # an interrupted import costs nothing for the parts already done. Applies to
            # both the curate and as-is paths (each files to the same folder/slug).
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
                # Low-touch import (ADR 0010): SKIP curate; file the original body
                # verbatim into the classify-chosen folder. No second LLM call.
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
            # classify/curate (or the analyse call itself) deferred: capture_raw never
            # consumed the analyse-pass binary, so clean up its temp file here rather
            # than leak it (the inbound item is already durable in inbox/).
            _cleanup_fetched(analysed.fetched)
            # Concise operator-readable line (issue #52): a deferral is a partial
            # success (the raw item is durable in inbox/), so say so clearly rather than
            # leaving the degraded path silent. Grep-friendly prefix.
            held = holding.result.raw_path or "inbox"
            logger.info(
                "ingest deferred: held %s (LLM unavailable: %s) in %.0fms",
                held,
                exc,
                (time.monotonic() - started) * 1000,
            )
            return self._commit_deferred(holding, exc, do_commit=commit)

        # Curation succeeded: the holding page is superseded by the curated/raw pages.
        if holding.result.raw_path is not None:
            self._vault.remove_page(holding.result.raw_path)

        page_paths = self._written_page_paths(plan)
        # Retain (Hindsight) reads the already-durable pages off disk and never touches
        # the working tree, so it runs OUTSIDE the working-tree lock -- keeping the
        # locked section down to the sub-second log-append -> stage -> commit -> push.
        phase("indexing")
        self._retain_pages(page_paths, classification)

        report = self._build_report(capture, classification, raw, page_paths, plan)
        # The exact paths this capture touched -- curated page(s) (incl. any OTHER
        # existing page the file-plan updated), the raw sidecar, every saved asset, the
        # superseded inbox/ hold (a deletion), and the shared log.md -- so the commit
        # stages only its own work and never sweeps a concurrent capture's asset (#85).
        commit_paths = self._capture_commit_paths(
            report, holding_raw=holding.result.raw_path
        )
        # The narrow tree-mutating critical section (issue #85): append the shared
        # log.md, stage exactly this capture's paths, commit, rebase, push -- all under
        # the working-tree lock so two concurrent captures never collide on log.md, the
        # index lock, or the rebase. The LLM passes above ran unlocked.
        with self._git.capture_lock:
            self._apply_navigation(plan, page_paths)
            committed = self._commit(
                report, classification, do_commit=commit, paths=commit_paths
            )
        # Record the capture liveness marker only on a clean (non-conflict) ingest, so a
        # wedged sync leaves BOTH the capture and push markers stale -- silence is then
        # the heartbeat's diagnostic (issue #15). The push marker is recorded inside
        # _commit on an actual push.
        if not committed.conflict:
            self._record_marker(MARKER_CAPTURE)
        # Concise operator-readable success line (issue #52): one terse, grep-friendly
        # "ingest filed:" naming the curated path(s), the routed page_type, the page
        # tags, and the wall-clock duration, so a successful capture is no longer silent
        # on the happy path. A conflict is already surfaced via the report.
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
        """Return the existing curated path when this is a no-op re-run, else ``None``.

        The skip-on-unchanged short-circuit (issue #95, task D) is only taken when BOTH
        conditions hold, so it never skips genuine work:

        * the raw-capture pass reported ``skipped_unchanged`` -- the source body was
          byte-identical to an existing raw page. Because that raw path embeds the slug
          (``raw/<subdir>/<slug>.md``), a match guarantees classify reproduced the prior
          run's slug; a drifted slug would have created a fresh raw page instead.
        * a curated page already exists at the classify-routed ``<folder>/<slug>.md``.

        A type with no content folder (only ``inbox`` is excluded from
        :data:`_TYPE_FOLDER`) or a missing curated page returns ``None`` so the caller
        falls through to the normal curate/as-is pass -- the short-circuit is purely an
        optimisation and is conservative by construction (a false negative just re-runs
        curate; it never wrongly skips an absent page).
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
        """Terminal path for a no-op re-run: unchanged content already curated (#95 D).

        Removes the (now superseded) holding page written this run by
        :meth:`persist_inbound` -- exactly like the success path -- then returns an
        ``unchanged`` report WITHOUT running the curate, navigation-log, or Hindsight
        retain passes. So neither the curated page's ``updated:`` date nor the
        ``log.md`` is churned and no LLM/index budget is re-spent for content already on
        disk; the page stays searchable from its original retain. The holding-removal
        deletion is committed (or, for the ``commit=False`` batch path, staged for the
        caller's batched commit) just like a normal success, and the capture liveness
        marker is recorded on a clean (non-conflict) run since the pipeline ran
        healthily.
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
        """Pull the vault so writes land on current state (SPEC step 0)."""
        try:
            self._git.pull()
        except GitSyncError as exc:
            raise IngestError(f"vault pull failed before ingest: {exc}") from exc
