"""Git sync for the vault, and the appliance's only path to git.

git is never an LLM tool (SPEC section 3). :class:`GitSync` shells out to two shipped
bash scripts, ``bin/vault-pull`` and ``bin/vault-commit``, and classifies their exit
codes. It never pushes ``--force`` and fails loudly on a rebase conflict.

The scripts push to the vault's own remote, ``THOTH_GIT_REMOTE`` (default ``origin``),
the same place the rebase pulled from, so no repository owner is hardcoded. Tests
redirect that at a local bare repo with ``THOTH_PUSH_REMOTE``, ``THOTH_GIT_REMOTE`` and
``THOTH_GIT_BRANCH``.

Only the standard library is imported here, so importing this at pytest collection is
always safe. Page content belongs to :mod:`thoth.vault` and is never touched here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from thoth.config import Config

VAULT_PULL_SCRIPT: str = "vault-pull"
"""Filename of the shipped pull-before-write bash script in :func:`bin_dir`."""

VAULT_COMMIT_SCRIPT: str = "vault-commit"
"""Filename of the shipped commit+push bash script in :func:`bin_dir`."""

VAULT_BOOTSTRAP_SCRIPT: str = "vault-bootstrap"
"""Filename of the shipped clone-an-empty-vault bash script in :func:`bin_dir`."""

# Sentinel emitted by vault-commit on a rebase conflict. Matching is substring-based so
# the rest of the line (the "resolve in Obsidian" guidance) can change without breaking
# the classification
_CONFLICT_SENTINEL: str = "VAULT CONFLICT"

# Stdout marker from vault-commit when nothing is staged, used to set
# GitResult.committed
_NOTHING_TO_COMMIT: str = "nothing to commit"

# Bounded retry on .git/index.lock contention. capture_lock only serialises this
# process, so a concurrent obsidian-git commit can still hold the index lock for a
# sub-second window. git does not block on the lock, it fails immediately, so a
# transient collision has to be retried here. Matching is substring-based so wording
# drift still classifies
_INDEX_LOCK_SIGNALS: tuple[str, ...] = ("unable to lock index", "index.lock")
_LOCK_RETRY_ATTEMPTS: int = 5
_LOCK_RETRY_BACKOFF: float = 0.1


def _is_index_lock_failure(returncode: int, stderr: str) -> bool:
    """True when a git run failed on ``.git/index.lock`` contention, otherwise False.

    Args:
        returncode: The git process exit code.
        stderr: The captured standard error text.

    Returns:
        True for a non-zero exit naming the index lock, otherwise False.
    """
    if returncode == 0:
        return False
    lowered = stderr.lower()
    return any(signal in lowered for signal in _INDEX_LOCK_SIGNALS)


def _run_with_lock_retry(
    run: Callable[[], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Runs ``run``, re-running it on a transient ``.git/index.lock`` collision.

    Up to :data:`_LOCK_RETRY_ATTEMPTS` tries with a growing backoff, to give the
    competing client time to release the lock. Any other outcome is returned on the
    first occurrence so the caller's own classification is untouched, and an exhausted
    retry returns the last attempt so git's stderr surfaces as it would from a single
    run.

    Args:
        run: Callable performing one ``subprocess.run``, already ``check=False``.

    Returns:
        The completed process of the final attempt.
    """
    completed = run()
    for attempt in range(1, _LOCK_RETRY_ATTEMPTS):
        if not _is_index_lock_failure(completed.returncode, completed.stderr):
            return completed
        time.sleep(_LOCK_RETRY_BACKOFF * attempt)
        completed = run()
    return completed


class GitSyncError(Exception):
    """Base error for git sync failures, meaning a script exited non-zero."""


class VaultConflictError(GitSyncError):
    """Raised when ``vault-commit`` hits a rebase conflict, to resolve in Obsidian.

    The script has already aborted the rebase, so the remote is untouched. ``stderr``
    carries the ``VAULT CONFLICT`` line that the caller surfaces over Slack.
    """


@dataclass(frozen=True, slots=True)
class GitResult:
    """Outcome of a single sync-script run.

    Attributes:
        returncode: The script's process exit code.
        stdout: Captured standard output.
        stderr: Captured standard error.
        committed: False when ``vault-commit`` reported nothing to commit. A clean
            :meth:`GitSync.pull` sets it True to mean "ran cleanly", since pull
            never commits.
    """

    returncode: int
    stdout: str
    stderr: str
    committed: bool


@dataclass(frozen=True, slots=True)
class Divergence:
    """How far the local vault branch is ahead of its push remote (issue #15).

    Computed from git and never from an LLM, so
    :meth:`thoth.alerts.Alerter.alert_unpushed_divergence` can report "N commits
    unpushed since T" when a rebase conflict refuses the push.

    Attributes:
        commits_ahead: Local commits not yet on the remote, or -1 when unknown.
        since: Author time of the oldest unpushed commit, or None.
    """

    commits_ahead: int
    since: datetime | None


def _resolve_bin_dir(module_path: Path) -> Path:
    """Finds the shipped ``bin/`` directory by walking up from ``module_path``.

    Returns the first ancestor holding a ``vault-pull`` script, which is the repo root
    in an editable or CI checkout. A non-editable install has no such ancestor, so
    ``PATH`` is consulted next. Falls back to a repo-root guess so the path is always
    concrete.

    Args:
        module_path: The resolved path of this module file.

    Returns:
        The bin directory, whose existence is not guaranteed in the fallback.
    """
    for ancestor in module_path.parents:
        candidate = ancestor / "bin"
        if (candidate / VAULT_PULL_SCRIPT).is_file():
            return candidate
    # Non-editable install (the container): no ancestor holds the scripts, but the
    # Dockerfile copies them onto PATH at /usr/local/bin, so honour PATH before guessing
    which = shutil.which(VAULT_PULL_SCRIPT)
    if which is not None:
        return Path(which).resolve().parent
    parents = module_path.parents
    repo_root = parents[2] if len(parents) > 2 else parents[-1]
    return repo_root / "bin"


def bin_dir() -> Path:
    """Returns the path to the shipped ``bin/`` directory.

    Returns:
        The absolute bin directory, which is not guaranteed to exist.
    """
    return _resolve_bin_dir(Path(__file__).resolve())


class GitSync:
    """Deterministic wrapper running the bash sync scripts for one vault.

    Cheap and stateless beyond its configuration. ``PKM_VAULT`` is forced into the child
    environment from ``config.vault_path``, so the scripts and :mod:`thoth.vault` cannot
    disagree on the root.
    """

    def __init__(
        self,
        config: Config,
        *,
        env: Mapping[str, str] | None = None,
        bin_path: Path | None = None,
    ) -> None:
        """Builds a :class:`GitSync` for ``config``'s vault.

        Args:
            config: Frozen runtime config owning the vault root.
            env: Base child environment, defaulting to :data:`os.environ`. An
                ambient ``PKM_VAULT`` in it never wins.
            bin_path: Directory holding the sync scripts, defaulting to
                :func:`bin_dir`.
        """
        self._config = config
        self._vault_root = config.vault_path
        base_env: Mapping[str, str] = os.environ if env is None else env
        child_env = dict(base_env)
        child_env["PKM_VAULT"] = str(config.vault_path)
        self._child_env = child_env
        self._bin_path = bin_dir() if bin_path is None else bin_path
        # Per-tree mutex for a capture's tree-mutating sections, rationale on
        # capture_lock
        self._capture_lock = threading.RLock()

    @property
    def capture_lock(self) -> AbstractContextManager[bool]:
        """Re-entrant mutex for the tree-mutating sections of a capture (issue #85).

        :class:`thoth.ingest.Ingestor` holds it only around the orient pull and the
        log-append, stage, commit, rebase, push sequence, and never across the slow
        analyse, classify and curate LLM passes. So concurrent captures overlap on the
        expensive work and serialise only on the commit.

        Re-entrant, so a held section that nests :meth:`commit` or :meth:`pull` cannot
        self-deadlock. One ``GitSync`` per vault per process makes this the per-tree
        mutex.
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
        """Runs ``vault-pull``, a ``pull --rebase --autostash`` onto current state.

        Args:
            timeout: Seconds before the script is killed.

        Returns:
            The result, with ``committed=True`` meaning it ran cleanly.

        Raises:
            GitSyncError: if the script exits non-zero.
        """
        return self._run_checked(VAULT_PULL_SCRIPT, (), timeout=timeout)

    def bootstrap(self, *, timeout: float = 300.0) -> GitResult:
        """Runs ``vault-bootstrap`` to clone the vault into an empty ``$PKM_VAULT``.

        A freshly provisioned cluster mounts an empty vault PVC and nothing else clones
        the repo, so the script init, fetches and checks out ``THOTH_VAULT_REPO_URL``
        into the mount point. It tolerates a non-empty mount dir such as a
        ``lost+found``.

        It is a no-op when ``$PKM_VAULT`` is already a git repo, and when
        ``THOTH_VAULT_REPO_URL`` is unset, which is the dev and test default. Run it as
        a Helm initContainer before each vault-mounting workload.

        Args:
            timeout: Seconds before the script is killed, allowing for a full clone.

        Returns:
            The result, whose stdout line reports whether it cloned or skipped.

        Raises:
            GitSyncError: if the script exits non-zero.
        """
        return self._run_checked(VAULT_BOOTSTRAP_SCRIPT, (), timeout=timeout)

    def stage(self, paths: Sequence[str], *, timeout: float = 30.0) -> None:
        """Stages exactly ``paths`` with ``git add``, and never ``add -A``.

        Used by the batch import path, where each capture stages its own paths up front
        and a single later :meth:`commit` commits the accumulated index. Staging only
        this capture's own paths means a batch commit cannot sweep an unrelated
        capture's untracked file (issue #85).

        A path may name a deletion, such as a superseded ``inbox/`` hold. A
        never-tracked, now-deleted hold is dropped, because passing it to ``git add``
        would fail the whole call on an unmatched pathspec.

        Empty ``paths`` is a no-op. Runs ``git`` directly rather than a sync script,
        with the caller holding :attr:`capture_lock`.

        Args:
            paths: Vault-relative paths to stage.
            timeout: Seconds before the ``git add`` is killed.

        Raises:
            GitSyncError: if ``git add`` exits non-zero.
        """
        stageable = self._stageable(paths, timeout=timeout)
        if not stageable:
            return
        # Retried on index.lock contention, a non-lock failure raises below unchanged
        completed = _run_with_lock_retry(
            lambda: self._exec(["git", "add", "--", *stageable], timeout=timeout)
        )
        if completed.returncode != 0:
            raise GitSyncError(
                f"git add failed (exit {completed.returncode}). "
                f"stderr: {completed.stderr.strip()!r}"
            )

    def _stageable(self, paths: Sequence[str], *, timeout: float) -> list[str]:
        """Keeps only the paths that exist in the working tree or are already tracked.

        A path that is neither is a never-committed hold removed within the same run. It
        carries no git change, and leaving it in would abort ``git add`` on an unmatched
        pathspec. Assumes plain file paths rather than globs.
        """
        statuses = [(path, (self._vault_root / path).exists()) for path in paths]
        missing = [path for path, on_disk in statuses if not on_disk]
        tracked: set[str] = set()
        if missing:
            # One batched probe for every missing path. No --error-unmatch as it aborts
            # on the first unmatched pathspec, and -z neutralises core.quotePath so
            # non-ASCII paths round-trip verbatim
            completed = self._exec(
                ["git", "ls-files", "-z", "--", *missing], timeout=timeout
            )
            if completed.returncode == 0:
                tracked = set(completed.stdout.split("\0")) - {""}
        return [path for path, on_disk in statuses if on_disk or path in tracked]

    def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        timeout: float = 120.0,
    ) -> GitResult:
        """Runs ``vault-commit``, which stages, commits, rebases and pushes.

        ``paths`` is the exact set staged for this commit, so it cannot sweep a
        concurrent capture's untracked asset and orphan an embedded ``![[asset]]``
        (issue #85). The script prefixes the subject with ``agent:`` and never pushes
        ``--force``. Nothing staged returns ``committed=False`` rather than raising.

        Args:
            message: The commit subject.
            paths: Vault-relative paths to stage, or None for the staged index. A
                path may name a deletion.
            timeout: Seconds before the script is killed.

        Returns:
            The result, with ``committed=False`` when nothing was staged.

        Raises:
            VaultConflictError: on a rebase conflict, where the rebase has been
                aborted and the remote is unchanged.
            GitSyncError: on any other non-zero exit.
        """
        script_args = (message, "--", *paths) if paths is not None else (message,)
        return self._run_checked(
            VAULT_COMMIT_SCRIPT, script_args, timeout=timeout, classify_conflict=True
        )

    def divergence(self, *, timeout: float = 30.0) -> Divergence:
        """Counts local vault commits ahead of the rebase tracking ref.

        Measured against ``THOTH_GIT_REMOTE`` and ``THOTH_GIT_BRANCH``, not
        ``THOTH_PUSH_REMOTE``, which may differ. Only called from the conflict path,
        where ``vault-pull`` has just refreshed that ref, so the count is accurate at
        alert time. Any failure is swallowed and reported as ``commits_ahead=-1``, so
        this can be called from inside a conflict handler without raising anew.

        Args:
            timeout: Seconds before each read-only git probe is killed.

        Returns:
            The unpushed local commit count and the oldest commit's time.
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
        if ahead == 0:
            return Divergence(commits_ahead=0, since=None)
        # Author time of the oldest unpushed commit, which is the "unpushed since T"
        # stamp
        oldest = self._git_text(
            ("log", "--reverse", "--format=%at", rng), timeout=timeout
        )
        since = _parse_first_epoch(oldest)
        return Divergence(commits_ahead=ahead, since=since)

    def _git_text(self, args: Sequence[str], *, timeout: float) -> str | None:
        """Runs ``git <args>`` read-only in the vault, returning stdout or ``None``."""
        try:
            completed = self._exec(["git", *args], timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _exec(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Runs one child process in the vault root with the forced environment.

        Every subprocess this class spawns comes through here.

        Args:
            argv: The full argument vector, never a shell string.
            timeout: Seconds before the process is killed.

        Returns:
            The completed process, run ``check=False`` so a failure returns for the
            caller's own classification.
        """
        return subprocess.run(
            list(argv),
            cwd=str(self._vault_root),
            env=self._child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _run(self, script: str, args: Sequence[str], *, timeout: float) -> GitResult:
        """Runs one sync script and classifies its result.

        Args:
            script: The script filename, such as :data:`VAULT_COMMIT_SCRIPT`.
            args: Positional arguments passed to the script.
            timeout: Seconds before the script is killed.

        Returns:
            The result, whose ``committed`` comes from the ``nothing to commit``
            stdout marker that only ``vault-commit`` emits.
        """
        script_path = self._bin_path / script
        # Fixed argv, no shell=True. The script name is a module constant and the vault
        # root comes from the frozen Config, so there is no injection surface.
        # Retried on index.lock contention: these scripts stage and rebase, so a
        # concurrent client holding the lock would otherwise fail the whole capture
        completed = _run_with_lock_retry(
            lambda: self._exec(["bash", str(script_path), *args], timeout=timeout)
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

    def _run_checked(
        self,
        script: str,
        args: Sequence[str],
        *,
        timeout: float,
        classify_conflict: bool = False,
    ) -> GitResult:
        """Runs one sync script via :meth:`_run` and raises on a non-zero exit.

        Args:
            script: The script filename, such as :data:`VAULT_COMMIT_SCRIPT`.
            args: Positional arguments passed to the script.
            timeout: Seconds before the script is killed.
            classify_conflict: Raise :class:`VaultConflictError` on a ``VAULT
                CONFLICT`` stderr, which only ``vault-commit`` emits.

        Returns:
            The result of a clean, zero-exit run.

        Raises:
            VaultConflictError: on a rebase conflict when ``classify_conflict`` is set.
            GitSyncError: on any other non-zero exit.
        """
        result = self._run(script, args, timeout=timeout)
        if result.returncode != 0:
            if classify_conflict and _CONFLICT_SENTINEL in result.stderr:
                raise VaultConflictError(
                    self._format_failure(script, result),
                )
            raise GitSyncError(
                self._format_failure(script, result),
            )
        return result

    @staticmethod
    def _format_failure(script: str, result: GitResult) -> str:
        """Builds a diagnostic message carrying the script's exit code and output."""
        return (
            f"{script} failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )


def _parse_first_epoch(text: str | None) -> datetime | None:
    """Parses the first line of git ``%at`` output into an aware datetime."""
    if not text:
        return None
    tokens = text.split()
    first = tokens[0] if tokens else ""
    if not first:
        return None
    try:
        epoch = int(first)
    except ValueError:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)
