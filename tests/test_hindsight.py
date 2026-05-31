"""Tests for :mod:`thoth.hindsight`.

Every test isolates the external boundary: no real ``hindsight`` binary, no Postgres,
no Gemini. A :class:`RecordingRunner` fake stands in for the
:class:`~thoth.hindsight.SubprocessRunner` seam, recording the argv it is handed and
returning a canned :class:`subprocess.CompletedProcess` (or raising), so the tests
assert on the exact command line the wrapper builds, on how it classifies the result,
and on the bounded retry around the checked calls -- all without spawning a process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from thoth.config import Config, load_config
from thoth.hindsight import (
    DEFAULT_BANK,
    DEFAULT_BINARY,
    FORGET_SUBCOMMAND,
    RECALL_SUBCOMMAND,
    RETAIN_SUBCOMMAND,
    SOURCE_SENTINEL,
    Hindsight,
    HindsightError,
    HindsightTransientError,
    RecallHit,
    base_args,
    default_runner,
    parse_recall,
    retain_text,
)


@dataclass
class RecordingRunner:
    """A fake :class:`~thoth.hindsight.SubprocessRunner` for tests.

    It records every ``argv`` (and ``timeout``) it is called with and returns a
    canned :class:`subprocess.CompletedProcess`, so a test can assert on the exact
    command line built and on how the wrapper classifies the canned result.

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

    @property
    def last(self) -> list[str]:
        """The most recently recorded argv."""
        return self.calls[-1]


@dataclass
class ScriptedRunner:
    """A runner that replays a scripted sequence of outcomes, recording every call.

    Each entry is either an ``int`` exit code (returned as a completed process with that
    ``returncode``) or an :class:`Exception` instance (raised, to simulate a spawn
    error). The last entry is reused once the script is exhausted, so a single
    success-or-failure can be repeated indefinitely.

    Attributes:
        script: The outcomes to replay in order (exit code or exception to raise).
        stdout: The canned stdout for the completed-process outcomes.
        calls: Every ``argv`` list seen, in call order.
    """

    script: list[int | Exception]
    stdout: str = ""
    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and replay the next scripted outcome (or raise it)."""
        self.calls.append(list(argv))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        outcome = self.script[index]
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=outcome,
            stdout=self.stdout,
            stderr="boom",
        )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A minimal :class:`Config` (only the required vault path is needed here)."""
    return load_config({"PKM_VAULT": str(tmp_path)})


def _make(
    config: Config,
    runner: RecordingRunner | ScriptedRunner,
    *,
    timeout: float = 120.0,
    retries: int = 3,
) -> Hindsight:
    """Build a :class:`Hindsight` on a recording/scripted runner (zero backoff)."""
    return Hindsight(
        config,
        runner=runner,
        timeout=timeout,
        retries=retries,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )


def _json_recall(*records: dict[str, object]) -> str:
    """Render a ``-o json`` recall payload (a bare list of hit records)."""
    return json.dumps(list(records))


# --------------------------------------------------------------------------- #
# Official CLI surface: binary `hindsight`, profile via -p, bank positional.
# --------------------------------------------------------------------------- #


def test_default_binary_and_bank_match_official_surface() -> None:
    """The binary is `hindsight` and the bank is `thoth` (renamed off hermes)."""
    assert DEFAULT_BINARY == "hindsight"
    assert DEFAULT_BANK == "thoth"


def test_base_args_is_binary_only_without_a_profile() -> None:
    """Without a profile, base_args is just the binary -- bank is NOT in the prefix."""
    assert base_args() == ("hindsight",)
    # `-p` is the profile, never the bank.
    assert "-p" not in base_args()


def test_base_args_includes_profile_when_supplied() -> None:
    """A profile is emitted as `-p <profile>` (the profile, not the bank)."""
    assert base_args(profile="work") == ("hindsight", "-p", "work")


def test_binary_and_profile_are_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THOTH_HINDSIGHT_BINARY / _PROFILE re-point the prefix (VPS reconcile seam)."""
    monkeypatch.setenv("THOTH_HINDSIGHT_BINARY", "hindsight-embed")
    monkeypatch.setenv("THOTH_HINDSIGHT_PROFILE", "prod")
    assert base_args() == ("hindsight-embed", "-p", "prod")


def test_bank_is_env_overridable(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THOTH_HINDSIGHT_BANK overrides the positional bank id."""
    monkeypatch.setenv("THOTH_HINDSIGHT_BANK", "otherbank")
    hs = Hindsight(config)
    assert hs.bank == "otherbank"


def test_base_args_and_bank_overrides_are_honoured(config: Config) -> None:
    """Per-instance base_args + bank overrides re-point the CLI surface (VPS seam)."""
    runner = RecordingRunner(stdout="[]")
    hs = Hindsight(
        config, base_args=("hindsight-embed", "-p", "prof"), bank="b1", runner=runner
    )
    assert hs.base_args == ("hindsight-embed", "-p", "prof")
    assert hs.bank == "b1"
    hs.recall("anything")
    # The prefix is the override, then the verb tokens, then the positional bank id.
    assert runner.last[:3] == ["hindsight-embed", "-p", "prof"]
    assert runner.last[3 : 3 + len(RECALL_SUBCOMMAND)] == list(RECALL_SUBCOMMAND)
    assert runner.last[3 + len(RECALL_SUBCOMMAND)] == "b1"


def test_no_hermes_bank_reference_remains_in_module() -> None:
    """No `hermes` *bank* reference survives in the module source (#9).

    The only tolerated mention is the VPS-reconciliation note that the box currently has
    the binary installed under the hermes *user* (a deployment fact, not the bank). A
    bank-shaped reference -- a quoted ``"hermes"`` / ``'hermes'`` literal, ``bank
    hermes``, or ``-p hermes`` (the old, wrong "profile == bank" spelling) -- is gone.
    """
    import thoth.hindsight as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for forbidden in (
        '"hermes"',
        "'hermes'",
        "bank hermes",
        "-p hermes",
        "bank_id: hermes",
    ):
        assert forbidden not in source, f"stale hermes bank reference: {forbidden!r}"


# --------------------------------------------------------------------------- #
# retain_text / parse_recall (pure helpers).
# --------------------------------------------------------------------------- #


def test_retain_text_prefixes_exactly_one_source_line() -> None:
    """retain_text emits one 'SOURCE: <path>' line, a blank line, then the facts."""
    blob = retain_text("entities/foo.md", "Foo is a thing.\nMore detail.")
    lines = blob.split("\n")
    assert lines[0] == f"{SOURCE_SENTINEL} entities/foo.md"
    assert lines[1] == ""
    assert lines[2] == "Foo is a thing."
    # Exactly one SOURCE: line in the whole blob.
    assert blob.count(SOURCE_SENTINEL) == 1


def test_parse_recall_prefers_the_rel_tag_over_the_sentinel() -> None:
    """The vault path is recovered from each hit's `rel` tag (primary channel, #7)."""
    # The hit text is post-extraction atomic facts with NO SOURCE: line; the path
    # survives only because it is tagged.
    stdout = _json_recall(
        {"text": "Foo is a coordinator.", "tags": ["entity", "entities/foo.md"]},
        {"text": "Bar relates to CAP.", "tags": ["concept", "concepts/bar.md"]},
    )
    hits = parse_recall(stdout)
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]
    assert all(isinstance(h, RecallHit) for h in hits)


def test_parse_recall_accepts_rel_prefixed_tags_and_dict_tags() -> None:
    """Both `rel:<path>` string tags and a {'rel': <path>} mapping are honoured."""
    prefixed = _json_recall({"text": "fact", "tags": ["entity", "rel:entities/foo.md"]})
    assert [h.path for h in parse_recall(prefixed)] == ["entities/foo.md"]
    mapped = _json_recall({"text": "fact", "tags": {"rel": "concepts/bar.md"}})
    assert [h.path for h in parse_recall(mapped)] == ["concepts/bar.md"]


def test_parse_recall_falls_back_to_sentinel_when_tags_absent() -> None:
    """With no usable tag, the SOURCE: line in the hit text recovers the path (#7)."""
    stdout = _json_recall(
        {"text": "SOURCE: entities/foo.md\n\nFoo is a thing.", "tags": []},
        {"text": "no tag, no sentinel -> dropped"},
    )
    hits = parse_recall(stdout)
    assert [h.path for h in hits] == ["entities/foo.md"]


def test_parse_recall_dedupes_across_tag_and_sentinel_order_preserving() -> None:
    """Duplicate paths (from either channel) collapse, first-seen order preserved."""
    stdout = _json_recall(
        {"text": "a", "tags": ["entity", "entities/foo.md"]},
        {"text": "b", "tags": ["concept", "concepts/bar.md"]},
        {"text": "SOURCE: entities/foo.md\n\nrepeat"},  # duplicate via sentinel
    )
    assert [h.path for h in parse_recall(stdout)] == [
        "entities/foo.md",
        "concepts/bar.md",
    ]


def test_parse_recall_reads_wrapped_envelopes() -> None:
    """A dict envelope wrapping the hit list (results/hits/...) is unwrapped."""
    for key in ("results", "hits", "memories", "observations", "data"):
        payload = json.dumps(
            {key: [{"text": "x", "tags": ["entity", "entities/foo.md"]}]}
        )
        assert [h.path for h in parse_recall(payload)] == ["entities/foo.md"]


def test_parse_recall_empty_json_returns_empty_list() -> None:
    """An empty JSON list (or an envelope with none) yields [] without raising."""
    assert parse_recall("[]") == []
    assert parse_recall('{"results": []}') == []


def test_parse_recall_non_json_falls_back_to_sentinel_scan() -> None:
    """Non-JSON stdout (a CLI that ignored -o json) still yields SOURCE: provenance."""
    stdout = (
        "result 1 score=0.91\n"
        "SOURCE: entities/foo.md\n"
        "some fact text\n"
        "SOURCE: concepts/bar.md\n"
        "SOURCE: entities/foo.md\n"  # duplicate
    )
    hits = parse_recall(stdout)
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]
    assert all(h.text.startswith(SOURCE_SENTINEL) for h in hits)


def test_parse_recall_non_json_no_markers_returns_empty() -> None:
    """Non-JSON stdout with no SOURCE: lines yields []."""
    assert parse_recall("no markers here\njust prose\n") == []
    # A 'SOURCE:' not at line start is not a marker (anchored at line start).
    assert parse_recall("see SOURCE: entities/foo.md inline\n") == []


# --------------------------------------------------------------------------- #
# retain()
# --------------------------------------------------------------------------- #


def test_retain_builds_official_argv_with_bank_positional_and_tags(
    config: Config,
) -> None:
    """retain builds base_args + retain + <bank> + <text>, then --document-tags."""
    runner = RecordingRunner()
    hs = _make(config, runner)

    hs.retain("entities/foo.md", "Foo facts.", tags=["entity"])

    argv = runner.last
    prefix = list(base_args())
    assert argv[: len(prefix)] == prefix
    verb = argv[len(prefix) : len(prefix) + len(RETAIN_SUBCOMMAND)]
    assert verb == list(RETAIN_SUBCOMMAND)
    # The bank id is the first positional after the verb.
    assert argv[len(prefix) + len(RETAIN_SUBCOMMAND)] == DEFAULT_BANK
    # The text (with the SOURCE: sentinel) is the next positional -- NOT behind --text.
    text_value = argv[len(prefix) + len(RETAIN_SUBCOMMAND) + 1]
    assert text_value.startswith(f"{SOURCE_SENTINEL} entities/foo.md")
    assert "Foo facts." in text_value


def test_retain_adds_the_rel_path_as_a_provenance_tag(config: Config) -> None:
    """The vault path is always added to --document-tags as primary provenance (#7)."""
    runner = RecordingRunner()
    hs = _make(config, runner)

    hs.retain("entities/foo.md", "facts", tags=["entity"])
    tag_value = runner.last[runner.last.index("--document-tags") + 1]
    tags = tag_value.split(",")
    # Both the page type and the rel path are present; rel path is the provenance tag.
    assert tags == ["entity", "entities/foo.md"]


def test_retain_dedupes_rel_path_when_caller_already_passes_it(
    config: Config,
) -> None:
    """If the caller already includes the rel path, it is not duplicated in tags."""
    runner = RecordingRunner()
    hs = _make(config, runner)
    # ingest/reindex pass tags=[page_type, rel]; the rel must appear exactly once.
    hs.retain("entities/foo.md", "facts", tags=["entity", "entities/foo.md"])
    tag_value = runner.last[runner.last.index("--document-tags") + 1]
    assert tag_value == "entity,entities/foo.md"
    assert tag_value.count("entities/foo.md") == 1


def test_retain_drops_empty_tags_but_always_keeps_rel_path(config: Config) -> None:
    """Empty tag strings are filtered; the rel path tag is always present."""
    runner = RecordingRunner()
    hs = _make(config, runner)

    hs.retain("concepts/bar.md", "facts", tags=["", "concept", ""])
    idx = runner.last.index("--document-tags")
    assert runner.last[idx + 1] == "concept,concepts/bar.md"

    # Even with no caller tags, the rel path tag is emitted (provenance must survive).
    hs.retain("concepts/baz.md", "facts")
    assert runner.last[runner.last.index("--document-tags") + 1] == "concepts/baz.md"


def test_retain_raises_on_permanent_exit_without_retry(config: Config) -> None:
    """A permanent (bad-usage exit 2) failure raises and is NOT retried."""
    runner = ScriptedRunner(script=[2])
    hs = _make(config, runner)
    with pytest.raises(HindsightError) as exc_info:
        hs.retain("entities/foo.md", "facts")
    msg = str(exc_info.value)
    assert "retain" in msg
    assert "entities/foo.md" in msg
    # Fail-fast: exactly one spawn, no retry on a permanent error.
    assert len(runner.calls) == 1


def test_retain_passes_configured_timeout_to_runner(config: Config) -> None:
    """The configured timeout reaches the runner unchanged."""
    runner = RecordingRunner()
    hs = _make(config, runner, timeout=7.5)
    hs.retain("entities/foo.md", "facts")
    assert runner.timeouts[-1] == 7.5


# --------------------------------------------------------------------------- #
# recall()
# --------------------------------------------------------------------------- #


def test_recall_builds_argv_with_json_output_and_parses_tag_paths(
    config: Config,
) -> None:
    """recall sends <bank> <query> -o json (no --limit) and maps tag paths into hits."""
    runner = RecordingRunner(
        stdout=_json_recall(
            {"text": "fact", "tags": ["entity", "entities/foo.md"]},
            {"text": "fact2", "tags": ["concept", "concepts/bar.md"]},
        )
    )
    hs = _make(config, runner)

    hits = hs.recall("how does foo work", limit=3)

    argv = runner.last
    prefix = list(base_args())
    assert argv[: len(prefix)] == prefix
    verb = argv[len(prefix) : len(prefix) + len(RECALL_SUBCOMMAND)]
    assert verb == list(RECALL_SUBCOMMAND)
    assert argv[len(prefix) + len(RECALL_SUBCOMMAND)] == DEFAULT_BANK
    assert "how does foo work" in argv
    # Structured output requested, not pretty-stdout scraping.
    assert argv[argv.index("-o") + 1] == "json"
    # VPS-confirmed: hindsight-embed recall has no --limit; the cap is client-side.
    assert "--limit" not in argv
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]


def test_recall_caps_results_client_side_to_limit(config: Config) -> None:
    """With no CLI --limit, recall truncates the parsed hits to ``limit`` itself."""
    runner = RecordingRunner(
        stdout=_json_recall(
            {"text": "a", "tags": ["entity", "entities/a.md"]},
            {"text": "b", "tags": ["concept", "concepts/b.md"]},
            {"text": "c", "tags": ["entity", "entities/c.md"]},
        )
    )
    hs = _make(config, runner)

    hits = hs.recall("everything", limit=2)

    # Three hits parsed, but the client-side cap keeps only the first two (in order).
    assert "--limit" not in runner.last
    assert [h.path for h in hits] == ["entities/a.md", "concepts/b.md"]


def test_recall_empty_json_returns_empty_and_does_not_raise(config: Config) -> None:
    """A clean run with an empty JSON list yields [] (no results is normal)."""
    runner = RecordingRunner(returncode=0, stdout="[]")
    hs = _make(config, runner)
    assert hs.recall("nothing matches") == []


def test_recall_raises_on_permanent_exit(config: Config) -> None:
    """A permanent (exit 2) recall failure raises HindsightError, fail-fast."""
    runner = ScriptedRunner(script=[2])
    hs = _make(config, runner)
    with pytest.raises(HindsightError) as exc_info:
        hs.recall("q")
    assert "recall" in str(exc_info.value)
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# Bounded retry around the checked calls (#11).
# --------------------------------------------------------------------------- #


def test_retain_retries_transient_failure_then_succeeds(config: Config) -> None:
    """A transient non-zero exit is retried and the eventual success is accepted."""
    # First two attempts fail with a transient (non-permanent) exit, third succeeds.
    runner = ScriptedRunner(script=[1, 1, 0])
    hs = _make(config, runner, retries=3)
    hs.retain("entities/foo.md", "facts")  # must not raise
    assert len(runner.calls) == 3


def test_recall_retries_transient_failure_then_succeeds(config: Config) -> None:
    """recall retries a transient failure and parses the successful attempt's stdout."""
    runner = ScriptedRunner(
        script=[1, 0],
        stdout=_json_recall({"text": "x", "tags": ["entity", "entities/foo.md"]}),
    )
    hs = _make(config, runner, retries=3)
    hits = hs.recall("q")
    assert [h.path for h in hits] == ["entities/foo.md"]
    assert len(runner.calls) == 2


def test_retain_spawn_error_is_treated_as_transient_and_retried(
    config: Config,
) -> None:
    """An OSError spawn failure (daemon socket not up) is transient and retried."""
    runner = ScriptedRunner(script=[OSError("no such file"), OSError("nope"), 0])
    hs = _make(config, runner, retries=3)
    hs.retain("entities/foo.md", "facts")
    assert len(runner.calls) == 3


def test_retain_exhausts_retries_then_raises_transient_error(config: Config) -> None:
    """A persistently transient failure raises after exactly `retries` attempts."""
    runner = ScriptedRunner(script=[1])  # always exit 1
    hs = _make(config, runner, retries=3)
    with pytest.raises(HindsightTransientError):
        hs.retain("entities/foo.md", "facts")
    assert len(runner.calls) == 3


def test_permanent_failure_fails_fast_without_spawning_retries(
    config: Config,
) -> None:
    """A permanent (exit 2) failure raises immediately, no second spawn (#11)."""
    runner = ScriptedRunner(script=[2, 0])  # would succeed on a retry, but must not
    hs = _make(config, runner, retries=5)
    with pytest.raises(HindsightError) as exc_info:
        hs.retain("entities/foo.md", "facts")
    assert not isinstance(exc_info.value, HindsightTransientError)
    assert len(runner.calls) == 1


def test_retry_count_is_configurable_at_construction(config: Config) -> None:
    """The attempt count is configurable; retries=1 disables retry entirely."""
    runner = ScriptedRunner(script=[1])
    hs = _make(config, runner, retries=1)
    with pytest.raises(HindsightError):
        hs.retain("entities/foo.md", "facts")
    assert len(runner.calls) == 1  # no retry when retries=1


def test_transient_error_is_a_hindsight_error_subclass() -> None:
    """HindsightTransientError subclasses HindsightError so handlers still catch it."""
    assert issubclass(HindsightTransientError, HindsightError)


# --------------------------------------------------------------------------- #
# forget()  (check=False semantics, NO retry)
# --------------------------------------------------------------------------- #


def test_forget_builds_expected_argv_with_bank_positional(config: Config) -> None:
    """forget builds base_args + FORGET_SUBCOMMAND + [bank, rel_path]."""
    runner = RecordingRunner()
    hs = _make(config, runner)
    hs.forget("entities/foo.md")
    argv = runner.last
    assert argv == [*base_args(), *FORGET_SUBCOMMAND, DEFAULT_BANK, "entities/foo.md"]


def test_forget_does_not_raise_on_nonzero_exit_and_does_not_retry(
    config: Config,
) -> None:
    """forget swallows a non-zero exit (full-rebuild is the authoritative reset)."""
    runner = ScriptedRunner(script=[3])  # would be 'transient' for a checked call
    hs = _make(config, runner)
    hs.forget("entities/missing.md")  # must not raise
    # Best-effort: exactly one call, no retry even on a would-be-transient exit.
    assert len(runner.calls) == 1


def test_forget_swallows_spawn_errors(config: Config) -> None:
    """forget swallows an OSError spawn failure too (best-effort)."""
    runner = ScriptedRunner(script=[OSError("missing binary")])
    hs = _make(config, runner)
    hs.forget("entities/missing.md")  # must not raise
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# probe()  ("did it land?")
# --------------------------------------------------------------------------- #


def test_probe_true_when_path_among_hits(config: Config) -> None:
    """probe returns True when the recalled hits include the path."""
    runner = RecordingRunner(
        stdout=_json_recall(
            {"text": "fact", "tags": ["entity", "entities/foo.md"]},
            {"text": "fact2", "tags": ["concept", "concepts/bar.md"]},
        )
    )
    hs = _make(config, runner)
    assert hs.probe("concepts/bar.md", "anything") is True


def test_probe_false_when_path_absent(config: Config) -> None:
    """probe returns False when the path is not among the recalled hits."""
    runner = RecordingRunner(
        stdout=_json_recall({"text": "fact", "tags": ["entity", "entities/foo.md"]})
    )
    hs = _make(config, runner)
    assert hs.probe("entities/missing.md", "anything") is False


def test_probe_false_on_empty_recall(config: Config) -> None:
    """probe on an empty recall result is False (and issues exactly one call)."""
    runner = RecordingRunner(stdout="[]")
    hs = _make(config, runner)
    assert hs.probe("entities/foo.md", "q") is False
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# default_runner (the real seam, exercised without the absent CLI).
# --------------------------------------------------------------------------- #


def test_default_runner_captures_text_and_does_not_raise_on_nonzero() -> None:
    """default_runner returns a text CompletedProcess and never raises on exit code.

    Runs a tiny in-process Python child (always available) instead of the absent
    ``hindsight`` binary, proving capture_output/text/check=False semantics.
    """
    argv = [sys.executable, "-c", "import sys; print('hi'); sys.exit(5)"]
    result = default_runner(argv, timeout=30.0)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 5
    assert result.stdout.strip() == "hi"
    assert isinstance(result.stdout, str)  # text mode


def test_hindsight_uses_default_runner_when_none_injected(config: Config) -> None:
    """With no runner injected, Hindsight wires up default_runner."""
    hs = Hindsight(config)
    # Access the private seam only in tests (SLF001 allowed here).
    assert hs._runner is default_runner  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Import safety (no hindsight package pulled in at module import).
# --------------------------------------------------------------------------- #


def test_module_import_pulls_in_no_hindsight_package() -> None:
    """Importing thoth.hindsight must not import any 'hindsight' Python package.

    The wrapper is pure subprocess; only stdlib, tenacity, and thoth.config may appear
    at top level. A stray ``import hindsight`` would break collection in CI where the
    package is absent.
    """
    import thoth.hindsight  # noqa: F401  (already imported; this asserts on sys.modules)

    leaked = [
        name
        for name in sys.modules
        if name == "hindsight" or name.startswith("hindsight.")
    ]
    assert leaked == []
