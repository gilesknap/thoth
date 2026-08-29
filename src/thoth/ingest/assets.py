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
    """Stages binaries into ``raw/assets`` under the digest idempotency rule."""

    def _obtain_primary_asset(
        self,
        capture: Capture,
        cls: Classification,
        fetched: FetchedBinary | None,
        *,
        local_ext: str,
    ) -> tuple[RawCaptureResult, str | None]:
        """Acquires a binary capture's primary asset and any provenance URL.

        The shared acquisition step for both binary kinds. A URL capture reuses the
        analyse pass's single download when present, so there is no second fetch and no
        leaked temp, falling back to fetching for a standalone call. A local path
        capture is staged under the given extension with no provenance URL.
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
        """Moves a fetched binary's temp file into the asset folder.

        Idempotent on the bytes. An identical destination skips the move and cleans up
        the staged file, and a byte mismatch at the same slug surfaces as drift rather
        than an overwrite. The happy path moves the temp file, so only the skip and
        error paths must clean it up.
        """
        asset_name = f"{cls.slug}.{fetched.suggested_ext}"
        return self._store_asset(fetched.tmp_path, asset_name)

    def _save_local_asset_result_named(
        self, asset_slug: str, path: Path, ext: str
    ) -> RawCaptureResult:
        """Stages a local file into the asset folder under an explicit slug.

        The source is copied to a fresh temp file first, so the vault's move never
        consumes the caller's original. That also lets a multi-image batch (issue #84)
        save each extra under its own numbered name while the primary keeps the bare
        slug. The same digest idempotency rule applies, and the staged copy is always
        cleaned up on the skip and error paths.
        """
        staged = self._stage_bytes(path.read_bytes())
        return self._store_asset(staged, f"{asset_slug}.{ext}")

    @staticmethod
    def _stage_bytes(data: bytes) -> Path:
        """Writes bytes to a fresh temp file, consumed by :meth:`_store_asset`."""
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            return Path(handle.name)

    def _store_text_asset(self, asset_name: str, text: str) -> str | None:
        """Stages a derived text artifact and stores it under the asset folder.

        Used for the Excalidraw reconstruction (issue #68). The text is written to a
        fresh temp file, so the same digest rule applies and nothing is leaked.

        A derived artifact is an enhancement and must never lose or defer the
        already-durable primary capture. A store failure is swallowed to None here,
        leaving the existing asset untouched rather than aborting (ADR 0009).

        Drift is the realistic failure: reconstruction is a non-deterministic model
        call, so a byte-identical re-ingest produces a different scene.
        """
        staged = self._stage_bytes(text.encode("utf-8"))
        try:
            result = self._store_asset(staged, asset_name)
        except IngestError:
            return None
        return result.asset_paths[0] if result.asset_paths else None

    def _store_asset(self, tmp_path: Path, asset_name: str) -> RawCaptureResult:
        """Moves a staged file into the asset folder idempotently, leaking nothing.

        The staged bytes are compared to any existing asset of the same name before the
        move. Equal bytes are an idempotent skip, different bytes are drift and a loud
        error rather than a silent overwrite, and a missing asset is a fresh create. The
        staged file is unlinked on every path that does not hand it to the vault, and on
        a vault failure too, so no temp file is ever leaked.

        Raises:
            IngestError: on drift against an existing asset, or a rejected write.
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
            # The vault moves the temp file in on success, leaving nothing to clean. On
            # a skip, a drift error, or a failed write the bytes are still staged, so
            # unlink them here and no temp file is ever leaked
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
