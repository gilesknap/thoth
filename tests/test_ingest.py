"""Tests for :mod:`thoth.ingest` -- the bounded-pass capture pipeline.

These exercise the orchestrator with every external boundary isolated: a fake
Anthropic client (driving a real :class:`thoth.llm.LLM`) returns canned classify and
file-plan JSON, a fake :class:`thoth.extract.Extractor` returns canned documents or
raises, a fake :class:`thoth.hindsight.Hindsight` records ``retain``/``probe`` calls,
and a REAL :class:`thoth.git_sync.GitSync` runs against a LOCAL bare repo in
``tmp_path`` (no network, no GitHub, no ``gh`` helper). The :class:`thoth.vault.Vault`
is real over a seeded temporary vault, so the closed-surface validators run for real and
a rejected plan provably writes nothing outside the vault root.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from thoth.config import Config, load_config
from thoth.extract import ExtractedDoc, FetchedBinary, FetchError, SsrfError
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.hindsight import HindsightError, RecallHit
from thoth.ingest import (
    _CURATE_ATTEMPTS,
    Capture,
    CaptureKind,
    Classification,
    IngestError,
    Ingestor,
    IngestReport,
    RawCaptureResult,
)
from thoth.llm import LLM
from thoth.state import MARKER_CAPTURE, MARKER_PUSH, MarkerStore
from thoth.vault import TYPE_ENUMERATION, Vault

MAIN = "main"

# --------------------------------------------------------------------------- #
# Fakes for the injected boundaries.
# --------------------------------------------------------------------------- #


def _text_response(text: str) -> dict[str, Any]:
    """Shape a fake Anthropic response as :func:`thoth.llm.extract_text` reads it."""
    return {"content": [{"type": "text", "text": text}]}


class _ScriptedMessages:
    """A fake ``client.messages`` returning the next scripted response per call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        """Store the ordered canned responses and record every create kwargs."""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call and pop the next canned response (last one repeats)."""
        self.calls.append(kwargs)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _ScriptedClient:
    """A fake Anthropic client exposing :class:`_ScriptedMessages`."""

    def __init__(self, *texts: str) -> None:
        """Build a client whose ``messages.create`` returns each ``text`` in turn."""
        self.messages = _ScriptedMessages([_text_response(t) for t in texts])


class _RaisingClient:
    """A fake client whose ``messages.create`` always raises (LLM-failure path)."""

    class _Messages:
        def create(self, **kwargs: Any) -> Any:
            """Always raise to simulate a transport failure."""
            raise RuntimeError("boom")

    def __init__(self) -> None:
        """Build the always-raising client."""
        self.messages = _RaisingClient._Messages()


@dataclass
class _RecordedRetain:
    """One recorded :meth:`FakeHindsight.retain` call."""

    rel_path: str
    facts: str
    tags: tuple[str, ...]


class FakeExtractor:
    """A fake :class:`thoth.extract.Extractor` returning canned results or raising."""

    def __init__(
        self,
        *,
        doc: ExtractedDoc | None = None,
        binary: FetchedBinary | None = None,
        transcript: str = "",
        web_extract_error: Exception | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        """Configure the canned outputs / errors for each method."""
        self._doc = doc
        self._binary = binary
        self._transcript = transcript
        self._web_extract_error = web_extract_error
        self._fetch_error = fetch_error
        self.web_extract_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.transcribe_calls: list[Path] = []

    def web_extract(self, url: str) -> ExtractedDoc:
        """Return the canned :class:`ExtractedDoc` or raise the configured error."""
        self.web_extract_calls.append(url)
        if self._web_extract_error is not None:
            raise self._web_extract_error
        assert self._doc is not None
        return self._doc

    def fetch_binary(self, url: str) -> FetchedBinary:
        """Return the canned :class:`FetchedBinary` or raise the configured error."""
        self.fetch_calls.append(url)
        if self._fetch_error is not None:
            raise self._fetch_error
        assert self._binary is not None
        return self._binary

    def transcribe(self, audio_path: Path, *, model: str = "base") -> str:
        """Return the canned transcript text."""
        self.transcribe_calls.append(audio_path)
        return self._transcript


class _ConflictingGitSync(GitSync):
    """A real GitSync whose ``commit`` deterministically raises a conflict.

    ``pull`` still runs for real against the local bare repo (so pass 0 behaves
    normally); only the commit-push step simulates the post-pull origin divergence that
    yields a rebase conflict, which is racy to reproduce with two clones in-process.
    """

    def commit(self, message: str, *, timeout: float = 120.0) -> Any:
        """Raise :class:`VaultConflictError` as the vault-commit script would."""
        raise VaultConflictError(
            "vault-commit failed (exit 1). stderr: 'VAULT CONFLICT: resolve'"
        )


class _FailingGitSync(GitSync):
    """A real GitSync whose ``commit`` raises a plain (non-conflict) GitSyncError.

    ``pull`` still runs for real against the local bare repo; only the commit-push step
    fails as a generic push/transport error would, to exercise the deferred path's
    ``except GitSyncError`` branch (distinct from the rebase-conflict subclass).
    """

    def commit(self, message: str, *, timeout: float = 120.0) -> Any:
        """Raise :class:`GitSyncError` as a failed push would (no conflict)."""
        raise GitSyncError("vault-commit failed (exit 1). stderr: 'push rejected'")


class FakeHindsight:
    """A fake :class:`thoth.hindsight.Hindsight` recording retain/probe calls."""

    def __init__(
        self,
        *,
        retain_error: Exception | None = None,
        probe_result: bool = True,
        probe_error: Exception | None = None,
    ) -> None:
        """Configure recorded behaviour and any errors to raise."""
        self._retain_error = retain_error
        self._probe_result = probe_result
        self._probe_error = probe_error
        self.retained: list[_RecordedRetain] = []
        self.probed: list[tuple[str, str]] = []

    def retain(self, rel_path: str, facts: str, *, tags: Sequence[str] = ()) -> None:
        """Record the retain call or raise the configured error."""
        self.retained.append(_RecordedRetain(rel_path, facts, tuple(tags)))
        if self._retain_error is not None:
            raise self._retain_error

    def probe(self, rel_path: str, query: str) -> bool:
        """Record the probe call and return the canned result (or raise)."""
        self.probed.append((rel_path, query))
        if self._probe_error is not None:
            raise self._probe_error
        return self._probe_result

    def recall(self, query: str, *, limit: int = 10) -> list[RecallHit]:
        """Unused by ingest; present for interface completeness."""
        return []


# --------------------------------------------------------------------------- #
# Canned model output.
# --------------------------------------------------------------------------- #


def _classify_json(
    *,
    page_type: str = "note",
    slug: str = "transformer-models",
    title: str = "Transformer Models",
    entities: list[str] | None = None,
    concepts: list[str] | None = None,
) -> str:
    """Build a classify-call JSON string."""
    return json.dumps(
        {
            "type": page_type,
            "slug": slug,
            "title": title,
            "entities": entities or [],
            "concepts": concepts or ["transformer-models"],
        }
    )


def _file_plan_json(
    *,
    folder: str = "notes",
    slug: str = "transformer-models",
    page_type: str = "note",
    title: str = "Transformer Models",
    body: str = "Transformers use attention.",
    wikilinks: list[str] | None = None,
    embeds: list[str] | None = None,
    index_entries: list[dict[str, str]] | None = None,
    extra_pages: list[dict[str, Any]] | None = None,
) -> str:
    """Build a curate-call file-plan JSON string with one (or more) pages."""
    page: dict[str, Any] = {
        "action": "create",
        "folder": folder,
        "slug": slug,
        "frontmatter": {
            "title": title,
            "type": page_type,
            # The model supplies created/updated placeholders (validate_file_plan
            # requires them); Vault.write_page re-stamps them at write time.
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["ai-ml"],
        },
        "body": body,
        "wikilinks": wikilinks or ["[[attention]]", "[[neural-networks]]"],
    }
    if embeds is not None:
        page["embeds"] = embeds
    pages: list[dict[str, Any]] = [page, *(extra_pages or [])]
    plan: dict[str, Any] = {"pages": pages}
    if index_entries is not None:
        plan["index_entries"] = index_entries
    return json.dumps(plan)


# --------------------------------------------------------------------------- #
# Seeded vault + real GitSync over a local bare repo.
# --------------------------------------------------------------------------- #

_INDEX_SEED = """\
---
title: Home
type: summary
updated: 2026-05-30
---

# Home

## Knowledge catalog

### Entities

### Notes

### Memories
"""

_LOG_SEED = """\
# Vault Log

> Append-only.

## [2026-05-30] create | Vault initialized
- structure seeded
"""

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "notes",
    "memories",
    "actions",
    "inbox",
)


def _git(cwd: Path, *args: str) -> str:
    """Run a git command with global/system config neutralised; return stdout."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _seed_vault(root: Path) -> None:
    """Write the minimal vault skeleton (folders + index.md + log.md) under ``root``."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (root / "log.md").write_text(_LOG_SEED, encoding="utf-8")


@dataclass(frozen=True)
class IngestHarness:
    """A ready-to-use ingest playground: real vault + real GitSync + fakes."""

    config: Config
    vault: Vault
    git: GitSync
    work: Path
    bare: Path
    other: Path
    env: dict[str, str]

    def origin_files(self) -> list[str]:
        """Return the tracked file list at the bare repo's HEAD."""
        out = _git(self.bare, "ls-tree", "-r", "--name-only", MAIN)
        return [line for line in out.splitlines() if line]


@pytest.fixture
def harness(tmp_path: Path) -> IngestHarness:
    """Build a seeded vault inside a git work clone wired to a local bare origin."""
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", "-b", MAIN, str(bare))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.email", "tester@example.invalid")
    _git(seed, "config", "user.name", "thoth-test")
    _seed_vault(seed)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init vault spine")
    _git(seed, "push", "origin", MAIN)

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "tester@example.invalid")
    _git(work, "config", "user.name", "thoth-test")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    _git(other, "config", "user.email", "tester@example.invalid")
    _git(other, "config", "user.name", "thoth-test")

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["THOTH_GIT_REMOTE"] = "origin"
    env["THOTH_GIT_BRANCH"] = MAIN
    env["THOTH_PUSH_REMOTE"] = str(bare)
    env.pop("PKM_VAULT", None)

    config = load_config({"PKM_VAULT": str(work)})
    vault = Vault(config)
    git = GitSync(config, env=env)
    return IngestHarness(
        config=config,
        vault=vault,
        git=git,
        work=work,
        bare=bare,
        other=other,
        env=env,
    )


def _make_binary(tmp_path: Path, *, ext_hint: str = "png") -> FetchedBinary:
    """Stage a tiny fake binary in a tmp file and wrap it as a FetchedBinary."""
    src = tmp_path / f"download-staged.{ext_hint}"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes")
    return FetchedBinary(
        source_url="https://example.com/pic",
        tmp_path=src,
        content_type=f"image/{ext_hint}",
        suggested_ext=ext_hint,
    )


def _build_ingestor(
    harness: IngestHarness,
    *,
    client: Any,
    extractor: FakeExtractor,
    hindsight: FakeHindsight,
    markers: MarkerStore | None = None,
    guard: Any = None,
) -> Ingestor:
    """Wire an :class:`Ingestor` with a real LLM (fake client) + the given fakes."""
    llm = LLM(harness.config, client=client, guard=guard)
    return Ingestor(
        harness.config,
        harness.vault,
        llm,
        extractor,  # type: ignore[arg-type]  # structural fake
        hindsight,  # type: ignore[arg-type]  # structural fake
        harness.git,
        markers=markers,
    )


# --------------------------------------------------------------------------- #
# Dataclass + import-safety smoke tests.
# --------------------------------------------------------------------------- #


def test_capture_kind_enum_values() -> None:
    """CaptureKind has the five documented string values."""
    assert {k.value for k in CaptureKind} == {"url", "pdf", "image", "audio", "text"}


def test_dataclasses_construct_with_defaults() -> None:
    """The frozen result dataclasses build with their documented defaults."""
    cls = Classification(page_type="note", slug="x", title="X")
    assert cls.entities == [] and cls.concepts == []
    raw = RawCaptureResult(raw_path=None, disposition="none")
    assert raw.asset_paths == []
    report = IngestReport(
        page_paths=[],
        raw_paths=[],
        asset_paths=[],
        obsidian_links=[],
        wikilinks=[],
        committed=False,
    )
    assert report.conflict is False and report.message == ""


def test_module_import_is_light() -> None:
    """Importing thoth.ingest pulls in no heavy/absent third-party client."""
    import sys

    import thoth.ingest  # noqa: F401  (import for the side effect of collection)

    for heavy in ("anthropic", "exa_py", "firecrawl", "slack_bolt", "whisper"):
        assert heavy not in sys.modules


# --------------------------------------------------------------------------- #
# Happy path: URL capture end-to-end.
# --------------------------------------------------------------------------- #


def test_ingest_url_happy_path(harness: IngestHarness) -> None:
    """A URL capture writes raw + curated pages, navigates, retains, and commits."""
    doc = ExtractedDoc(
        source_url="https://example.com/transformers",
        title="Transformer Models",
        markdown="Transformers use attention to weigh tokens.",
    )
    client = _ScriptedClient(_classify_json(), _file_plan_json())
    extractor = FakeExtractor(doc=doc)
    hindsight = FakeHindsight()
    ingestor = _build_ingestor(
        harness, client=client, extractor=extractor, hindsight=hindsight
    )

    report = ingestor.ingest(Capture(url="https://example.com/transformers"))

    # Curated page + raw page written and on disk.
    assert report.page_paths == ["notes/transformer-models.md"]
    assert report.raw_paths == ["raw/articles/transformer-models.md"]
    assert harness.vault.page_exists("notes/transformer-models.md")
    assert harness.vault.page_exists("raw/articles/transformer-models.md")

    # Hindsight retained exactly once for the one curated page, with type + path tags.
    assert len(hindsight.retained) == 1
    retained = hindsight.retained[0]
    assert retained.rel_path == "notes/transformer-models.md"
    assert retained.tags == ("note", "notes/transformer-models.md")
    assert hindsight.probed == [("notes/transformer-models.md", "Transformer Models")]

    # obsidian link is harness-built (matches Vault.obsidian_uri), not from the model.
    assert report.obsidian_links == [
        harness.vault.obsidian_uri("notes/transformer-models.md")
    ]
    assert report.wikilinks == ["[[transformer-models]]"]

    # Committed to the local origin.
    assert report.committed is True
    assert report.conflict is False
    assert "notes/transformer-models.md" in harness.origin_files()


def test_ingest_appends_index_and_log(harness: IngestHarness) -> None:
    """A note page lands a Notes catalog line and a log block."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body text")
    client = _ScriptedClient(_classify_json(), _file_plan_json())
    ingestor = _build_ingestor(
        harness,
        client=client,
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    ingestor.ingest(Capture(url="https://e.com/a"))

    index_text = (harness.work / "index.md").read_text(encoding="utf-8")
    assert "[[transformer-models]]" in index_text
    log_text = (harness.work / "log.md").read_text(encoding="utf-8")
    assert "ingest" in log_text
    assert "notes/transformer-models.md" in log_text


def test_ingest_uses_model_supplied_index_entry(harness: IngestHarness) -> None:
    """When the plan carries index_entries the harness applies them verbatim."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    plan = _file_plan_json(
        index_entries=[
            {
                "section": "Notes",
                "wikilink": "transformer-models",
                "summary": "attention-based sequence models",
            }
        ]
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    ingestor.ingest(Capture(url="https://e.com/a"))

    index_text = (harness.work / "index.md").read_text(encoding="utf-8")
    assert "[[transformer-models]] - attention-based sequence models" in index_text


# --------------------------------------------------------------------------- #
# Closed-surface: rejection paths write nothing escaping the vault.
# --------------------------------------------------------------------------- #


def test_curate_rejects_folder_type_mismatch(harness: IngestHarness) -> None:
    """A page whose folder/type pair is illegal is rejected and not written."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    # type=concept is NOT allowed in actions/ -> validate_file_plan rejects it.
    bad_plan = _file_plan_json(folder="actions", page_type="note")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), bad_plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    with pytest.raises(IngestError):
        ingestor.ingest(Capture(url="https://e.com/a"))

    assert not (harness.work / "actions" / "transformer-models.md").exists()
    assert not harness.vault.page_exists("notes/transformer-models.md")


def test_curate_rejects_escaping_slug(harness: IngestHarness, tmp_path: Path) -> None:
    """A slug that tries to escape the vault root never creates a file outside it."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    # validate_slug rejects slashes/.. so the plan fails validation before any write.
    bad_plan = _file_plan_json(slug="../../etc/passwd")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), bad_plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    with pytest.raises(IngestError):
        ingestor.ingest(Capture(url="https://e.com/a"))

    # Nothing created anywhere outside the vault work tree.
    assert not (tmp_path / "etc").exists()
    assert not (harness.work.parent / "etc").exists()


def test_curate_rejects_absolute_folder(harness: IngestHarness) -> None:
    """An unknown/absolute folder is rejected by the folder/type contract."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    bad_plan = _file_plan_json(folder="/etc")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), bad_plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    with pytest.raises(IngestError):
        ingestor.ingest(Capture(url="https://e.com/a"))
    assert not (Path("/etc") / "transformer-models.md").exists()


# --------------------------------------------------------------------------- #
# Idempotency + drift on raw capture.
# --------------------------------------------------------------------------- #


def test_capture_raw_creates_then_skips_unchanged(harness: IngestHarness) -> None:
    """Re-capturing an identical body recomputes sha256 and skips the rewrite."""
    cls = Classification(
        page_type="note", slug="dup-source", title="Dup", concepts=["dup-source"]
    )
    doc = ExtractedDoc(source_url="https://e.com/x", title="Dup", markdown="same body")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    first = ingestor.capture_raw(Capture(url="https://e.com/x"), cls)
    assert first.disposition == "created"
    rel = "raw/articles/dup-source.md"
    mtime_before = (harness.work / rel).stat().st_mtime_ns

    second = ingestor.capture_raw(Capture(url="https://e.com/x"), cls)
    assert second.disposition == "skipped_unchanged"
    assert second.raw_path == rel
    # The existing raw file was not rewritten.
    assert (harness.work / rel).stat().st_mtime_ns == mtime_before


def test_capture_raw_skips_unchanged_body_ending_in_newline(
    harness: IngestHarness,
) -> None:
    """Re-capturing an identical newline-terminated body still skips (idempotent).

    Regression: the skip-unchanged compare must use the same parse-stable digest the
    writer stamps (``Vault.stored_body_sha256``), not ``body_sha256`` of the raw body,
    so a body with a trailing newline (the normal extractor case) is not re-reported as
    drift on an unchanged page.
    """
    cls = Classification(
        page_type="note", slug="nl-source", title="NL", concepts=["nl-source"]
    )
    doc = ExtractedDoc(
        source_url="https://e.com/nl", title="NL", markdown="real article body\n"
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    first = ingestor.capture_raw(Capture(url="https://e.com/nl"), cls)
    assert first.disposition == "created"
    rel = "raw/articles/nl-source.md"
    mtime_before = (harness.work / rel).stat().st_mtime_ns

    second = ingestor.capture_raw(Capture(url="https://e.com/nl"), cls)
    assert second.disposition == "skipped_unchanged"
    assert (harness.work / rel).stat().st_mtime_ns == mtime_before


def test_capture_raw_detects_drift(harness: IngestHarness) -> None:
    """A changed body for the same slug is flagged as drift and rewritten."""
    cls = Classification(page_type="note", slug="drift-source", title="Drift")
    first_doc = ExtractedDoc(source_url="https://e.com/d", title="D", markdown="v1")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(doc=first_doc),
        hindsight=FakeHindsight(),
    )
    first = ingestor.capture_raw(Capture(url="https://e.com/d"), cls)
    assert first.disposition == "created"

    second_doc = ExtractedDoc(source_url="https://e.com/d", title="D", markdown="v2")
    ingestor2 = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(doc=second_doc),
        hindsight=FakeHindsight(),
    )
    second = ingestor2.capture_raw(Capture(url="https://e.com/d"), cls)
    assert second.disposition == "updated_drift"
    body = (harness.work / "raw/articles/drift-source.md").read_text(encoding="utf-8")
    assert "v2" in body


# --------------------------------------------------------------------------- #
# Image capture: bytes -> save_asset, embed, never base64.
# --------------------------------------------------------------------------- #


def test_image_capture_saves_asset_and_embeds(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """An image URL is fetched to a tmp file, saved under raw/assets, and embedded."""
    binary = _make_binary(tmp_path, ext_hint="png")
    classify = _classify_json(
        page_type="memory", slug="beach-day", title="Beach Day", concepts=[]
    )
    # No model embeds -> the harness derives the ![[asset]] embed itself.
    plan = _file_plan_json(
        folder="memories",
        slug="beach-day",
        page_type="memory",
        title="Beach Day",
        body="A sunny afternoon.",
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(binary=binary),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(
        Capture(url="https://example.com/pic", filename="pic.png", source="slack")
    )

    assert report.asset_paths == ["raw/assets/beach-day.png"]
    asset = harness.work / "raw/assets/beach-day.png"
    assert asset.is_file()
    # The saved bytes are the raw image bytes, NOT base64.
    raw_bytes = asset.read_bytes()
    assert raw_bytes.startswith(b"\x89PNG")
    page_text = (harness.work / "memories/beach-day.md").read_text(encoding="utf-8")
    assert "![[beach-day.png]]" in page_text
    # No base64 blob anywhere in the written page.
    assert "base64" not in page_text.lower()


def test_image_reingest_same_bytes_is_skipped_not_overwrite(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """A second ingest of the same image URL/slug is idempotent (no VaultError).

    SPEC section 6 step 2: 'Skip if sha256 exists'. The first ingest commits the asset;
    the second must report ``skipped_unchanged`` for the asset rather than letting
    ``save_asset`` raise 'refusing to overwrite' and crash the pipeline.
    """
    classify = _classify_json(
        page_type="memory", slug="beach-day", title="Beach Day", concepts=[]
    )
    plan = _file_plan_json(
        folder="memories", slug="beach-day", page_type="memory", title="Beach Day"
    )

    first = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(binary=_make_binary(tmp_path, ext_hint="png")),
        hindsight=FakeHindsight(),
    )
    report1 = first.ingest(
        Capture(url="https://example.com/pic", filename="pic.png", source="slack")
    )
    assert report1.asset_paths == ["raw/assets/beach-day.png"]
    asset = harness.work / "raw/assets/beach-day.png"
    mtime_before = asset.stat().st_mtime_ns

    # Second ingest with byte-identical content: must NOT raise and must not rewrite.
    second_src = tmp_path / "again.png"
    second_src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes")
    second = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(
            binary=FetchedBinary(
                source_url="https://example.com/pic",
                tmp_path=second_src,
                content_type="image/png",
                suggested_ext="png",
            )
        ),
        hindsight=FakeHindsight(),
    )
    report2 = second.ingest(
        Capture(url="https://example.com/pic", filename="pic.png", source="slack")
    )
    assert report2.asset_paths == ["raw/assets/beach-day.png"]
    # The asset bytes on disk were not rewritten.
    assert asset.stat().st_mtime_ns == mtime_before
    # The fetched tmp file was consumed (skip path cleans it up; no leak).
    assert not second_src.exists()


def test_image_reingest_changed_bytes_is_drift_error(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """A different image at an existing asset slug is surfaced as drift, not
    overwrite."""
    classify = _classify_json(
        page_type="memory", slug="beach-day", title="Beach Day", concepts=[]
    )
    plan = _file_plan_json(
        folder="memories", slug="beach-day", page_type="memory", title="Beach Day"
    )
    first = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(binary=_make_binary(tmp_path, ext_hint="png")),
        hindsight=FakeHindsight(),
    )
    first.ingest(
        Capture(url="https://example.com/pic", filename="pic.png", source="slack")
    )
    original = (harness.work / "raw/assets/beach-day.png").read_bytes()

    drift_src = tmp_path / "different.png"
    drift_src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"TOTALLY-different-bytes")
    drifting = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(
            binary=FetchedBinary(
                source_url="https://example.com/pic",
                tmp_path=drift_src,
                content_type="image/png",
                suggested_ext="png",
            )
        ),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="drift"):
        drifting.ingest(
            Capture(url="https://example.com/pic", filename="pic.png", source="slack")
        )
    # The existing asset was NOT overwritten, and the drifting tmp file was cleaned up.
    assert (harness.work / "raw/assets/beach-day.png").read_bytes() == original
    assert not drift_src.exists()


def test_asset_capture_no_tmp_leak_on_drift(
    harness: IngestHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed second image ingest leaves no thoth-/tmp staged file behind.

    Points the staging temp dir at an isolated directory and asserts it is empty after
    the drift error, proving the staged copy is unlinked on the error path.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(staging))

    classify = _classify_json(
        page_type="memory", slug="scan", title="Scan", concepts=[]
    )
    plan = _file_plan_json(
        folder="memories", slug="scan", page_type="memory", title="Scan"
    )
    src1 = tmp_path / "first.jpg"
    src1.write_bytes(b"\xff\xd8\xff" + b"jpeg-one")
    first = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    first.ingest(Capture(path=src1, filename="scan.jpg"))
    # The first ingest's staged copy was moved into the vault, not left behind.
    assert list(staging.iterdir()) == []

    src2 = tmp_path / "second.jpg"
    src2.write_bytes(b"\xff\xd8\xff" + b"jpeg-DIFFERENT")
    second = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="drift"):
        second.ingest(Capture(path=src2, filename="scan.jpg"))
    # No staged temp file leaked on the error path.
    assert list(staging.iterdir()) == []
    # The caller's own source is still intact (we stage a copy, never consume it).
    assert src2.is_file()


def test_pdf_capture_writes_paper_page_and_keeps_binary(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """A PDF lands a raw/papers/<slug>.md text page AND keeps the binary (SPEC 6.2)."""
    pdf_src = tmp_path / "paper-dl.pdf"
    pdf_src.write_bytes(b"%PDF-1.7\n" + b"binary-pdf-bytes")
    binary = FetchedBinary(
        source_url="https://example.com/attention.pdf",
        tmp_path=pdf_src,
        content_type="application/pdf",
        suggested_ext="pdf",
    )
    classify = _classify_json(
        page_type="note",
        slug="attention-paper",
        title="Attention Is All You Need",
        concepts=["attention-paper"],
    )
    plan = _file_plan_json(
        folder="notes",
        slug="attention-paper",
        page_type="note",
        title="Attention Is All You Need",
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(binary=binary),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(
        Capture(url="https://example.com/attention.pdf", filename="attention.pdf")
    )

    # The binary is kept under raw/assets (papers tree keeps the .pdf alongside).
    assert "raw/assets/attention-paper.pdf" in report.asset_paths
    kept = harness.work / "raw/assets/attention-paper.pdf"
    assert kept.read_bytes().startswith(b"%PDF")
    # A searchable raw/papers page exists with provenance + a pointer to the binary.
    assert report.raw_paths == ["raw/papers/attention-paper.md"]
    paper = harness.vault.read_page("raw/papers/attention-paper.md")
    assert paper.frontmatter["source_url"] == "https://example.com/attention.pdf"
    assert "raw/assets/attention-paper.pdf" in paper.body
    # No base64 anywhere.
    assert "base64" not in paper.body.lower()


def _pdf_binary(
    tmp_path: Path, *, data: bytes = b"%PDF-1.7\nsame-pdf-bytes"
) -> FetchedBinary:
    """Stage a fresh PDF tmp file (save_asset consumes it on a successful move)."""
    src = tmp_path / "pdf-staged.pdf"
    src.write_bytes(data)
    return FetchedBinary(
        source_url="https://example.com/a.pdf",
        tmp_path=src,
        content_type="application/pdf",
        suggested_ext="pdf",
    )


def test_pdf_reingest_same_is_skipped(harness: IngestHarness, tmp_path: Path) -> None:
    """Re-ingesting the same PDF skips both the binary and the paper page
    (idempotent)."""
    cls = Classification(
        page_type="note",
        slug="attention-paper",
        title="Attention",
        concepts=["attention-paper"],
    )
    capture = Capture(url="https://example.com/a.pdf", filename="a.pdf")

    dir_a = tmp_path / "a"
    dir_a.mkdir()
    first = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(binary=_pdf_binary(dir_a)),
        hindsight=FakeHindsight(),
    )
    r1 = first.capture_raw(capture, cls)
    assert r1.disposition == "created"
    assert r1.raw_path == "raw/papers/attention-paper.md"
    assert r1.asset_paths == ["raw/assets/attention-paper.pdf"]

    dir_b = tmp_path / "b"
    dir_b.mkdir()
    second = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(binary=_pdf_binary(dir_b)),
        hindsight=FakeHindsight(),
    )
    r2 = second.capture_raw(capture, cls)
    assert r2.disposition == "skipped_unchanged"
    assert r2.asset_paths == ["raw/assets/attention-paper.pdf"]


def test_image_capture_from_local_path(harness: IngestHarness, tmp_path: Path) -> None:
    """A server-resolvable local image path is staged into raw/assets via the vault."""
    src = tmp_path / "screenshot.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"jpeg-bytes")
    classify = _classify_json(page_type="memory", slug="screenshot", title="Shot")
    plan = _file_plan_json(
        folder="memories", slug="screenshot", page_type="memory", title="Shot"
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(),  # no web call for a local path
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(path=src, filename="screenshot.jpg"))

    assert report.asset_paths == ["raw/assets/screenshot.jpg"]
    assert (
        (harness.work / "raw/assets/screenshot.jpg")
        .read_bytes()
        .startswith(b"\xff\xd8\xff")
    )
    # The caller's original tmp file is preserved (we stage a copy before the move).
    assert src.is_file()


# --------------------------------------------------------------------------- #
# Classification validation.
# --------------------------------------------------------------------------- #


def test_classify_rejects_out_of_vocab_type(harness: IngestHarness) -> None:
    """A classification with an unknown type surfaces as IngestError."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(page_type="wibble")),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="valid vault type"):
        ingestor.classify(Capture(text="hello"))


def test_classify_prompt_enumerates_exactly_the_vault_types(
    harness: IngestHarness,
) -> None:
    """The classify prompt's type list is derived from the vault vocabulary (ADR 0005).

    Every content type (and no out-of-vocabulary word) appears in the prompt's
    "type (one of ...)" clause, derived from :data:`thoth.vault.TYPE_ENUMERATION` (the
    four content types; the ``inbox`` machinery type is never a classify target). A type
    added to or removed from the vault contract changes this prompt automatically, so
    the prompt and the enforcement gate cannot diverge.
    """
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json()),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    prompt = ingestor._classify_prompt(Capture(text="hello"))
    enumerated = ", ".join(TYPE_ENUMERATION)
    assert f"type (one of {enumerated})" in prompt
    for page_type in TYPE_ENUMERATION:
        assert page_type in prompt
    assert "wibble" not in prompt


def test_classify_rejects_bad_slug(harness: IngestHarness) -> None:
    """A classification with a malformed slug surfaces as IngestError."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(slug="Not A Slug")),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="slug"):
        ingestor.classify(Capture(text="hello"))


def test_classify_unparseable_output(harness: IngestHarness) -> None:
    """Non-JSON classify output surfaces as IngestError (not a raw parse error)."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("no json here at all"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="classification"):
        ingestor.classify(Capture(text="hello"))


def test_classify_llm_transport_failure(harness: IngestHarness) -> None:
    """An exception from the client is wrapped as IngestError."""
    ingestor = _build_ingestor(
        harness,
        client=_RaisingClient(),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError, match="classify LLM call failed"):
        ingestor.classify(Capture(text="hello"))


def test_classify_parses_an_action(harness: IngestHarness) -> None:
    """An action classification round-trips its type (ADR 0005: no life_admin dict)."""
    classify = _classify_json(
        page_type="action",
        slug="fix-fence",
        title="Fix the fence",
        concepts=[],
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(classify),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = ingestor.classify(Capture(text="remind me to fix the fence"))
    assert cls.page_type == "action"


def test_ingest_action_lands_in_actions_without_index(harness: IngestHarness) -> None:
    """An actionable page lands in actions/ and gets NO index catalog entry."""
    classify = _classify_json(
        page_type="action",
        slug="fix-fence",
        title="Fix the fence",
        concepts=[],
    )
    plan = _file_plan_json(
        folder="actions",
        slug="fix-fence",
        page_type="action",
        title="Fix the fence",
        body="Buy timber and repair the back fence.",
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(classify, plan),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(text="remind me to fix the fence"))

    assert report.page_paths == ["actions/fix-fence.md"]
    assert harness.vault.page_exists("actions/fix-fence.md")
    index_text = (harness.work / "index.md").read_text(encoding="utf-8")
    assert "fix-fence" not in index_text  # life-admin is surfaced by Bases, not index


# --------------------------------------------------------------------------- #
# Commit conflict, retain failure, extractor failure.
# --------------------------------------------------------------------------- #


def test_commit_conflict_surfaces_in_report(harness: IngestHarness) -> None:
    """A VaultConflictError becomes report.conflict with the path; content is filed."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="local body")
    conflicting_git = _ConflictingGitSync(harness.config, env=harness.env)
    llm = LLM(
        harness.config, client=_ScriptedClient(_classify_json(), _file_plan_json())
    )
    ingestor = Ingestor(
        harness.config,
        harness.vault,
        llm,
        FakeExtractor(doc=doc),  # type: ignore[arg-type]
        FakeHindsight(),  # type: ignore[arg-type]
        conflicting_git,
    )

    report = ingestor.ingest(Capture(url="https://e.com/a"))

    assert report.conflict is True
    assert report.committed is False
    assert "notes/transformer-models.md" in report.message
    # The page is already filed locally (fail-loud, content not lost; no --force).
    assert harness.vault.page_exists("notes/transformer-models.md")


# --------------------------------------------------------------------------- #
# liveness markers recorded on a successful capture/push (issue #15).
# --------------------------------------------------------------------------- #


def test_successful_ingest_records_capture_and_push_markers(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """A clean URL ingest records both the capture and push liveness markers."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body text")
    markers = MarkerStore(tmp_path / "marker.db", clock=lambda: 4242.0)
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
        markers=markers,
    )

    report = ingestor.ingest(Capture(url="https://e.com/a"))

    assert report.committed is True
    assert markers.get(MARKER_CAPTURE) == 4242.0
    assert markers.get(MARKER_PUSH) == 4242.0


def test_conflict_records_no_push_marker(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """A push conflict files content locally but records NO push marker (no push ran).

    The capture marker is also absent: on a conflict the ingest path does not advance
    either marker, so the stale "last push" time is exactly the diagnostic the daily
    heartbeat surfaces.
    """
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="local body")
    markers = MarkerStore(tmp_path / "marker.db", clock=lambda: 1.0)
    conflicting_git = _ConflictingGitSync(harness.config, env=harness.env)
    llm = LLM(
        harness.config, client=_ScriptedClient(_classify_json(), _file_plan_json())
    )
    ingestor = Ingestor(
        harness.config,
        harness.vault,
        llm,
        FakeExtractor(doc=doc),  # type: ignore[arg-type]
        FakeHindsight(),  # type: ignore[arg-type]
        conflicting_git,
        markers=markers,
    )

    report = ingestor.ingest(Capture(url="https://e.com/a"))

    assert report.conflict is True
    assert markers.get(MARKER_PUSH) is None
    assert markers.get(MARKER_CAPTURE) is None


def test_marker_write_failure_does_not_break_ingest(
    harness: IngestHarness,
) -> None:
    """A MarkerStore that raises on record does not fail an otherwise-good ingest."""

    class _BoomMarkers:
        def record(self, name: str, *, ts: float | None = None) -> None:
            raise RuntimeError("marker db gone")

    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
        markers=cast(MarkerStore, _BoomMarkers()),
    )
    # The capture still succeeds despite the marker write blowing up (best-effort).
    report = ingestor.ingest(Capture(url="https://e.com/a"))
    assert report.committed is True


def test_no_markers_store_is_a_clean_noop(harness: IngestHarness) -> None:
    """The default (no MarkerStore) records nothing and ingests normally."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )
    report = ingestor.ingest(Capture(url="https://e.com/a"))
    assert report.committed is True


def test_retain_failure_surfaces_after_durable_write(harness: IngestHarness) -> None:
    """A Hindsight retain failure raises but the vault page is already on disk."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(retain_error=HindsightError("retain blew up")),
    )

    with pytest.raises(IngestError, match="hindsight retain failed"):
        ingestor.ingest(Capture(url="https://e.com/a"))

    # The curated page write happened before retain, so it is durable on disk.
    assert harness.vault.page_exists("notes/transformer-models.md")


def test_probe_failure_does_not_abort(harness: IngestHarness) -> None:
    """A probe HindsightError is swallowed: the ingest still completes and commits."""
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(probe_error=HindsightError("probe down")),
    )
    report = ingestor.ingest(Capture(url="https://e.com/a"))
    assert report.committed is True


def test_extractor_fetch_error_aborts_before_commit(harness: IngestHarness) -> None:
    """A FetchError during capture aborts with IngestError and commits nothing."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(web_extract_error=FetchError("502 upstream")),
        hindsight=FakeHindsight(),
    )

    with pytest.raises(IngestError, match="extraction"):
        ingestor.ingest(Capture(url="https://e.com/a"))

    # No curated page written, nothing pushed to origin beyond the seed.
    assert not harness.vault.page_exists("notes/transformer-models.md")
    assert "notes/transformer-models.md" not in harness.origin_files()


def test_extractor_ssrf_error_aborts(harness: IngestHarness) -> None:
    """An SsrfError (blocked URL) during capture aborts the ingest."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=FakeExtractor(web_extract_error=SsrfError("blocked private IP")),
        hindsight=FakeHindsight(),
    )
    with pytest.raises(IngestError):
        ingestor.ingest(Capture(url="https://169.254.169.254/latest/meta-data"))


# --------------------------------------------------------------------------- #
# Issue #14: capture durability decoupled from the classify LLM call.
# --------------------------------------------------------------------------- #


def _inbox_holds(harness: IngestHarness) -> list[str]:
    """Return the inbox/ holding pages currently on disk (vault-relative)."""
    inbox = harness.work / "inbox"
    return [f"inbox/{p.name}" for p in sorted(inbox.glob("*.md"))]


def test_ingest_defers_when_daily_budget_exhausted(harness: IngestHarness) -> None:
    """A budget-exhausted classify defers the capture, never loses it (issue #16).

    The daily LLM guard raises :class:`~thoth.budget.BudgetExceededError` from
    ``LLM.complete``; the classify pass treats it like any model-availability failure,
    so the raw is held durably in ``inbox/`` and the report is *deferred* -- the same
    capture-never-lost path #14 built, now reached by the cost cap not an outage.
    """
    from thoth.budget import KIND_ANTHROPIC, BudgetGuard, BudgetStore

    budget = BudgetGuard(store=BudgetStore(harness.work / "budget.db"), limit=1)
    budget.charge(KIND_ANTHROPIC)  # pre-exhaust today's single-call budget

    class _NeverCalledClient:
        """A client whose ``messages.create`` must never run (the guard trips first)."""

        class _Messages:
            def create(self, **_kwargs: Any) -> Any:
                raise AssertionError("the budget guard should block before the client")

        messages = _Messages()

    ingestor = _build_ingestor(
        harness,
        client=_NeverCalledClient(),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
        guard=budget,
    )

    report = ingestor.ingest(Capture(text="a thought worth keeping"))

    assert report.deferred is True
    assert report.page_paths == []
    holds = _inbox_holds(harness)
    assert len(holds) == 1  # the capture is durable on disk, awaiting re-curation
    assert report.committed is True


def test_ingest_persists_raw_then_defers_when_classify_llm_fails(
    harness: IngestHarness,
) -> None:
    """LLM down at classify: the inbound text is persisted to inbox + curation deferred.

    Acceptance for #14: with the LLM forced to fail (the injected client raises), ingest
    still persists the raw inbound item durably (an inbox/ holding page) and reports a
    *deferred* curation rather than losing the capture or raising.
    """
    ingestor = _build_ingestor(
        harness,
        client=_RaisingClient(),  # the injected LLM seam, forced to fail
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(text="a thought worth keeping"))

    # The capture was NOT lost: a durable inbox holding page exists and is reported.
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    assert report.deferred is True
    assert report.page_paths == []
    assert report.raw_paths == holds  # the held raw page is surfaced
    assert "deferred" in report.message.lower()
    # The held page carries the inbound body and is type: inbox for the sweep to find.
    held = harness.vault.read_page(holds[0])
    assert held.frontmatter["type"] == "inbox"
    assert "a thought worth keeping" in held.body
    # It was committed to the local origin (durable beyond the process).
    assert report.committed is True
    assert holds[0] in harness.origin_files()


def test_ingest_persists_raw_then_defers_when_curate_llm_fails(
    harness: IngestHarness,
) -> None:
    """LLM down at curate (classify OK): raw is still persisted + curation deferred.

    The classify call succeeds but the curate call fails; because the raw was persisted
    before any LLM call, the capture is safe and the report is deferred (not an error).
    """

    class _ClassifyOkThenRaise:
        """Client whose first create() returns classify JSON, then always raises."""

        class _Messages:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    return {"content": [{"type": "text", "text": _classify_json()}]}
                raise RuntimeError("curate boom")

        def __init__(self) -> None:
            self.messages = _ClassifyOkThenRaise._Messages()

    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body text")
    ingestor = _build_ingestor(
        harness,
        client=_ClassifyOkThenRaise(),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(url="https://e.com/a"))

    assert report.deferred is True
    assert report.page_paths == []
    # No curated page was written (the validation gate / curate never produced a plan).
    assert not harness.vault.page_exists("notes/transformer-models.md")
    # The durable inbox holding page exists and carries the extracted article body.
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    assert "body text" in harness.vault.read_page(holds[0]).body


def test_ingest_deferred_hold_carries_capture_source(harness: IngestHarness) -> None:
    """A deferred MCP capture is held under its OWN source, not a hardcoded 'slack'.

    Regression for the provenance bug: ``_write_inbox_holding`` must stamp the held
    inbox page with the capture's real ``source`` (here ``mcp``), since these are the
    items a later reindex/sweep re-curates and must attribute correctly.
    """
    ingestor = _build_ingestor(
        harness,
        client=_RaisingClient(),  # force the deferred path
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(text="an mcp-origin thought", source="mcp"))

    assert report.deferred is True
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    # The held page round-trips the true origin, not the old hardcoded "slack".
    assert harness.vault.read_page(holds[0]).frontmatter["source"] == "mcp"


def test_ingest_deferred_hold_durable_when_push_conflicts(
    harness: IngestHarness,
) -> None:
    """#14 edge: LLM down AND deferred push refused -> hold stays durable locally.

    With the LLM forced to fail (deferred) and the commit raising
    :class:`VaultConflictError`, the report is both ``deferred`` and ``conflict`` and
    the inbox holding page is still on disk (the capture is never lost; no ``--force``).
    """
    conflicting_git = _ConflictingGitSync(harness.config, env=harness.env)
    llm = LLM(harness.config, client=_RaisingClient())
    ingestor = Ingestor(
        harness.config,
        harness.vault,
        llm,
        FakeExtractor(),  # type: ignore[arg-type]  # structural fake
        FakeHindsight(),  # type: ignore[arg-type]  # structural fake
        conflicting_git,
    )

    report = ingestor.ingest(Capture(text="durable through a conflict"))

    assert report.deferred is True
    assert report.conflict is True
    assert report.committed is False
    # The durable hold survived the refused push (still on disk locally).
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    assert harness.vault.page_exists(holds[0])


def test_ingest_deferred_hold_durable_when_push_fails(harness: IngestHarness) -> None:
    """#14 edge: LLM down AND the deferred push errors -> hold stays durable locally.

    With the LLM forced to fail (deferred) and the commit raising a plain
    :class:`GitSyncError` (a failed push, not a rebase conflict), the report is
    ``deferred`` but NOT committed, and the inbox holding page is still present locally.
    """
    failing_git = _FailingGitSync(harness.config, env=harness.env)
    llm = LLM(harness.config, client=_RaisingClient())
    ingestor = Ingestor(
        harness.config,
        harness.vault,
        llm,
        FakeExtractor(),  # type: ignore[arg-type]  # structural fake
        FakeHindsight(),  # type: ignore[arg-type]  # structural fake
        failing_git,
    )

    report = ingestor.ingest(Capture(text="durable through a push failure"))

    assert report.deferred is True
    # The push was NOT recorded as committed (it raised), but nothing was lost.
    assert report.committed is False
    assert report.conflict is False
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    assert harness.vault.page_exists(holds[0])


def test_ingest_happy_path_removes_inbox_holding_page(harness: IngestHarness) -> None:
    """On successful curation the (now superseded) inbox holding page is removed.

    The pre-classify durability write is a holding copy; once the curated + raw pages
    are written it is redundant, so the happy path cleans it up (no stray inbox item for
    the reindex sweep to re-process).
    """
    doc = ExtractedDoc(
        source_url="https://example.com/x",
        title="Transformer Models",
        markdown="Transformers use attention to weigh tokens.",
    )
    extractor = FakeExtractor(doc=doc)
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), _file_plan_json()),
        extractor=extractor,
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(url="https://example.com/x"))

    # The curated + raw pages landed exactly as before this change.
    assert report.deferred is False
    assert report.page_paths == ["notes/transformer-models.md"]
    assert report.raw_paths == ["raw/articles/transformer-models.md"]
    # No inbox holding page is left behind on the happy path.
    assert _inbox_holds(harness) == []
    # The source was fetched exactly once: persist_inbound's extraction is reused by
    # capture_raw (prefetched), not re-fetched.
    assert extractor.web_extract_calls == ["https://example.com/x"]


def test_ingest_defer_is_idempotent_on_body_sha(harness: IngestHarness) -> None:
    """Re-deferring the identical inbound text reuses the same inbox hold (idempotent).

    The holding slug is derived from the body SHA-256, so a second deferred ingest of
    the same text lands on the same path (no duplicate hold accumulates).
    """
    first = _build_ingestor(
        harness,
        client=_RaisingClient(),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    first.ingest(Capture(text="exact same body"))
    holds_after_first = _inbox_holds(harness)
    assert len(holds_after_first) == 1

    second = _build_ingestor(
        harness,
        client=_RaisingClient(),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    second.ingest(Capture(text="exact same body"))
    # Still exactly one hold at the same path (SHA-keyed, idempotent).
    assert _inbox_holds(harness) == holds_after_first


def test_ingest_validation_failure_still_raises_and_keeps_raw(
    harness: IngestHarness,
) -> None:
    """A schema-invalid plan still raises IngestError (gate kept); raw stays in inbox.

    A *validation* failure is distinct from an LLM-availability failure: it still aborts
    with IngestError (no curated/navigation write on an invalid plan), but the inbound
    item was already persisted to inbox before classify, so the capture is not lost.
    """
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body text")
    bad_plan = _file_plan_json(folder="actions", page_type="note")  # illegal pair
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), bad_plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    with pytest.raises(IngestError):
        ingestor.ingest(Capture(url="https://e.com/a"))

    # The validation gate held: no curated page written.
    assert not harness.vault.page_exists("notes/transformer-models.md")
    # But the raw inbound item is safe in the inbox holding area (capture not lost).
    assert len(_inbox_holds(harness)) == 1


def test_ingest_defer_when_classify_fails_for_image_holds_provenance(
    harness: IngestHarness, tmp_path: Path
) -> None:
    """An image capture with the LLM down holds a provenance stub (never base64)."""
    src = tmp_path / "scan.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"jpeg-bytes")
    ingestor = _build_ingestor(
        harness,
        client=_RaisingClient(),
        extractor=FakeExtractor(),  # no web call for a local path
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(path=src, filename="scan.jpg"))

    assert report.deferred is True
    holds = _inbox_holds(harness)
    assert len(holds) == 1
    body = harness.vault.read_page(holds[0]).body
    # The held stub records the source for a later sweep and carries no base64 blob.
    assert "scan.jpg" in body
    assert "base64" not in body.lower()


# --------------------------------------------------------------------------- #
# fetch_candidates / search_vault read-only behaviour.
# --------------------------------------------------------------------------- #


def test_search_vault_matches_filename_and_body(harness: IngestHarness) -> None:
    """search_vault finds a curated page by filename and by body, de-duplicated."""
    harness.vault.write_page(
        "entities",
        "program-motion-controller",
        {
            "title": "Program Motion Controller",
            "type": "entity",
            "source": "manual",
            "tags": ["controls"],
        },
        "The central coordinator for axes.",
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    by_name = ingestor.search_vault("motion")
    assert "entities/program-motion-controller.md" in by_name
    by_body = ingestor.search_vault("coordinator")
    assert "entities/program-motion-controller.md" in by_body


def test_search_vault_empty_query_returns_empty(harness: IngestHarness) -> None:
    """A blank query returns no candidates (no scan)."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    assert ingestor.search_vault("   ") == []


def test_fetch_candidates_dedupes_terms(harness: IngestHarness) -> None:
    """fetch_candidates merges entity/concept hits without duplicates."""
    harness.vault.write_page(
        "notes",
        "attention",
        {
            "title": "Attention",
            "type": "note",
            "source": "manual",
            "tags": ["ai-ml"],
        },
        "Attention weighs tokens.",
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(
        page_type="note",
        slug="x",
        title="attention",
        entities=["attention"],
        concepts=["attention"],
    )
    candidates = ingestor.fetch_candidates(cls)
    assert candidates.count("notes/attention.md") == 1


# --------------------------------------------------------------------------- #
# Curate validation rejection without going through full ingest.
# --------------------------------------------------------------------------- #


def test_curate_rejects_too_few_wikilinks(harness: IngestHarness) -> None:
    """A page with <2 wikilinks fails validate_file_plan -> IngestError, no write."""
    plan = _file_plan_json(wikilinks=["[[only-one]]"])
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(plan),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(page_type="note", slug="transformer-models", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")
    with pytest.raises(IngestError, match="file plan rejected"):
        ingestor.curate(Capture(text="x"), cls, raw, [])
    assert not harness.vault.page_exists("notes/transformer-models.md")


def test_curate_unparseable_plan(harness: IngestHarness) -> None:
    """Non-JSON curate output surfaces as IngestError naming the file plan."""
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("sorry, no plan"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(page_type="note", slug="x", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")
    with pytest.raises(IngestError, match="file plan"):
        ingestor.curate(Capture(text="x"), cls, raw, [])


def test_curate_prompt_embeds_file_plan_contract(harness: IngestHarness) -> None:
    """The curate prompt spells out the file-plan contract (folders/fields/sources).

    Without this the model only saw "return a file plan" and guessed the envelope, so
    validate_file_plan rejected every capture and the vault stayed empty.
    """
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient("{}"),
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(page_type="note", slug="x", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")
    prompt = ingestor._curate_prompt(Capture(text="hello"), cls, raw, [])
    for token in (
        "notes",
        "entities",
        "actions",
        "wikilinks",
        "created",
        "updated",
        "slack",
    ):
        assert token in prompt, f"curate prompt missing {token!r}"


def test_curate_retries_then_succeeds_on_corrective_plan(
    harness: IngestHarness,
) -> None:
    """A first invalid plan is recovered: the validation errors are fed back and the
    corrected plan is written (the exact failure that left the live vault empty)."""
    bad_plan = _file_plan_json(folder="actions", page_type="note")  # folder/type clash
    good_plan = _file_plan_json()
    client = _ScriptedClient(bad_plan, good_plan)
    ingestor = _build_ingestor(
        harness,
        client=client,
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(page_type="note", slug="transformer-models", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")

    plan = ingestor.curate(Capture(text="x"), cls, raw, [])

    assert plan["_written"] == ["notes/transformer-models.md"]
    assert harness.vault.page_exists("notes/transformer-models.md")
    # Two curate attempts were made and the retry fed the validation errors back.
    assert len(client.messages.calls) == 2
    retry_msgs = client.messages.calls[1]["messages"]
    retry_text = " ".join(
        m["content"] for m in retry_msgs if isinstance(m["content"], str)
    )
    assert "REJECTED" in retry_text


def test_curate_reraises_after_exhausting_corrective_retry(
    harness: IngestHarness,
) -> None:
    """A persistently invalid plan still aborts after one retry, writing nothing."""
    bad_plan = _file_plan_json(folder="actions", page_type="note")
    client = _ScriptedClient(bad_plan)  # the last response repeats -> both invalid
    ingestor = _build_ingestor(
        harness,
        client=client,
        extractor=FakeExtractor(),
        hindsight=FakeHindsight(),
    )
    cls = Classification(page_type="note", slug="transformer-models", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")

    with pytest.raises(IngestError, match="file plan"):
        ingestor.curate(Capture(text="x"), cls, raw, [])
    assert len(client.messages.calls) == _CURATE_ATTEMPTS  # one corrective retry tried
    assert not harness.vault.page_exists("actions/transformer-models.md")
    assert not harness.vault.page_exists("notes/transformer-models.md")


def test_apply_navigation_skips_unknown_index_section(harness: IngestHarness) -> None:
    """A model-supplied index section that isn't a catalog section is skipped, the
    already-written page is kept, and a correct default catalog entry is derived.

    Regression: navigation used to abort the whole capture (IngestError) on an unknown
    index section even though the curated page was already on disk.
    """
    doc = ExtractedDoc(source_url="https://e.com/a", title="T", markdown="body")
    plan = _file_plan_json(
        index_entries=[
            {
                "section": "Bogus / Scratch",
                "wikilink": "transformer-models",
                "summary": "s",
            }
        ],
    )
    ingestor = _build_ingestor(
        harness,
        client=_ScriptedClient(_classify_json(), plan),
        extractor=FakeExtractor(doc=doc),
        hindsight=FakeHindsight(),
    )

    report = ingestor.ingest(Capture(url="https://e.com/a"))

    assert report.page_paths == ["notes/transformer-models.md"]
    assert harness.vault.page_exists("notes/transformer-models.md")
    index_text = (harness.work / "index.md").read_text(encoding="utf-8")
    # The derived default catalog entry landed; the bogus section never did.
    assert "[[transformer-models]]" in index_text
    assert "Bogus" not in index_text


def test_curate_passes_schema_md_as_system_extra(harness: IngestHarness) -> None:
    """The injected SCHEMA.md text is forwarded to the curate call as system_extra."""
    client = _ScriptedClient(_file_plan_json())
    llm = LLM(harness.config, client=client)
    ingestor = Ingestor(
        harness.config,
        harness.vault,
        llm,
        FakeExtractor(),  # type: ignore[arg-type]
        FakeHindsight(),  # type: ignore[arg-type]
        harness.git,
        schema_md="# Vault Schema\nrules here",
    )
    cls = Classification(page_type="note", slug="transformer-models", title="T")
    raw = RawCaptureResult(raw_path=None, disposition="none")
    ingestor.curate(Capture(text="x"), cls, raw, [])
    # The single create call carried the schema text in its system blocks.
    create_kwargs = client.messages.calls[-1]
    system_texts = [block["text"] for block in create_kwargs["system"]]
    assert any("rules here" in text for text in system_texts)
