"""The bytes-SHA-256 idempotent ``raw/assets`` store shared by the binary passes."""

from __future__ import annotations

import tempfile
from pathlib import Path

from thoth.extract import FetchedBinary
from thoth.vault import SlugError, Vault, VaultError

from ._shared import (
    Capture,
    Classification,
    IngestError,
    RawCaptureResult,
    _IngestorBase,
    _require,
)


class _AssetStore(_IngestorBase):
    """Stages binaries into ``raw/assets`` with the SHA-256 idempotency/drift rule."""

    def _obtain_primary_asset(
        self,
        capture: Capture,
        cls: Classification,
        fetched: FetchedBinary | None,
        *,
        local_ext: str,
    ) -> tuple[RawCaptureResult, str | None]:
        """Acquire a binary capture's primary asset; return it plus any provenance URL.

        The shared acquisition step of :meth:`_capture_pdf` and :meth:`_capture_image`:
        a ``url`` capture reuses the analyse pass's single download when present (no
        second fetch, no leaked temp) -- falling back to fetching for a standalone
        :meth:`capture_raw` call -- and carries the fetch's ``source_url``; a local
        ``path`` capture is staged under ``local_ext`` with no provenance URL.
        """
        if capture.url is not None:
            binary = (
                fetched
                if fetched is not None
                else self._extractor.fetch_binary(capture.url)
            )
            return self._save_fetched_asset(cls, binary), binary.source_url
        path = _require(capture.path, "path")
        return self._save_local_asset_result_named(cls.slug, path, local_ext), None

    def _save_fetched_asset(
        self, cls: Classification, fetched: FetchedBinary
    ) -> RawCaptureResult:
        """Move a :class:`~thoth.extract.FetchedBinary` tmp file into ``raw/assets``.

        Idempotent on the fetched bytes' SHA-256: if the destination asset already
        holds byte-identical content the move is skipped (``'skipped_unchanged'``) and
        the staged tmp file is cleaned up; a byte mismatch at the same slug is surfaced
        as drift (never an overwrite). On the happy path :meth:`Vault.save_asset` moves
        the tmp file; only the error/skip path must clean it up.
        """
        asset_name = f"{cls.slug}.{fetched.suggested_ext}"
        return self._store_asset(fetched.tmp_path, asset_name)

    def _save_local_asset_result_named(
        self, asset_slug: str, path: Path, ext: str
    ) -> RawCaptureResult:
        """Stage a local file into ``raw/assets`` under an explicit asset slug.

        The source is copied into a fresh tmp file first so :meth:`Vault.save_asset`'s
        move never consumes the caller's original (the Slack/MCP tmp download), and a
        multi-image batch (issue #84) can save each extra image under its own
        ``<slug>-N`` name while the primary keeps the bare ``<slug>``. The same
        bytes-SHA-256 idempotency/drift rule as :meth:`_save_fetched_asset` applies,
        and the staged tmp copy is always cleaned up on the skip/error path.
        """
        staged = self._stage_bytes(path.read_bytes())
        return self._store_asset(staged, f"{asset_slug}.{ext}")

    @staticmethod
    def _stage_bytes(data: bytes) -> Path:
        """Write ``data`` to a fresh tmp file consumed by :meth:`_store_asset`."""
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            return Path(handle.name)

    def _store_text_asset(self, asset_name: str, text: str) -> str | None:
        """Stage a derived *text* artifact and store it under ``raw/assets``.

        Used for the ``<slug>.excalidraw.md`` reconstruction (issue #68). The text is
        written to a fresh tmp file and handed to :meth:`_store_asset` (so the bytes-
        SHA-256 idempotency/drift rule applies and the tmp is never leaked). Returns the
        stored vault-relative path, or ``None`` if the (best-effort) write fails.

        Crucially, a derived artifact is an *enhancement* and must never lose or defer
        the already-durable primary capture: an :class:`IngestError` from
        :meth:`_store_asset` -- most realistically *drift*, because
        :meth:`~thoth.analyse.Analyser.reconstruct_excalidraw` is a non-deterministic
        model call so a byte-identical re-ingest produces a *different*
        ``<slug>.excalidraw.md`` -- is swallowed to ``None`` here (the existing asset
        is left untouched) rather than aborting the capture (ADR 0009).
        """
        staged = self._stage_bytes(text.encode("utf-8"))
        try:
            result = self._store_asset(staged, asset_name)
        except IngestError:
            return None
        return result.asset_paths[0] if result.asset_paths else None

    def _store_asset(self, tmp_path: Path, asset_name: str) -> RawCaptureResult:
        """Move ``tmp_path`` into ``raw/assets`` idempotently, never leaking the tmp.

        Compares the staged bytes' SHA-256 to any existing asset of the same name
        *before* the move: equal bytes mean an idempotent skip, different bytes mean
        drift (a loud error, never a silent overwrite), and a missing asset means a
        fresh create. The tmp/staged file is unlinked on every path that does not hand
        it to :meth:`Vault.save_asset` (skip and drift), and on a ``save_asset`` failure
        (for example a malformed asset filename), so no ``thoth-*`` temp file is leaked.

        Raises:
            IngestError: if the staged bytes differ from an existing asset's bytes
                (drift), or the vault rejects the write.
        """
        rel = f"raw/assets/{asset_name}"
        try:
            new_sha = Vault.bytes_sha256(tmp_path.read_bytes())
            if self._vault.asset_exists(asset_name):
                existing_sha = self._vault.asset_sha256(asset_name)
                if existing_sha != new_sha:
                    raise IngestError(
                        f"asset drift: {rel!r} already exists with different bytes; "
                        "refusing to overwrite (resolve in Obsidian)"
                    )
                return RawCaptureResult(
                    raw_path=None,
                    disposition="skipped_unchanged",
                    asset_paths=[rel],
                )
            written = self._vault.save_asset(tmp_path, asset_name)
            return RawCaptureResult(
                raw_path=None, disposition="created", asset_paths=[written]
            )
        except (SlugError, VaultError) as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc
        finally:
            # save_asset MOVES the tmp into the vault on success, leaving nothing to
            # clean. On a skip, a drift error, or a save_asset failure the bytes are
            # still staged, so unlink them here -- no thoth-* temp file is ever leaked.
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
