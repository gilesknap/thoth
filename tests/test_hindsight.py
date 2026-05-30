"""Tests for :mod:`thoth.hindsight`.

Every test isolates the external boundary: no real ``hindsight-embed`` binary, no
Postgres, no Gemini. A :class:`RecordingRunner` fake stands in for the
:class:`~thoth.hindsight.SubprocessRunner` seam, recording the argv it is handed
and returning a canned :class:`subprocess.CompletedProcess`, so the tests assert on
the exact command line the wrapper builds and on how it classifies the result.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from thoth.config import Config, load_config
from thoth.hindsight import (
    BANK,
    BASE_ARGS,
    FORGET_SUBCOMMAND,
    RECALL_SUBCOMMAND,
    RETAIN_SUBCOMMAND,
    SOURCE_SENTINEL,
    Hindsight,
    HindsightError,
    RecallHit,
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


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A minimal :class:`Config` (only the required vault path is needed here)."""
    return load_config({"PKM_VAULT": str(tmp_path)})


def _make(
    config: Config, runner: RecordingRunner, *, timeout: float = 120.0
) -> Hindsight:
    """Build a :class:`Hindsight` wired to the recording runner."""
    return Hindsight(config, runner=runner, timeout=timeout)


# --------------------------------------------------------------------------- #
# Attested surface is pinned (SPEC section 8/15).
# --------------------------------------------------------------------------- #


def test_attested_base_args_and_bank_are_pinned() -> None:
    """BASE_ARGS and BANK match the attested CLI surface exactly."""
    assert BANK == "hermes"
    assert BASE_ARGS == ("hindsight-embed", "-p", "hermes")


def test_base_args_override_is_honoured(config: Config) -> None:
    """A per-instance base_args override re-points the CLI prefix (VPS-time seam)."""
    runner = RecordingRunner()
    hs = Hindsight(config, base_args=("hs", "-p", "other"), runner=runner)
    assert hs.base_args == ("hs", "-p", "other")
    hs.recall("anything")
    # The override prefixes the argv instead of the attested default.
    assert runner.last[:3] == ["hs", "-p", "other"]


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


def test_parse_recall_extracts_paths_order_preserving_and_deduped() -> None:
    """Multiple SOURCE: lines parse into RecallHit.path values, ordered and unique."""
    stdout = (
        "result 1 score=0.91\n"
        "SOURCE: entities/foo.md\n"
        "some fact text\n"
        "result 2 score=0.88\n"
        "SOURCE: concepts/bar.md\n"
        "another fact\n"
        "result 3 score=0.40\n"
        "SOURCE: entities/foo.md\n"  # duplicate of the first
        "repeat\n"
    )
    hits = parse_recall(stdout)
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]
    # The raw matched text is retained as provenance.
    assert all(h.text.startswith(SOURCE_SENTINEL) for h in hits)


def test_parse_recall_empty_stdout_returns_empty_list() -> None:
    """No SOURCE: lines (or empty stdout) yields [] without raising."""
    assert parse_recall("") == []
    assert parse_recall("no markers here\njust prose\n") == []


def test_parse_recall_ignores_non_leading_source_text() -> None:
    """A 'SOURCE:' that is not at line start is not treated as a marker."""
    # Leading whitespace before SOURCE: should not match (anchored at line start).
    assert parse_recall("  SOURCE: entities/foo.md\n") == []
    # Mid-line mention is ignored too.
    assert parse_recall("see SOURCE: entities/foo.md inline\n") == []


# --------------------------------------------------------------------------- #
# retain()
# --------------------------------------------------------------------------- #


def test_retain_builds_expected_argv_with_source_sentinel_and_tags(
    config: Config,
) -> None:
    """retain builds BASE_ARGS + RETAIN_SUBCOMMAND + --text(...) + --tags."""
    runner = RecordingRunner()
    hs = _make(config, runner)

    hs.retain("entities/foo.md", "Foo facts.", tags=["entity", "entities/foo.md"])

    argv = runner.last
    assert argv[: len(BASE_ARGS)] == list(BASE_ARGS)
    assert argv[len(BASE_ARGS) : len(BASE_ARGS) + len(RETAIN_SUBCOMMAND)] == list(
        RETAIN_SUBCOMMAND
    )
    assert "--text" in argv
    text_value = argv[argv.index("--text") + 1]
    # The SOURCE: sentinel travels inside the --text payload.
    assert text_value.startswith(f"{SOURCE_SENTINEL} entities/foo.md")
    assert "Foo facts." in text_value
    # Tags are comma-joined after --tags.
    assert "--tags" in argv
    assert argv[argv.index("--tags") + 1] == "entity,entities/foo.md"


def test_retain_drops_empty_tags_and_omits_flag_when_none(config: Config) -> None:
    """Empty tag strings are filtered; --tags is omitted entirely when none remain."""
    runner = RecordingRunner()
    hs = _make(config, runner)

    hs.retain("concepts/bar.md", "facts", tags=["", "concept", ""])
    assert runner.last[runner.last.index("--tags") + 1] == "concept"

    hs.retain("concepts/baz.md", "facts", tags=["", ""])
    assert "--tags" not in runner.last

    hs.retain("concepts/qux.md", "facts")  # default empty tags
    assert "--tags" not in runner.last


def test_retain_raises_hindsighterror_on_nonzero_exit(config: Config) -> None:
    """A non-zero CLI exit makes retain raise HindsightError with stderr surfaced."""
    runner = RecordingRunner(returncode=2, stderr="backend unreachable")
    hs = _make(config, runner)
    with pytest.raises(HindsightError) as exc_info:
        hs.retain("entities/foo.md", "facts")
    msg = str(exc_info.value)
    assert "retain" in msg
    assert "entities/foo.md" in msg
    assert "backend unreachable" in msg


def test_retain_passes_configured_timeout_to_runner(config: Config) -> None:
    """The configured timeout reaches the runner unchanged."""
    runner = RecordingRunner()
    hs = _make(config, runner, timeout=7.5)
    hs.retain("entities/foo.md", "facts")
    assert runner.timeouts[-1] == 7.5


# --------------------------------------------------------------------------- #
# recall()
# --------------------------------------------------------------------------- #


def test_recall_builds_argv_and_parses_paths(config: Config) -> None:
    """recall sends query + --limit and maps SOURCE: lines into RecallHit paths."""
    runner = RecordingRunner(
        stdout="SOURCE: entities/foo.md\nfact\nSOURCE: concepts/bar.md\nfact2\n"
    )
    hs = _make(config, runner)

    hits = hs.recall("how does foo work", limit=3)

    argv = runner.last
    assert argv[: len(BASE_ARGS)] == list(BASE_ARGS)
    assert argv[len(BASE_ARGS) : len(BASE_ARGS) + len(RECALL_SUBCOMMAND)] == list(
        RECALL_SUBCOMMAND
    )
    assert "how does foo work" in argv
    assert argv[argv.index("--limit") + 1] == "3"
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]
    assert all(isinstance(h, RecallHit) for h in hits)


def test_recall_empty_stdout_returns_empty_and_does_not_raise(config: Config) -> None:
    """A clean run with no markers yields [] (no results is a normal outcome)."""
    runner = RecordingRunner(returncode=0, stdout="")
    hs = _make(config, runner)
    assert hs.recall("nothing matches") == []


def test_recall_raises_hindsighterror_on_nonzero_exit(config: Config) -> None:
    """A non-zero exit on recall raises HindsightError (unlike forget)."""
    runner = RecordingRunner(returncode=1, stderr="index missing")
    hs = _make(config, runner)
    with pytest.raises(HindsightError) as exc_info:
        hs.recall("q")
    assert "recall" in str(exc_info.value)
    assert "index missing" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# forget()  (check=False semantics)
# --------------------------------------------------------------------------- #


def test_forget_builds_expected_argv(config: Config) -> None:
    """forget builds BASE_ARGS + FORGET_SUBCOMMAND + [rel_path]."""
    runner = RecordingRunner()
    hs = _make(config, runner)
    hs.forget("entities/foo.md")
    argv = runner.last
    assert argv == [*BASE_ARGS, *FORGET_SUBCOMMAND, "entities/foo.md"]


def test_forget_does_not_raise_on_nonzero_exit(config: Config) -> None:
    """forget swallows a non-zero exit (full-rebuild is the authoritative reset)."""
    runner = RecordingRunner(returncode=3, stderr="no such path")
    hs = _make(config, runner)
    # Must not raise: best-effort by design.
    hs.forget("entities/missing.md")
    assert runner.calls  # the call was still made


# --------------------------------------------------------------------------- #
# probe()  ("did it land?")
# --------------------------------------------------------------------------- #


def test_probe_true_when_path_among_hits(config: Config) -> None:
    """probe returns True when the recalled hits include the path."""
    runner = RecordingRunner(
        stdout="SOURCE: entities/foo.md\nfact\nSOURCE: concepts/bar.md\nfact2\n"
    )
    hs = _make(config, runner)
    assert hs.probe("concepts/bar.md", "anything") is True


def test_probe_false_when_path_absent(config: Config) -> None:
    """probe returns False when the path is not among the recalled hits."""
    runner = RecordingRunner(stdout="SOURCE: entities/foo.md\nfact\n")
    hs = _make(config, runner)
    assert hs.probe("entities/missing.md", "anything") is False


def test_probe_false_on_empty_recall(config: Config) -> None:
    """probe on an empty recall result is False (and issues exactly one call)."""
    runner = RecordingRunner(stdout="")
    hs = _make(config, runner)
    assert hs.probe("entities/foo.md", "q") is False
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# default_runner (the real seam, exercised without the absent CLI).
# --------------------------------------------------------------------------- #


def test_default_runner_captures_text_and_does_not_raise_on_nonzero() -> None:
    """default_runner returns a text CompletedProcess and never raises on exit code.

    Runs a tiny in-process Python child (always available) instead of the absent
    ``hindsight-embed`` binary, proving capture_output/text/check=False semantics.
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

    The wrapper is pure subprocess; only stdlib + thoth.config may appear at top
    level. A stray ``import hindsight`` would break collection in CI where the
    package is absent.
    """
    import thoth.hindsight  # noqa: F401  (already imported; this asserts on sys.modules)

    leaked = [
        name
        for name in sys.modules
        if name == "hindsight" or name.startswith("hindsight.")
    ]
    assert leaked == []
