"""Tests for :mod:`thoth.inbox_drain` -- the source-independent inbox-hold sweep (#105).

A real seeded vault under ``tmp_path`` carries a handful of ``inbox/hold-*.md`` pages;
:func:`thoth.inbox_drain.drain_captures` walks them and yields one
:class:`~thoth.ingest.Capture` per recoverable TEXT hold (binary stubs are skipped). No
network, no LLM -- the drain is pure read + Capture construction.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from thoth.config import Config, load_config
from thoth.inbox_drain import drain_captures
from thoth.vault import Vault

_FOLDERS = ("entities", "notes", "memories", "actions", "inbox", "raw/assets")


def _hold(
    *,
    source: str | None,
    body: str,
    mode: str | None = None,
    filename: str | None = None,
) -> str:
    """Render a minimal ``inbox/hold-*`` page (type: inbox) with given fields."""
    source_line = f"source: {source}\n" if source is not None else ""
    mode_line = f"mode: {mode}\n" if mode is not None else ""
    filename_line = f"filename: {filename}\n" if filename is not None else ""
    return (
        "---\n"
        "title: Held capture\n"
        "type: inbox\n"
        f"{source_line}"
        f"{mode_line}"
        f"{filename_line}"
        "tags: [inbox]\n"
        "---\n\n"
        f"{body}\n"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A real Vault over a freshly skeletoned tmp vault."""
    root = tmp_path / "pkm-vault"
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    config: Config = load_config({"PKM_VAULT": str(root)})
    return Vault(config)


def _write_hold(
    vault: Vault,
    name: str,
    *,
    source: str | None,
    body: str,
    mode: str | None = None,
    filename: str | None = None,
) -> str:
    """Write ``inbox/<name>`` and return its vault-relative path."""
    (vault.root / "inbox" / name).write_text(
        _hold(source=source, body=body, mode=mode, filename=filename), encoding="utf-8"
    )
    return f"inbox/{name}"


def test_drain_captures_yields_capture_per_text_hold(vault: Vault) -> None:
    """Each text hold yields a Capture whose text == stored body, source threaded."""
    _write_hold(vault, "hold-aaa111.md", source="slack", body="a note about dogs")
    _write_hold(vault, "hold-bbb222.md", source="mcp", body="a note about cats")
    drained = list(drain_captures(vault))
    assert [hold.rel for hold in drained] == [
        "inbox/hold-aaa111.md",
        "inbox/hold-bbb222.md",
    ]
    by_source = {hold.capture.source: hold.capture.text for hold in drained}
    assert by_source == {"slack": "a note about dogs", "mcp": "a note about cats"}


def test_drain_captures_skips_binary_stub_holds(
    vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    """A hold whose body is the binary provenance stub is skipped and logged."""
    stub = (
        "# Held capture\n\n"
        "Binary source: `photo.jpg`\n\n"
        "_Unsupported binary content held at capture time; queued for a later "
        "reindex/sweep to fetch and curate._"
    )
    _write_hold(vault, "hold-bin999.md", source="slack", body=stub)
    _write_hold(vault, "hold-txt111.md", source="slack", body="real text")
    with caplog.at_level(logging.INFO, logger="thoth.inbox_drain"):
        drained = list(drain_captures(vault))
    assert [hold.rel for hold in drained] == ["inbox/hold-txt111.md"]
    assert any("binary stub" in r.getMessage() for r in caplog.records)


def test_drain_captures_invalid_source_falls_back_to_import(vault: Vault) -> None:
    """A missing/garbage source yields a Capture with source=='import', no crash."""
    _write_hold(vault, "hold-nosrc.md", source=None, body="no source here")
    _write_hold(vault, "hold-bad.md", source="not-a-source", body="bad source")
    by_text = {h.capture.text: h.capture.source for h in drain_captures(vault)}
    assert by_text == {"no source here": "import", "bad source": "import"}


def test_drain_captures_sorted_deterministic(vault: Vault) -> None:
    """Holds are returned in sorted path order regardless of write order."""
    _write_hold(vault, "hold-ccc.md", source="slack", body="c")
    _write_hold(vault, "hold-aaa.md", source="slack", body="a")
    _write_hold(vault, "hold-bbb.md", source="slack", body="b")
    rels = [hold.rel for hold in drain_captures(vault)]
    assert rels == ["inbox/hold-aaa.md", "inbox/hold-bbb.md", "inbox/hold-ccc.md"]


def test_drain_captures_honours_stamped_mode_and_filename(vault: Vault) -> None:
    """A hold's stamped mode/filename are read back so the sweep honours intent."""
    _write_hold(
        vault,
        "hold-asis.md",
        source="import",
        body="raw dump",
        mode="as-is",
        filename="notes.md",
    )
    _write_hold(vault, "hold-cur.md", source="slack", body="curate me", mode="curate")
    by_text = {h.capture.text: h for h in drain_captures(vault)}
    assert by_text["raw dump"].as_is is True
    assert by_text["raw dump"].capture.filename == "notes.md"
    assert by_text["curate me"].as_is is False
    assert by_text["curate me"].capture.filename is None


def test_drain_captures_missing_mode_falls_back_to_curate(vault: Vault) -> None:
    """A hold with no/unknown mode defaults to re-curate (as_is False), no crash."""
    _write_hold(vault, "hold-nomode.md", source="slack", body="legacy hold")
    _write_hold(
        vault, "hold-weird.md", source="slack", body="weird mode", mode="nonsense"
    )
    assert all(h.as_is is False for h in drain_captures(vault))


def test_drain_captures_empty_inbox(vault: Vault) -> None:
    """An inbox with no holds yields nothing (no crash)."""
    assert list(drain_captures(vault)) == []
