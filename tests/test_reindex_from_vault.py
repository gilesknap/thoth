"""Tests for :mod:`thoth.reindex_from_vault`.

Every external boundary is isolated. The vault is a **real**
:class:`~thoth.vault.Vault` over ``tmp_path`` with hand-authored curated pages, and the
manifest is a **real** JSON file under a tmp ``THOTH_HOME`` (the config is built with
``THOTH_HOME`` pointed into ``tmp_path``), so the body-hash idempotency key, the folder
walk, and the manifest round-trip are exercised for real. Hindsight is a
:class:`RecordingHindsight` fake recording every ``forget(rel)`` and
``retain(rel, facts, tags)`` call (and their order), and the bank-reset spawn goes
through a :class:`RecordingRunner` standing in for the
:class:`~thoth.hindsight.SubprocessRunner` seam -- so no ``hindsight-embed`` binary,
Postgres, or Gemini is touched and tests assert the exact argv and call sequence.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from thoth.budget import BudgetExceededError
from thoth.config import Config, load_config
from thoth.hindsight import DEFAULT_BANK, Hindsight, HindsightError, base_args
from thoth.reindex_from_vault import (
    INDEXED_DIRS,
    RESET_CONFIRM_FLAG,
    RESET_SUBCOMMAND,
    SKIP_FILES,
    Reindexer,
    ReindexError,
    ReindexResult,
    manifest_path,
    page_type,
)
from thoth.state import MARKER_REINDEX, MarkerStore
from thoth.vault import KNOWLEDGE_DIRS, Vault


def test_indexed_dirs_derive_from_canonical_vault_dirs() -> None:
    """INDEXED_DIRS = KNOWLEDGE_DIRS + LIFE_ADMIN_DIRS minus inbox (#40, ADR 0004).

    The reindex walk is derived from the canonical vault folder lists (issue #19 single
    source); this guard fails if it drifts from vault.py or starts walking inbox/.
    """
    from thoth.vault import LIFE_ADMIN_DIRS

    expected = (
        *KNOWLEDGE_DIRS,
        *(d for d in LIFE_ADMIN_DIRS if d != "inbox"),
    )
    assert INDEXED_DIRS == expected
    assert set(KNOWLEDGE_DIRS) <= set(INDEXED_DIRS)
    assert "inbox" not in INDEXED_DIRS


# --------------------------------------------------------------------------- #
# Fakes for the external boundaries.
# --------------------------------------------------------------------------- #


class RecordingHindsight(Hindsight):
    """A fake :class:`~thoth.hindsight.Hindsight` recording retain/forget calls.

    It subclasses :class:`~thoth.hindsight.Hindsight` so it is a drop-in *type* (no
    ``# type: ignore`` at call sites) yet overrides ``__init__`` to construct nothing
    and spawns no subprocess. ``retain`` records ``(rel, facts, tuple(tags))`` and
    ``forget`` records ``rel``; a single ``events`` list preserves the interleaved call
    order so a test can assert forget-then-retain. ``retain`` can be made to raise
    :class:`~thoth.hindsight.HindsightError` for a chosen path to exercise the
    error-wrapping path.

    Attributes:
        retains: Every ``(rel, facts, tags)`` retained, in call order.
        forgets: Every ``rel`` forgotten, in call order.
        events: Interleaved ``("retain"|"forget", rel)`` log preserving call order.
        fail_retain_for: A vault path for which ``retain`` raises HindsightError.
    """

    def __init__(self, *, fail_retain_for: str | None = None) -> None:
        """Build the recorder; no config or runner is needed (nothing is spawned)."""
        self.retains: list[tuple[str, str, tuple[str, ...]]] = []
        self.forgets: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.fail_retain_for = fail_retain_for
        # The Reindexer reads ``hindsight.bank`` to position the reset bank id; the real
        # __init__ is skipped here, so set the attribute the property reads.
        self._bank = DEFAULT_BANK

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Record a retain (or raise for the configured failing path)."""
        self.events.append(("retain", rel_path))
        if rel_path == self.fail_retain_for:
            raise HindsightError(f"backend unreachable for {rel_path}")
        self.retains.append((rel_path, facts, tuple(tags)))

    def forget(self, rel_path: str) -> None:
        """Record a best-effort forget (never raises, matching the real wrapper)."""
        self.events.append(("forget", rel_path))
        self.forgets.append(rel_path)


@dataclass
class RecordingRunner:
    """A fake :class:`~thoth.hindsight.SubprocessRunner` for ``reset_bank``.

    Records every ``argv``/``timeout`` and returns a canned completed process, so a test
    asserts the exact reset command line without spawning anything.

    Attributes:
        returncode: The exit code the canned result carries.
        stdout: The canned standard output.
        stderr: The canned standard error.
        calls: Every ``argv`` list seen, in call order.
        timeouts: Every ``timeout`` seen, in call order.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and return the canned completed process."""
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


# --------------------------------------------------------------------------- #
# Vault seeding helpers + fixtures.
# --------------------------------------------------------------------------- #


def _page(title: str, page_type_value: str, body: str, *, updated: str) -> str:
    """Render a minimal curated page (frontmatter + body) for seeding the vault."""
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type_value}\n"
        "created: 2026-05-30\n"
        f"updated: {updated}\n"
        "source: manual\n"
        "tags: [seed]\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


def _write(root: Path, rel: str, text: str) -> Path:
    """Write ``text`` to ``root/rel`` (creating parents); return the absolute path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A Config whose vault is ``tmp_path/vault`` and THOTH_HOME is ``tmp_path/home``.

    Pointing THOTH_HOME into ``tmp_path`` makes :func:`manifest_path` resolve under the
    tmp tree, so the manifest is a real on-disk file created by the run.
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    return load_config(
        {"PKM_VAULT": str(vault_root), "THOTH_HOME": str(tmp_path / "home")}
    )


@pytest.fixture
def vault(config: Config) -> Vault:
    """A real Vault over the tmp vault root."""
    return Vault(config)


def _seed_vault(root: Path) -> dict[str, str]:
    """Seed one page per indexed folder plus skip/excluded files; return label->rel map.

    Returns a mapping of a short label to the vault-relative path of the pages that
    *should* be indexed, so tests can refer to them by name. Per ADR 0004 the index now
    covers both the knowledge folders **and** the life-admin folders
    (``actions``/``media``/``memories``/``people``); only ``inbox/`` and ``raw/`` (and
    the spine / underscore dirs) stay excluded.
    """
    pages = {
        # knowledge
        "entity": "entities/program-motion-controller.md",
        "concept": "concepts/distributed-systems.md",
        "comparison": "comparisons/foo-vs-bar.md",
        "query": "queries/how-does-foo-work.md",
        # life-admin (now indexed too -- ADR 0004)
        "action": "actions/fix-fence.md",
        "media": "media/read-some-book.md",
        "memory": "memories/wifi-password.md",
        "person": "people/jane-doe.md",
    }
    _write(
        root,
        pages["entity"],
        _page("PMC", "entity", "Central coordinator.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["concept"],
        _page("Distributed Systems", "concept", "CAP notes.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["comparison"],
        _page("Foo vs Bar", "comparison", "Table here.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["query"],
        _page("How does foo work", "query", "Answer body.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["action"],
        _page("Fix fence", "action", "todo", updated="2026-05-30"),
    )
    _write(
        root,
        pages["media"],
        _page("Read some book", "media", "to read.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["memory"],
        _page("Wifi password", "memory", "it is hunter2.", updated="2026-05-30"),
    )
    _write(
        root,
        pages["person"],
        _page("Jane", "entity", "Collaborator.", updated="2026-05-30"),
    )

    # Files that must NEVER be retained:
    # - SKIP_FILES that happen to live inside an indexed folder
    _write(
        root,
        "entities/index.md",
        _page("Idx", "summary", "skip me", updated="2026-05-30"),
    )
    _write(
        root,
        "concepts/SCHEMA.md",
        _page("Schema", "summary", "skip me", updated="2026-05-30"),
    )
    _write(root, "queries/log.md", "# log\n")
    # - spine files at the root
    _write(root, "index.md", _page("Home", "summary", "home", updated="2026-05-30"))
    _write(root, "SCHEMA.md", "# schema\n")
    _write(root, "log.md", "# log\n")
    # - inbox/ holds transient deferred captures and stays excluded (ADR 0004)
    _write(
        root,
        "inbox/deadbeef12.md",
        _page("Held", "inbox", "deferred raw", updated="2026-05-30"),
    )
    # - non-indexed dirs (raw/ immutable source; underscore dirs are structure)
    _write(root, "raw/articles/some-clip.md", "---\ntype: entity\n---\n\nraw body\n")
    _write(root, "raw/assets/note.md", "stray\n")
    _write(root, "_bases/home.md", "base\n")
    _write(
        root,
        "_archive/old-entity.md",
        _page("Old", "entity", "archived", updated="2026-05-30"),
    )
    return pages


# --------------------------------------------------------------------------- #
# Pure helpers: page_type, manifest_path, body_hash.
# --------------------------------------------------------------------------- #


def test_page_type_parses_type_else_falls_back_to_page() -> None:
    """page_type reads a leading 'type:' value, else returns 'page'."""
    assert page_type("---\ntype: entity\ntitle: X\n---\n\nbody\n") == "entity"
    assert page_type("---\ntype:  concept \n---\n\nbody\n") == "concept"
    # No frontmatter / no type line -> neutral fallback.
    assert page_type("just a body, no frontmatter\n") == "page"
    assert page_type("---\ntitle: X\n---\n\nbody\n") == "page"


def test_manifest_path_is_under_thoth_home_hindsight(config: Config) -> None:
    """manifest_path is <thoth_home>/hindsight/reindex-manifest.json."""
    expected = config.thoth_home / "hindsight" / "reindex-manifest.json"
    assert manifest_path(config) == expected


def test_body_hash_equals_vault_body_sha256_with_and_without_frontmatter(
    vault: Vault, config: Config
) -> None:
    """body_hash strips frontmatter and equals Vault.body_sha256 of the body.

    The shared idempotency key must match what read_page would compute, both for a page
    that has a frontmatter block and for a bare body with none.
    """
    reindexer = Reindexer(config, vault, RecordingHindsight())
    body = "Central coordinator.\nMore detail."
    with_fm = f"---\ntitle: X\ntype: entity\nupdated: 2026-05-30\n---\n\n{body}"
    # With a frontmatter block: hash is over the body alone.
    assert reindexer.body_hash(with_fm) == vault.body_sha256(body)
    # Round-trips through the vault: equals Vault.body_sha256(read_page(...).body).
    rel = vault.write_page(
        "entities",
        "pmc",
        {"title": "X", "type": "entity", "source": "manual", "tags": ["t"]},
        body,
    )
    page = vault.read_page(rel)
    on_disk = (vault.root / rel).read_text(encoding="utf-8")
    assert reindexer.body_hash(on_disk) == vault.body_sha256(page.body)
    # With NO frontmatter block: the whole input is the body.
    assert reindexer.body_hash(body) == vault.body_sha256(body)


def test_body_hash_invariant_under_updated_frontmatter_bump(
    vault: Vault, config: Config
) -> None:
    """Bumping only the frontmatter 'updated' does not change the body hash."""
    reindexer = Reindexer(config, vault, RecordingHindsight())
    body = "Same body content."
    a = _page("X", "entity", body, updated="2026-05-30")
    b = _page("X", "entity", body, updated="2026-06-01")
    assert a != b  # the texts differ (frontmatter)
    assert reindexer.body_hash(a) == reindexer.body_hash(b)


class _BudgetTrippingHindsight(RecordingHindsight):
    """A recorder whose ``retain`` trips the daily budget after ``trip_after`` calls."""

    def __init__(self, *, trip_after: int) -> None:
        """Record retains until ``trip_after``, then raise BudgetExceededError."""
        super().__init__()
        self._trip_after = trip_after

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Retain until the cap, then act like the guarded wrapper (issue #16)."""
        if len(self.retains) >= self._trip_after:
            raise BudgetExceededError("daily LLM budget reached")
        super().retain(rel_path, facts, tags=tags)


def test_run_aborts_cleanly_when_budget_trips_mid_walk(
    config: Config, vault: Vault
) -> None:
    """A budget trip stops the rebuild: partial manifest, no prune/marker (issue #16).

    The pages retained before the cap are advanced in the manifest (so a later run does
    not re-spend on them), but the incomplete walk must NOT prune (unvisited pages are
    not deletions) and must NOT record the reindex liveness marker.
    """
    _seed_vault(vault.root)
    # Pre-seed a stale manifest entry for a page that no longer exists, so a *completed*
    # run would prune it; proving the abort skips pruning means this entry survives.
    hs = _BudgetTrippingHindsight(trip_after=1)
    markers = MarkerStore(config.state_db_path)
    reindexer = Reindexer(config, vault, hs, markers=markers)
    manifest = reindexer.load_manifest()
    manifest["entities/ghost.md"] = {"sha256": "deadbeef", "retained_at": "2026-05-30"}
    reindexer.write_manifest(manifest)

    result = reindexer.run()

    assert result.aborted is True
    assert result.changed == 1  # exactly one page retained before the cap
    assert result.pruned == 0  # pruning skipped on an incomplete walk
    assert len(hs.retains) == 1
    # The retained page is persisted; the ghost entry is NOT pruned (still present).
    persisted = reindexer.load_manifest()
    assert len(hs.retains[0]) == 3
    assert hs.retains[0][0] in persisted
    assert "entities/ghost.md" in persisted
    # No liveness marker: a budget-aborted reindex is not a healthy "alive" signal.
    assert markers.get(MARKER_REINDEX) is None


# --------------------------------------------------------------------------- #
# run(): first run retains everything; idempotent second run.
# --------------------------------------------------------------------------- #


def test_first_run_retains_every_curated_page_forget_then_retain(
    vault: Vault, config: Config
) -> None:
    """A fresh vault retains every curated page once (forget-then-retain) + manifest."""
    pages = _seed_vault(vault.root)
    hs = RecordingHindsight()
    result = Reindexer(config, vault, hs).run()

    assert isinstance(result, ReindexResult)
    assert result.changed == len(pages)
    assert result.skipped == 0
    assert result.pruned == 0
    assert result.live_pages == len(pages)
    assert result.full_rebuild is False

    # Exactly the four curated pages, each retained once.
    retained_rels = sorted(rel for rel, _, _ in hs.retains)
    assert retained_rels == sorted(pages.values())

    # forget-then-retain order: every retain is immediately preceded by a forget of the
    # same path.
    for index, (kind, rel) in enumerate(hs.events):
        if kind == "retain":
            assert hs.events[index - 1] == ("forget", rel)

    # The retained text is the BODY (frontmatter stripped) and matches the page body.
    by_rel = {rel: facts for rel, facts, _ in hs.retains}
    for rel in pages.values():
        page = vault.read_page(rel)
        assert by_rel[rel] == page.body
        assert "type:" not in by_rel[rel]  # frontmatter stripped

    # Tags carry [page_type, rel] -- for knowledge AND life-admin pages (ADR 0004), so
    # recall can scope by the page_type tag at query time.
    tags_by_rel = {rel: tags for rel, _, tags in hs.retains}
    assert tags_by_rel[pages["entity"]] == ("entity", pages["entity"])
    assert tags_by_rel[pages["query"]] == ("query", pages["query"])
    assert tags_by_rel[pages["memory"]] == ("memory", pages["memory"])
    assert tags_by_rel[pages["action"]] == ("action", pages["action"])

    # The manifest was written with a sha256 per page.
    manifest = json.loads(manifest_path(config).read_text(encoding="utf-8"))
    assert sorted(manifest) == sorted(pages.values())
    for rel in pages.values():
        assert "sha256" in manifest[rel]
        assert "retained_at" in manifest[rel]


# --------------------------------------------------------------------------- #
# liveness marker recorded on a successful reindex (issue #15).
# --------------------------------------------------------------------------- #


def test_successful_run_records_reindex_marker(vault: Vault, config: Config) -> None:
    """A completed reindex records the ``reindex`` liveness marker for the heartbeat."""
    _seed_vault(vault.root)
    markers = MarkerStore(config.state_db_path, clock=lambda: 7777.0)
    Reindexer(config, vault, RecordingHindsight(), markers=markers).run()
    assert markers.get(MARKER_REINDEX) == 7777.0


def test_empty_vault_run_still_records_reindex_marker(
    vault: Vault, config: Config
) -> None:
    """Even a no-op reindex (empty vault) records the marker -- it ran successfully."""
    markers = MarkerStore(config.state_db_path, clock=lambda: 1.0)
    result = Reindexer(config, vault, RecordingHindsight(), markers=markers).run()
    assert result.changed == 0
    assert markers.get(MARKER_REINDEX) == 1.0


def test_reindex_marker_write_failure_does_not_break_run(
    vault: Vault, config: Config
) -> None:
    """A MarkerStore that raises on record does not fail an otherwise-good reindex."""

    class _BoomMarkers:
        def record(self, name: str, *, ts: float | None = None) -> None:
            raise RuntimeError("marker db gone")

    _seed_vault(vault.root)
    reindexer = Reindexer(
        config, vault, RecordingHindsight(), markers=cast(MarkerStore, _BoomMarkers())
    )
    result = reindexer.run()
    assert result.changed > 0


def test_no_markers_store_reindex_is_a_clean_noop(vault: Vault, config: Config) -> None:
    """The default (no MarkerStore) reindexes normally and records nothing."""
    _seed_vault(vault.root)
    result = Reindexer(config, vault, RecordingHindsight()).run()
    assert result.changed > 0


def test_second_run_no_edits_skips_all_and_issues_zero_retains(
    vault: Vault, config: Config
) -> None:
    """An unchanged second run skips every page and does zero retain work."""
    pages = _seed_vault(vault.root)
    Reindexer(config, vault, RecordingHindsight()).run()

    hs2 = RecordingHindsight()
    result = Reindexer(config, vault, hs2).run()

    assert result.changed == 0
    assert result.skipped == len(pages)
    assert result.live_pages == len(pages)
    assert hs2.retains == []  # zero embedding work
    assert hs2.forgets == []  # no changed pages -> no forget either
    assert hs2.events == []


def test_editing_one_body_re_retains_only_that_page(
    vault: Vault, config: Config
) -> None:
    """A body change re-retains exactly the edited page on the next run."""
    pages = _seed_vault(vault.root)
    Reindexer(config, vault, RecordingHindsight()).run()

    # Change the body of one page (and its updated date, as a real edit would).
    edited = pages["concept"]
    _write(
        vault.root,
        edited,
        _page("Distributed Systems", "concept", "NEW body text.", updated="2026-06-02"),
    )

    hs2 = RecordingHindsight()
    result = Reindexer(config, vault, hs2).run()

    assert result.changed == 1
    assert result.skipped == len(pages) - 1
    assert [rel for rel, _, _ in hs2.retains] == [edited]


def test_frontmatter_only_change_does_not_re_retain(
    vault: Vault, config: Config
) -> None:
    """Bumping only frontmatter 'updated' (body unchanged) triggers no re-retain."""
    pages = _seed_vault(vault.root)
    Reindexer(config, vault, RecordingHindsight()).run()

    # Same body, only the 'updated' field differs.
    touched = pages["entity"]
    _write(
        vault.root,
        touched,
        _page("PMC", "entity", "Central coordinator.", updated="2026-06-09"),
    )

    hs2 = RecordingHindsight()
    result = Reindexer(config, vault, hs2).run()

    assert result.changed == 0
    assert result.skipped == len(pages)
    assert hs2.retains == []


# --------------------------------------------------------------------------- #
# Pruning deleted pages.
# --------------------------------------------------------------------------- #


def test_deleting_a_page_prunes_it_from_manifest(vault: Vault, config: Config) -> None:
    """A deleted page is forgotten and removed from the manifest on the next run."""
    pages = _seed_vault(vault.root)
    Reindexer(config, vault, RecordingHindsight()).run()

    gone = pages["comparison"]
    (vault.root / gone).unlink()

    hs2 = RecordingHindsight()
    result = Reindexer(config, vault, hs2).run()

    assert result.pruned == 1
    assert result.changed == 0
    assert result.skipped == len(pages) - 1
    assert result.live_pages == len(pages) - 1
    # The gone path was forgotten...
    assert gone in hs2.forgets
    # ...and dropped from the manifest.
    manifest = json.loads(manifest_path(config).read_text(encoding="utf-8"))
    assert gone not in manifest
    assert sorted(manifest) == sorted(rel for rel in pages.values() if rel != gone)


# --------------------------------------------------------------------------- #
# Full rebuild.
# --------------------------------------------------------------------------- #


def test_full_rebuild_resets_bank_then_re_retains_every_page(
    vault: Vault, config: Config
) -> None:
    """--full-rebuild wipes the bank (exact argv) then re-retains every live page."""
    pages = _seed_vault(vault.root)
    # Prime the manifest so a plain run would skip everything.
    Reindexer(config, vault, RecordingHindsight()).run()

    runner = RecordingRunner()
    hs2 = RecordingHindsight()
    result = Reindexer(config, vault, hs2, runner=runner).run(full_rebuild=True)

    assert result.full_rebuild is True
    # The reset ran exactly once with base_args + RESET_SUBCOMMAND + [-y, bank].
    assert runner.calls == [
        [*base_args(), *RESET_SUBCOMMAND, RESET_CONFIRM_FLAG, DEFAULT_BANK]
    ]
    # Every page re-retained despite matching manifest hashes.
    assert result.changed == len(pages)
    assert result.skipped == 0
    assert sorted(rel for rel, _, _ in hs2.retains) == sorted(pages.values())


def test_full_rebuild_reset_runs_before_any_retain(
    vault: Vault, config: Config
) -> None:
    """reset_bank is invoked before the first retain on a full rebuild."""
    _seed_vault(vault.root)

    order: list[str] = []

    class _OrderRunner(RecordingRunner):
        def __call__(
            self, argv: Sequence[str], *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            order.append("reset")
            return super().__call__(argv, timeout=timeout)

    class _OrderHindsight(RecordingHindsight):
        def retain(
            self, rel_path: str, facts: str, *, tags: Sequence[str] = ()
        ) -> None:
            order.append("retain")
            super().retain(rel_path, facts, tags=tags)

    Reindexer(config, vault, _OrderHindsight(), runner=_OrderRunner()).run(
        full_rebuild=True
    )
    assert order[0] == "reset"
    assert "retain" in order


def test_reset_bank_nonzero_exit_raises_reindexerror(
    vault: Vault, config: Config
) -> None:
    """A non-zero bank-reset exit raises ReindexError (full rebuild aborts)."""
    _seed_vault(vault.root)
    runner = RecordingRunner(returncode=2, stderr="backend unreachable")
    reindexer = Reindexer(config, vault, RecordingHindsight(), runner=runner)
    with pytest.raises(ReindexError) as exc_info:
        reindexer.reset_bank()
    assert "backend unreachable" in str(exc_info.value)


def test_reset_bank_passes_configured_timeout(vault: Vault, config: Config) -> None:
    """The configured timeout reaches the reset runner unchanged."""
    runner = RecordingRunner()
    reindexer = Reindexer(
        config,
        vault,
        RecordingHindsight(),
        runner=runner,
        timeout=9.5,
    )
    reindexer.reset_bank()
    assert runner.timeouts == [9.5]


def test_reset_bank_honours_base_args_override(vault: Vault, config: Config) -> None:
    """A base_args override re-points the reset CLI prefix (VPS-time seam)."""
    runner = RecordingRunner()
    reindexer = Reindexer(
        config,
        vault,
        RecordingHindsight(),
        runner=runner,
        base_args=("hindsight-embed", "-p", "other"),
    )
    reindexer.reset_bank()
    # The bank id stays positional after the (overridden) prefix + reset verb + -y.
    assert runner.calls == [
        [
            "hindsight-embed",
            "-p",
            "other",
            *RESET_SUBCOMMAND,
            RESET_CONFIRM_FLAG,
            DEFAULT_BANK,
        ]
    ]


def test_reset_bank_honours_bank_override(vault: Vault, config: Config) -> None:
    """A bank override re-points the positional bank id of the reset call."""
    runner = RecordingRunner()
    reindexer = Reindexer(
        config,
        vault,
        RecordingHindsight(),
        runner=runner,
        bank="otherbank",
    )
    reindexer.reset_bank()
    assert runner.calls == [
        [*base_args(), *RESET_SUBCOMMAND, RESET_CONFIRM_FLAG, "otherbank"]
    ]


def test_reset_bank_defaults_bank_to_the_hindsight_wrapper(
    vault: Vault, config: Config
) -> None:
    """When no bank override is given, the reset uses the wrapper's bank."""
    runner = RecordingRunner()
    hs = Hindsight(config, bank="wrapperbank")
    reindexer = Reindexer(config, vault, hs, runner=runner)
    reindexer.reset_bank()
    assert runner.calls == [
        [*base_args(), *RESET_SUBCOMMAND, RESET_CONFIRM_FLAG, "wrapperbank"]
    ]


# --------------------------------------------------------------------------- #
# Scope: only the four curated dirs, skipping SKIP_FILES and excluded dirs.
# --------------------------------------------------------------------------- #


def test_only_indexed_dirs_are_walked_skip_files_and_excluded_dirs_ignored(
    vault: Vault, config: Config
) -> None:
    """SKIP_FILES and non-INDEXED_DIRS never retained; knowledge + life-admin are."""
    pages = _seed_vault(vault.root)
    hs = RecordingHindsight()
    Reindexer(config, vault, hs).run()

    retained = {rel for rel, _, _ in hs.retains}
    assert retained == set(pages.values())
    # Life-admin pages are now in the index (ADR 0004); inbox/ and raw/ are not.
    assert "memories/wifi-password.md" in retained
    assert "actions/fix-fence.md" in retained
    assert "people/jane-doe.md" in retained

    # Spine files inside indexed folders are skipped.
    assert "entities/index.md" not in retained
    assert "concepts/SCHEMA.md" not in retained
    assert "queries/log.md" not in retained
    # Excluded dirs never appear: transient inbox/, immutable raw/, underscore dirs.
    for rel in retained:
        assert not rel.startswith("inbox/")
        assert not rel.startswith("raw/")
        assert not rel.startswith("_bases/")
        assert not rel.startswith("_archive/")


def test_indexed_dirs_and_skip_files_constants_are_as_specified() -> None:
    """INDEXED_DIRS = knowledge + life-admin minus inbox (ADR 0004); spine skipped."""
    assert INDEXED_DIRS == (
        "entities",
        "concepts",
        "comparisons",
        "queries",
        "actions",
        "media",
        "memories",
        "people",
    )
    assert "inbox" not in INDEXED_DIRS
    assert SKIP_FILES == frozenset({"SCHEMA.md", "index.md", "log.md"})


def test_run_on_empty_vault_is_a_noop_with_empty_manifest(
    vault: Vault, config: Config
) -> None:
    """A vault lacking the curated folders yields all-zero counts and {} manifest."""
    hs = RecordingHindsight()
    result = Reindexer(config, vault, hs).run()
    assert result == ReindexResult(
        changed=0, skipped=0, pruned=0, live_pages=0, full_rebuild=False
    )
    assert hs.events == []
    assert json.loads(manifest_path(config).read_text(encoding="utf-8")) == {}


# --------------------------------------------------------------------------- #
# Manifest robustness: parent dir creation, missing/corrupt file -> {}.
# --------------------------------------------------------------------------- #


def test_run_creates_manifest_parent_dir(vault: Vault, config: Config) -> None:
    """run() creates the manifest parent dir (it does not exist on a fresh box)."""
    _seed_vault(vault.root)
    assert not manifest_path(config).parent.exists()
    Reindexer(config, vault, RecordingHindsight()).run()
    assert manifest_path(config).is_file()


def test_missing_manifest_is_treated_as_empty(vault: Vault, config: Config) -> None:
    """A missing manifest loads as {} (no crash); the first run treats all changed."""
    pages = _seed_vault(vault.root)
    reindexer = Reindexer(config, vault, RecordingHindsight())
    assert reindexer.load_manifest() == {}
    result = reindexer.run()
    assert result.changed == len(pages)


def test_corrupt_manifest_is_treated_as_empty(vault: Vault, config: Config) -> None:
    """A corrupt manifest file loads as {} rather than raising."""
    pages = _seed_vault(vault.root)
    path = manifest_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")

    reindexer = Reindexer(config, vault, RecordingHindsight())
    assert reindexer.load_manifest() == {}
    # And a run recovers by re-retaining everything.
    result = reindexer.run()
    assert result.changed == len(pages)


def test_manifest_round_trips_through_write_and_load(
    vault: Vault, config: Config
) -> None:
    """write_manifest then load_manifest returns an equivalent mapping."""
    reindexer = Reindexer(config, vault, RecordingHindsight())
    manifest = {
        "entities/foo.md": {
            "sha256": "abc",
            "retained_at": "2026-05-30T00:00:00+00:00",
        },
        "concepts/bar.md": {
            "sha256": "def",
            "retained_at": "2026-05-30T00:00:00+00:00",
        },
    }
    reindexer.write_manifest(manifest)
    assert reindexer.load_manifest() == manifest


# --------------------------------------------------------------------------- #
# Error wrapping: a retain HindsightError becomes ReindexError.
# --------------------------------------------------------------------------- #


def test_retain_hindsighterror_is_wrapped_as_reindexerror(
    vault: Vault, config: Config
) -> None:
    """A retain HindsightError surfaces as ReindexError and stops the run."""
    pages = _seed_vault(vault.root)
    failing = pages["entity"]
    hs = RecordingHindsight(fail_retain_for=failing)
    reindexer = Reindexer(config, vault, hs)
    with pytest.raises(ReindexError) as exc_info:
        reindexer.run()
    assert failing in str(exc_info.value)


def test_manifest_not_advanced_for_failed_retain(vault: Vault, config: Config) -> None:
    """When a retain fails, that page's manifest entry is not advanced.

    The page is retained successfully on the FIRST run; we then change its body and make
    the next retain fail, and assert the manifest still carries the OLD hash (so a later
    healthy run will re-attempt it) -- the failure did not record the new body as done.
    """
    pages = _seed_vault(vault.root)
    Reindexer(config, vault, RecordingHindsight()).run()
    before = json.loads(manifest_path(config).read_text(encoding="utf-8"))

    failing = pages["entity"]
    _write(
        vault.root,
        failing,
        _page("PMC", "entity", "CHANGED body.", updated="2026-06-03"),
    )
    hs = RecordingHindsight(fail_retain_for=failing)
    with pytest.raises(ReindexError):
        Reindexer(config, vault, hs).run()

    after = json.loads(manifest_path(config).read_text(encoding="utf-8"))
    # The failing page's hash is unchanged from before the failed edit-run...
    assert after[failing]["sha256"] == before[failing]["sha256"]


# --------------------------------------------------------------------------- #
# Type-compatibility with the real Hindsight + import safety.
# --------------------------------------------------------------------------- #


def test_reindexer_accepts_a_real_hindsight_instance(
    vault: Vault, config: Config
) -> None:
    """A real Hindsight (no spawn here) wires up; reset uses its default seam type."""
    hs = Hindsight(config)
    reindexer = Reindexer(config, vault, hs)
    # No process is spawned: the manifest path resolves and an empty vault is a no-op.
    result = reindexer.run()
    assert result.live_pages == 0


def test_module_import_pulls_in_no_hindsight_package() -> None:
    """Importing thoth.reindex_from_vault imports no 'hindsight' Python package.

    The module is pure stdlib + thoth.*; a stray ``import hindsight`` would break
    collection in CI where the package is absent.
    """
    import thoth.reindex_from_vault  # noqa: F401

    leaked = [
        name
        for name in sys.modules
        if name == "hindsight" or name.startswith("hindsight.")
    ]
    assert leaked == []
