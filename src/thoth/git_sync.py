"""Deterministic git sync wrapper for the canonical vault.

This module is the appliance's *only* path to git, and git is **never an LLM
tool** (SPEC section 3): :class:`GitSync` shells out to two shipped bash scripts
— ``bin/vault-pull`` (pull --rebase --autostash before any write) and
``bin/vault-commit`` (stage explicit paths, commit, rebase, push) — and
classifies their exit codes into typed results. It never pushes ``--force`` and
fails loudly, surfacing the conflicting path, on a rebase conflict.

The Slack daemon dispatches each capture on a worker thread, all sharing one git
working tree, so :class:`GitSync` carries a re-entrant :attr:`GitSync.capture_lock`
that :class:`thoth.ingest.Ingestor` holds **only** around the small tree-mutating
critical sections (the orient pull, and the log-append → stage → commit → rebase →
push sequence) — never across the slow LLM passes. :meth:`commit` stages the
*explicit* paths a single capture wrote (``git add -- <paths>``), not the whole
tree, so a commit can never sweep a different, concurrent capture's untracked asset
into the wrong commit and orphan an embedded ``![[asset]]`` (issue #85).

The two scripts carry the SPEC's git wrappers (``GIT_CONFIG_GLOBAL=/dev/null`` +
``gh``'s credential helper, ``pull --rebase``, never ``--force``). They push back to
the vault's **own** remote — ``THOTH_GIT_REMOTE`` (default ``origin``), the place the
rebase pulled from — so no repository owner is hardcoded; if that remote is not
configured and ``THOTH_PUSH_REMOTE`` is unset the commit script fails loudly rather than
guessing. For tests and CI they honour ``THOTH_PUSH_REMOTE`` / ``THOTH_GIT_REMOTE`` /
``THOTH_GIT_BRANCH`` overrides (defaulting to ``origin`` / ``origin`` / ``main``), so a
test can redirect both the rebase and the push at a local bare repo.

Only the standard library is imported at module top level (``subprocess``,
``pathlib``, ``dataclasses``, ``os``); there is no network or third-party import,
so importing this module at pytest collection is always safe. The vault root and
child-environment ``PKM_VAULT`` are taken from the frozen :class:`thoth.config.Config`
so the scripts and :mod:`thoth.vault` always agree on the root. This module never
parses or writes page content (strict separation from :mod:`thoth.vault`).
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from thoth.config import Config

VAULT_PULL_SCRIPT: str = "vault-pull"
"""Filename of the shipped pull-before-write bash script in :func:`bin_dir`."""

VAULT_COMMIT_SCRIPT: str = "vault-commit"
"""Filename of the shipped commit+push bash script in :func:`bin_dir`."""

# Sentinel emitted by vault-commit when the rebase hits a conflict (verbatim
# prefix from the script). Matching is substring-based so the rest of the line
# (the human-facing "resolve in Obsidian" guidance) can evolve without breaking
# the classification.
_CONFLICT_SENTINEL: str = "VAULT CONFLICT"

# Stdout marker emitted by vault-commit when `git diff --cached --quiet` finds no
# staged changes (verbatim from the script). Used to set GitResult.committed.
_NOTHING_TO_COMMIT: str = "nothing to commit"


class GitSyncError(Exception):
    """Base error for git sync failures (a script exited non-zero)."""


class VaultConflictError(GitSyncError):
    """Raised when ``vault-commit`` hits a rebase conflict (must resolve in Obsidian).

    The rebase has already been aborted by the script (no ``--force``, the remote
    is untouched); the captured ``stderr`` carries the ``VAULT CONFLICT`` line that
    the caller surfaces over Slack.
    """


@dataclass(frozen=True, slots=True)
class GitResult:
    """Outcome of a single sync-script run.

    Attributes:
        returncode: The script's process exit code.
        stdout: Captured standard output (text).
        stderr: Captured standard error (text).
        committed: ``False`` when ``vault-commit`` reported "nothing to commit"
            (no staged changes); ``True`` when a commit was made. Always ``True``
            for a successful :meth:`GitSync.pull` (pull does not commit, but the
            field is unused there and set ``True`` to mean "ran cleanly").
    """

    returncode: int
    stdout: str
    stderr: str
    committed: bool


@dataclass(frozen=True, slots=True)
class Divergence:
    """How far the local vault branch is ahead of its push remote (issue #15).

    Computed deterministically from git (never an LLM) so the unpushed-divergence alert
    (:meth:`thoth.alerts.Alerter.alert_unpushed_divergence`) can report "N commits
    unpushed since T" when a rebase conflict refuses the push.

    Attributes:
        commits_ahead: Number of local commits not present on the remote tracking ref
            (``git rev-list --count <remote>/<branch>..HEAD``). ``-1`` when it could not
            be determined (e.g. no remote tracking ref / not a git tree).
        since: Author time of the *oldest* unpushed commit (the first that diverged from
            the remote), or ``None`` when unknown / nothing is ahead.
    """

    commits_ahead: int
    since: datetime | None


def _resolve_bin_dir(module_path: Path) -> Path:
    """Resolve the shipped ``bin/`` directory relative to ``module_path``.

    Walks up the ancestors of ``module_path`` (this module's resolved location)
    and returns the first ``<ancestor>/bin`` that actually holds a
    ``vault-pull`` script. When none is found, falls back to the repo-root guess
    (``parents[2]`` for ``src/thoth/git_sync.py``) so the path is always concrete.

    Args:
        module_path: The resolved path of this module file.

    Returns:
        The ``bin/`` directory path (existence not guaranteed in the fallback).
    """
    for ancestor in module_path.parents:
        candidate = ancestor / "bin"
        if (candidate / VAULT_PULL_SCRIPT).is_file():
            return candidate
    parents = module_path.parents
    repo_root = parents[2] if len(parents) > 2 else parents[-1]
    return repo_root / "bin"


def bin_dir() -> Path:
    """Return the absolute path to the shipped ``bin/`` directory.

    Resolves by walking up from this module's location to the first ancestor that
    contains a ``vault-pull`` script (the repo root in an editable install and in
    CI). Falls back to a repo-root guess so the path is always concrete even
    before the scripts exist on disk.

    Returns:
        The absolute ``bin/`` directory path (not guaranteed to exist).
    """
    return _resolve_bin_dir(Path(__file__).resolve())


class GitSync:
    """Deterministic wrapper running the bash sync scripts for one vault.

    The instance is cheap and stateless beyond its configuration; construct it
    from the frozen :class:`~thoth.config.Config` that owns the vault root. The
    child environment for every script run is derived once from ``env`` (defaulting
    to :data:`os.environ`) with ``PKM_VAULT`` forced to ``str(config.vault_path)``
    so the scripts and :mod:`thoth.vault` cannot disagree on the root.
    """

    def __init__(
        self,
        config: Config,
        *,
        env: Mapping[str, str] | None = None,
        bin_path: Path | None = None,
    ) -> None:
        """Build a :class:`GitSync` for ``config``'s vault.

        Args:
            config: The frozen runtime configuration; ``config.vault_path`` is the
                vault root (the scripts' working directory and ``PKM_VAULT``).
            env: Base environment for child processes; defaults to
                :data:`os.environ`. ``PKM_VAULT`` is always overridden to the
                config vault path, so an ambient ``PKM_VAULT`` cannot win.
            bin_path: Directory holding the sync scripts; defaults to
                :func:`bin_dir`.
        """
        self._config = config
        self._vault_root = config.vault_path
        base_env: Mapping[str, str] = os.environ if env is None else env
        child_env = dict(base_env)
        child_env["PKM_VAULT"] = str(config.vault_path)
        self._child_env = child_env
        self._bin_path = bin_dir() if bin_path is None else bin_path
        # Serialises the small tree-mutating critical sections of a capture (the orient
        # pull and the log-append -> stage -> commit -> rebase -> push) against any
        # other capture sharing this single git working tree (issue #85). The Slack
        # daemon dispatches events on a worker-thread pool, so two commits or two
        # rebases could otherwise collide on ``.git/index.lock`` or lose a ``log.md``
        # append (the shared append target), and a ``pull --rebase`` rewrites the whole
        # tree. The lock is NOT held across the slow classify/analyse/curate LLM passes,
        # so captures stay concurrent there; the orphaned-asset fix proper is
        # explicit-path staging in :meth:`commit`, and this lock only closes the
        # residual shared-state races. It is re-entrant so a held critical section that
        # nests :meth:`commit`/:meth:`pull` never self-deadlocks. One ``GitSync`` per
        # vault per process, so the instance lock is the per-working-tree mutex.
        self._capture_lock = threading.RLock()

    @property
    def capture_lock(self) -> AbstractContextManager[bool]:
        """Re-entrant mutex for the tree-mutating critical sections of a capture (#85).

        :class:`thoth.ingest.Ingestor` acquires it ONLY around the orient pull and the
        log-append → stage → commit → rebase → push sequence — the sub-second sections
        that touch the single shared git working tree, ``.git/index.lock``, or the
        shared ``log.md`` — and **never** across the slow analyse/classify/curate LLM
        passes, so concurrent captures (the Slack daemon runs each on a worker thread)
        overlap on the expensive work and only serialise on the commit. Re-entrant (an
        :class:`RLock`), so a held section that nests :meth:`commit`/:meth:`pull` does
        not self-deadlock. One ``GitSync`` per vault per process, so this instance lock
        is the per-working-tree mutex. Returned as a context manager
        (``with git.capture_lock:``).
        """
        return self._capture_lock

    @property
    def vault_root(self) -> Path:
        """The vault root the scripts run against (``== config.vault_path``)."""
        return self._vault_root

    @property
    def bin_path(self) -> Path:
        """The directory the sync scripts are resolved from."""
        return self._bin_path

    def pull(self, *, timeout: float = 120.0) -> GitResult:
        """Run ``vault-pull`` (``pull --rebase --autostash``) onto current state.

        Args:
            timeout: Seconds to allow the script before
                :class:`subprocess.TimeoutExpired` is raised.

        Returns:
            The :class:`GitResult` (``committed=True`` meaning "ran cleanly").

        Raises:
            GitSyncError: if the script exits non-zero (stderr/stdout attached to
                the message).
        """
        result = self._run(VAULT_PULL_SCRIPT, (), timeout=timeout)
        if result.returncode != 0:
            raise GitSyncError(
                self._format_failure("vault-pull", result),
            )
        return result

    def stage(self, paths: Sequence[str], *, timeout: float = 30.0) -> None:
        """Stage exactly ``paths`` in the working tree (``git add -- <paths>``).

        Used by the batch import path (``thoth capture``), where each capture stages its
        own page/raw/asset/``log.md`` paths up front and a single later :meth:`commit`
        (with no ``paths``) commits the accumulated index. Staging only this capture's
        own paths — never ``add -A`` — means a later batch commit cannot sweep an
        unrelated capture's untracked file (issue #85). A path may name a deletion (a
        superseded ``inbox/`` hold), which ``git add`` stages when the file is tracked.
        A never-tracked, now-deleted hold (created AND removed within one uncommitted
        run) exists in neither the working tree nor the index, so it is dropped —
        passing it to ``git add`` would fail the whole call on an unmatched pathspec.
        Empty ``paths`` is a no-op.

        Runs ``git`` directly (deterministic, like :meth:`divergence`), not a sync
        script. Held under :attr:`capture_lock` by the caller so it never races another
        capture's stage/commit on the shared index.

        Args:
            paths: Vault-relative paths to stage.
            timeout: Seconds to allow the ``git add`` before it is killed.

        Raises:
            GitSyncError: if ``git add`` exits non-zero.
        """
        stageable = self._stageable(paths, timeout=timeout)
        if not stageable:
            return
        completed = subprocess.run(
            ["git", "add", "--", *stageable],
            cwd=str(self._vault_root),
            env=self._child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise GitSyncError(
                f"git add failed (exit {completed.returncode}). "
                f"stderr: {completed.stderr.strip()!r}"
            )

    def _stageable(self, paths: Sequence[str], *, timeout: float) -> list[str]:
        """Keep only paths that exist in the working tree or are already tracked.

        A path that is neither (a never-committed hold removed within the same run)
        would make ``git add -- <path>`` abort on an unmatched pathspec, so it is
        dropped — it carries no git change anyway. Order-preserving.
        """
        kept: list[str] = []
        for path in paths:
            if (self._vault_root / path).exists():
                kept.append(path)
                continue
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                cwd=str(self._vault_root),
                env=self._child_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if tracked.returncode == 0:
                kept.append(path)
        return kept

    def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        timeout: float = 120.0,
    ) -> GitResult:
        """Run ``vault-commit <message> [-- <paths>]``: stage, commit, rebase, push.

        When ``paths`` is given they are the EXACT set staged for this commit
        (``git add -- <paths>`` in the script) — a single capture's own page(s), raw
        sidecar, assets, and ``log.md``, never the whole tree — so the commit cannot
        sweep a concurrent capture's untracked asset and orphan an embedded
        ``![[asset]]`` (issue #85). When ``paths`` is ``None`` the script commits
        whatever is already staged in the index (the batch path stages incrementally via
        :meth:`stage`). A path may name a deletion (a superseded ``inbox/`` hold).

        The script prefixes the commit subject with ``agent:`` and never pushes
        ``--force``. A clean run with no staged changes returns ``committed=False``
        and does **not** raise.

        Args:
            message: The commit subject (passed as the script's first argument).
            paths: The exact vault-relative paths to stage, or ``None`` to commit the
                already-staged index.
            timeout: Seconds to allow the script before
                :class:`subprocess.TimeoutExpired` is raised.

        Returns:
            The :class:`GitResult`; ``committed`` is ``False`` when nothing was
            staged, otherwise ``True``.

        Raises:
            VaultConflictError: on a rebase-conflict exit (stderr carries the
                ``VAULT CONFLICT`` line; the rebase has been aborted, the remote
                is unchanged).
            GitSyncError: on any other non-zero exit.
        """
        script_args = (message, "--", *paths) if paths is not None else (message,)
        result = self._run(VAULT_COMMIT_SCRIPT, script_args, timeout=timeout)
        if result.returncode != 0:
            if _CONFLICT_SENTINEL in result.stderr:
                raise VaultConflictError(
                    self._format_failure("vault-commit", result),
                )
            raise GitSyncError(
                self._format_failure("vault-commit", result),
            )
        return result

    def divergence(self, *, timeout: float = 30.0) -> Divergence:
        """Count local vault commits ahead of the rebase tracking ref.

        Measured against the ``THOTH_GIT_REMOTE`` / ``THOTH_GIT_BRANCH`` tracking ref
        the wrappers rebase onto (defaults ``origin`` / ``main``) -- not
        ``THOTH_PUSH_REMOTE``, which may differ. Only called from the conflict path,
        where ``vault-pull``'s ``pull --rebase`` has just refreshed that ref, so the
        count is accurate at alert time.

        Runs read-only ``git`` directly (not a sync script):
        ``rev-list --count <remote>/<branch>..HEAD`` for the
        ahead-count and the author time of the oldest commit in that range for
        ``since``. Any failure (no remote tracking ref, not a git tree, git error) is
        swallowed and reported as :class:`Divergence` ``(commits_ahead=-1, since=None)``
        so this can be called from inside a conflict handler without raising anew.

        Args:
            timeout: Seconds to allow each git probe.

        Returns:
            The :class:`Divergence` describing the unpushed local commits.
        """
        remote = self._child_env.get("THOTH_GIT_REMOTE", "origin")
        branch = self._child_env.get("THOTH_GIT_BRANCH", "main")
        rng = f"{remote}/{branch}..HEAD"
        count = self._git_text(("rev-list", "--count", rng), timeout=timeout)
        if count is None:
            return Divergence(commits_ahead=-1, since=None)
        try:
            ahead = int(count.strip())
        except ValueError:
            return Divergence(commits_ahead=-1, since=None)
        if ahead <= 0:
            return Divergence(commits_ahead=ahead if ahead == 0 else -1, since=None)
        # Author time (Unix seconds) of the OLDEST unpushed commit (the first that
        # diverged) -> the "unpushed since T" timestamp.
        oldest = self._git_text(
            ("log", "--reverse", "--format=%at", rng), timeout=timeout
        )
        since = _parse_first_epoch(oldest)
        return Divergence(commits_ahead=ahead, since=since)

    def _git_text(self, args: Sequence[str], *, timeout: float) -> str | None:
        """Run ``git <args>`` read-only in the vault, returning stdout or ``None``.

        Returns ``None`` on any non-zero exit or spawn failure so callers in an
        exception handler never see a new exception.
        """
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self._vault_root),
                env=self._child_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _run(self, script: str, args: Sequence[str], *, timeout: float) -> GitResult:
        """Run one sync script and classify its result.

        Invokes ``bash <bin_path>/<script> <args...>`` with the forced child
        environment and the vault root as the working directory, capturing text
        output. ``committed`` is derived from the ``nothing to commit`` stdout
        marker (only meaningful for ``vault-commit``).

        Args:
            script: The script filename (e.g. :data:`VAULT_COMMIT_SCRIPT`).
            args: Positional arguments passed to the script.
            timeout: Seconds before :class:`subprocess.TimeoutExpired`.

        Returns:
            The classified :class:`GitResult`.
        """
        script_path = self._bin_path / script
        # Fixed argv (no shell=True); the script name is a module constant and the
        # vault root comes from the frozen Config, so there is no injection surface.
        completed = subprocess.run(
            ["bash", str(script_path), *args],
            cwd=str(self._vault_root),
            env=self._child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        committed = (
            completed.returncode == 0 and _NOTHING_TO_COMMIT not in completed.stdout
        )
        return GitResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            committed=committed,
        )

    @staticmethod
    def _format_failure(script: str, result: GitResult) -> str:
        """Build a diagnostic message embedding the script's exit code and output."""
        return (
            f"{script} failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )


def _parse_first_epoch(text: str | None) -> datetime | None:
    """Parse the first line of git ``%at`` output (Unix seconds) into an aware datetime.

    Returns ``None`` for empty / unparseable input so a divergence probe never raises.
    """
    if not text:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first:
        return None
    try:
        epoch = int(first)
    except ValueError:
        return None
    return datetime.fromtimestamp(epoch, tz=_dt.UTC)
