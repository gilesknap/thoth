"""Deterministic git sync wrapper for the canonical vault.

This module is the appliance's *only* path to git, and git is **never an LLM tool**
(SPEC section 3). :class:`GitSync` shells out to two shipped bash scripts and
classifies their exit codes into typed results: ``bin/vault-pull``, which runs
``pull --rebase --autostash`` before any write, and ``bin/vault-commit``, which stages
explicit paths, commits, rebases and pushes. It never pushes ``--force``, and on a
rebase conflict it fails loudly and surfaces the conflicting path.

Concurrent captures share one git working tree, and the locking story lives on
:attr:`GitSync.capture_lock` (issue #85): what is serialised, what is not, and why.

The two scripts carry the SPEC's git wrappers: ``GIT_CONFIG_GLOBAL=/dev/null`` with
``gh``'s credential helper, ``pull --rebase``, and never ``--force``. They push back
to the vault's **own** remote, ``THOTH_GIT_REMOTE`` and by default ``origin``, the
place the rebase pulled from, so no repository owner is hardcoded. With that remote
unconfigured and ``THOTH_PUSH_REMOTE`` unset, the commit script fails loudly rather
than guesses. For tests and CI they honour the ``THOTH_PUSH_REMOTE``,
``THOTH_GIT_REMOTE`` and ``THOTH_GIT_BRANCH`` overrides, defaulting to ``origin``,
``origin`` and ``main``, so a test can redirect both the rebase and the push at a
local bare repo.

Module top level imports only the standard library: ``subprocess``, ``pathlib``,
``dataclasses`` and ``os``. There is no network or third-party import, so importing
this module at pytest collection is always safe. The vault root and the child
environment's ``PKM_VAULT`` come from the frozen :class:`thoth.config.Config`, so the
scripts and :mod:`thoth.vault` always agree on the root. This module never parses nor
writes page content, staying strictly separate from :mod:`thoth.vault`.
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

# Sentinel emitted by vault-commit when the rebase hits a conflict (verbatim
# prefix from the script). Matching is substring-based so the rest of the line
# (the human-facing "resolve in Obsidian" guidance) can evolve without breaking
# the classification.
_CONFLICT_SENTINEL: str = "VAULT CONFLICT"

# Stdout marker emitted by vault-commit when `git diff --cached --quiet` finds no
# staged changes (verbatim from the script). Used to set GitResult.committed.
_NOTHING_TO_COMMIT: str = "nothing to commit"

# Bounded retry on `.git/index.lock` contention. The Slack daemon dispatches each
# capture on a worker thread sharing one working tree, and :attr:`GitSync.capture_lock`
# only serialises *this* process's tree-mutating sections; a concurrent ``obsidian-git``
# commit (or any other client) can still hold the index lock for a sub-second window.
# git does not block on the lock, it fails immediately, so a transient collision must
# be retried here. The signal is git's own diagnostic on stderr; matching is
# substring-based so wording drift ("Unable to create index.lock") still classifies.
_INDEX_LOCK_SIGNALS: tuple[str, ...] = ("unable to lock index", "index.lock")
_LOCK_RETRY_ATTEMPTS: int = 5
_LOCK_RETRY_BACKOFF: float = 0.1


def _is_index_lock_failure(returncode: int, stderr: str) -> bool:
    """Return ``True`` when a git invocation failed on ``.git/index.lock`` contention.

    A non-zero exit whose ``stderr`` carries one of :data:`_INDEX_LOCK_SIGNALS`,
    case-insensitively, is a transient lock collision worth retrying. Everything else,
    success included, is not.

    Args:
        returncode: The git process exit code.
        stderr: The captured standard error text.

    Returns:
        ``True`` only for a non-zero exit that names the index lock.
    """
    if returncode == 0:
        return False
    lowered = stderr.lower()
    return any(signal in lowered for signal in _INDEX_LOCK_SIGNALS)


def _run_with_lock_retry(
    run: Callable[[], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Run ``run`` and re-run it on a transient ``.git/index.lock`` collision.

    Re-invokes ``run`` up to :data:`_LOCK_RETRY_ATTEMPTS` times while
    :func:`_is_index_lock_failure` reports an index-lock failure, sleeping a short,
    growing backoff between tries to let the competing client release the lock. ANY
    other outcome, success or a non-lock failure, returns to the caller unchanged on its
    first occurrence, so the caller's own classification is untouched. The last
    attempt's result returns even when it is still a lock failure, so an exhausted retry
    surfaces git's stderr exactly as a single run would.

    Args:
        run: A zero-argument callable performing one ``subprocess.run``, already
            configured with ``check=False`` so a failure returns rather than raises.

    Returns:
        The :class:`subprocess.CompletedProcess` of the final attempt.
    """
    completed = run()
    for attempt in range(1, _LOCK_RETRY_ATTEMPTS):
        if not _is_index_lock_failure(completed.returncode, completed.stderr):
            return completed
        time.sleep(_LOCK_RETRY_BACKOFF * attempt)
        completed = run()
    return completed


class GitSyncError(Exception):
    """Base error for a git sync failure, where a script exited non-zero."""


class VaultConflictError(GitSyncError):
    """Raised when ``vault-commit`` hits a rebase conflict, to resolve in Obsidian.

    The script has already aborted the rebase, with no ``--force`` and the remote
    untouched, and the captured ``stderr`` carries the ``VAULT CONFLICT`` line the
    caller surfaces over Slack.
    """


@dataclass(frozen=True, slots=True)
class GitResult:
    """Outcome of a single sync-script run.

    Attributes:
        returncode: The script's process exit code.
        stdout: Captured standard output, as text.
        stderr: Captured standard error, as text.
        committed: ``False`` when ``vault-commit`` reported "nothing to commit" with no
            staged changes, and ``True`` when a commit was made. Always ``True`` for a
            successful :meth:`GitSync.pull`, which does not commit, where the unused
            field means "ran cleanly".
    """

    returncode: int
    stdout: str
    stderr: str
    committed: bool


@dataclass(frozen=True, slots=True)
class Divergence:
    """How far the local vault branch is ahead of its push remote (issue #15).

    Computed deterministically from git, never an LLM, so the unpushed-divergence alert
    :meth:`thoth.alerts.Alerter.alert_unpushed_divergence` can report "N commits
    unpushed since T" when a rebase conflict refuses the push.

    Attributes:
        commits_ahead: Number of local commits absent from the remote tracking ref, from
            ``git rev-list --count <remote>/<branch>..HEAD``. ``-1`` when it could
            not be determined, as with no remote tracking ref or outside a git tree.
        since: Author time of the *oldest* unpushed commit, the first that diverged from
            the remote, or ``None`` when unknown or nothing is ahead.
    """

    commits_ahead: int
    since: datetime | None


def _resolve_bin_dir(module_path: Path) -> Path:
    """Resolve the shipped ``bin/`` directory relative to ``module_path``.

    Walks up the ancestors of ``module_path``, this module's resolved location, and
    returns the first ``<ancestor>/bin`` that actually holds a ``vault-pull`` script.
    That is the repo root in an editable, dev or CI checkout, where the scripts sit
    beside the source tree. When NO ancestor carries them, as in a non-editable install
    such as the container image, where the package lives under ``site-packages/thoth/``
    and the wrappers were copied to ``/usr/local/bin`` on ``PATH``, it consults ``PATH``
    through :func:`shutil.which` and returns ``vault-pull``'s directory when found. Only
    when both miss does it fall back to the repo-root guess, ``parents[2]`` for
    ``src/thoth/git_sync.py``, so the path is always concrete.

    Args:
        module_path: The resolved path of this module file.

    Returns:
        The ``bin/`` directory path, whose existence the fallback cannot guarantee.
    """
    for ancestor in module_path.parents:
        candidate = ancestor / "bin"
        if (candidate / VAULT_PULL_SCRIPT).is_file():
            return candidate
    # Non-editable install (the container): no ancestor holds the scripts, but the
    # Dockerfile copies them onto PATH (/usr/local/bin). Honour PATH before guessing.
    which = shutil.which(VAULT_PULL_SCRIPT)
    if which is not None:
        return Path(which).resolve().parent
    parents = module_path.parents
    repo_root = parents[2] if len(parents) > 2 else parents[-1]
    return repo_root / "bin"


def bin_dir() -> Path:
    """Return the absolute path to the shipped ``bin/`` directory.

    :func:`_resolve_bin_dir` does the resolution.

    Returns:
        The absolute ``bin/`` directory path, which is not guaranteed to exist.
    """
    return _resolve_bin_dir(Path(__file__).resolve())


class GitSync:
    """Deterministic wrapper running the bash sync scripts for one vault.

    The instance is cheap and stateless beyond its configuration, so construct it from
    the frozen :class:`~thoth.config.Config` that owns the vault root. The child
    environment for every script run derives once from ``env``, defaulting to
    :data:`os.environ`, with ``PKM_VAULT`` forced to ``str(config.vault_path)``, so the
    scripts and :mod:`thoth.vault` cannot disagree on the root.
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
            config: The frozen runtime configuration, whose ``config.vault_path`` is the
                vault root, the scripts' working directory and ``PKM_VAULT``.
            env: Base environment for child processes, defaulting to :data:`os.environ`.
                ``PKM_VAULT`` is always overridden to the config vault path, so an
                ambient ``PKM_VAULT`` cannot win.
            bin_path: Directory holding the sync scripts, defaulting to :func:`bin_dir`.
        """
        self._config = config
        self._vault_root = config.vault_path
        base_env: Mapping[str, str] = os.environ if env is None else env
        child_env = dict(base_env)
        child_env["PKM_VAULT"] = str(config.vault_path)
        self._child_env = child_env
        self._bin_path = bin_dir() if bin_path is None else bin_path
        # Per-working-tree mutex for a capture's tree-mutating critical sections; the
        # full rationale lives on the :attr:`capture_lock` property docstring (#85).
        self._capture_lock = threading.RLock()

    @property
    def capture_lock(self) -> AbstractContextManager[bool]:
        """Re-entrant mutex for the tree-mutating critical sections of a capture (#85).

        :class:`thoth.ingest.Ingestor` acquires it ONLY around the orient pull and the
        log-append, stage, commit, rebase and push sequence, the sub-second sections
        that touch the single shared git working tree, ``.git/index.lock`` or the shared
        ``log.md``. It **never** holds it across the slow analyse, classify and curate
        LLM passes, so concurrent captures, which the Slack daemon runs each on a worker
        thread, overlap on the expensive work and serialise only on the commit. It is
        re-entrant, an :class:`RLock`, so a held section nesting :meth:`commit` or
        :meth:`pull` does not self-deadlock. There is one ``GitSync`` per vault per
        process, so this instance lock is the per-working-tree mutex. It returns as a
        context manager, used as ``with git.capture_lock:``.
        """
        return self._capture_lock

    @property
    def vault_root(self) -> Path:
        """The vault root the scripts run against, equal to ``config.vault_path``."""
        return self._vault_root

    @property
    def bin_path(self) -> Path:
        """The directory the sync scripts are resolved from."""
        return self._bin_path

    def pull(self, *, timeout: float = 120.0) -> GitResult:
        """Run ``vault-pull``, a ``pull --rebase --autostash`` onto current state.

        Args:
            timeout: Seconds to allow the script before it raises
                :class:`subprocess.TimeoutExpired`.

        Returns:
            The :class:`GitResult`, where ``committed=True`` means "ran cleanly".

        Raises:
            GitSyncError: when the script exits non-zero, with stderr and stdout
                attached to the message.
        """
        return self._run_checked(VAULT_PULL_SCRIPT, (), timeout=timeout)

    def bootstrap(self, *, timeout: float = 300.0) -> GitResult:
        """Run ``vault-bootstrap``: clone the vault into an empty ``$PKM_VAULT``.

        A freshly-provisioned cluster mounts an empty vault PVC with no ``.git``, and
        nothing else clones the vault repo, so the script inits, fetches and checks out
        the ``THOTH_VAULT_REPO_URL`` repo into the mount point, tolerating a non-empty
        mount dir such as one holding ``lost+found``. It is a **no-op** when
        ``$PKM_VAULT`` is already a git repo, the steady state, and when
        ``THOTH_VAULT_REPO_URL`` is unset, the dev and test default that makes bootstrap
        opt-in through the cluster overlay. Both cases exit cleanly without touching the
        tree. A Helm initContainer runs it before each vault-mounting workload.

        Args:
            timeout: Seconds to allow the script, a full clone, before it raises
                :class:`subprocess.TimeoutExpired`.

        Returns:
            The :class:`GitResult`, where ``committed=True`` means "ran cleanly" and the
            stdout line reports whether it cloned or skipped.

        Raises:
            GitSyncError: when the script exits non-zero, with stderr and stdout
                attached to the message.
        """
        return self._run_checked(VAULT_BOOTSTRAP_SCRIPT, (), timeout=timeout)

    def stage(self, paths: Sequence[str], *, timeout: float = 30.0) -> None:
        """Stage exactly ``paths`` in the working tree (``git add -- <paths>``).

        The batch import path, ``thoth capture``, uses this: each capture stages its own
        page, raw, asset and ``log.md`` paths up front, and a single later
        :meth:`commit` with no ``paths`` commits the accumulated index. Staging only
        this capture's own paths, never ``add -A``, means a later batch commit cannot
        sweep an unrelated capture's untracked file (issue #85). A path may name a
        deletion, such as a superseded ``inbox/`` hold, which ``git add`` stages when
        the file is tracked. A never-tracked, now-deleted hold, created AND removed
        within one uncommitted run, exists in neither the working tree nor the index, so
        it is dropped, since passing it to ``git add`` would fail the whole call on an
        unmatched pathspec. Empty ``paths`` is a no-op.

        Runs ``git`` directly rather than a sync script, deterministically like
        :meth:`divergence`. The caller holds it under :attr:`capture_lock`, so it never
        races another capture's stage or commit on the shared index.

        Args:
            paths: Vault-relative paths to stage.
            timeout: Seconds to allow the ``git add`` before it is killed.

        Raises:
            GitSyncError: when ``git add`` exits non-zero.
        """
        stageable = self._stageable(paths, timeout=timeout)
        if not stageable:
            return
        # Retried on `.git/index.lock` contention from a concurrent client (see
        # :func:`_run_with_lock_retry`); a non-lock failure raises below unchanged.
        completed = _run_with_lock_retry(
            lambda: self._exec(["git", "add", "--", *stageable], timeout=timeout)
        )
        if completed.returncode != 0:
            raise GitSyncError(
                f"git add failed (exit {completed.returncode}). "
                f"stderr: {completed.stderr.strip()!r}"
            )

    def _stageable(self, paths: Sequence[str], *, timeout: float) -> list[str]:
        """Keep only paths that exist in the working tree or are already tracked.

        A path that is neither, such as a never-committed hold removed within the same
        run, would make ``git add -- <path>`` abort on an unmatched pathspec, so it is
        dropped, carrying no git change anyway. Order is preserved. This assumes plain
        file paths, with no directory or glob pathspecs.
        """
        statuses = [(path, (self._vault_root / path).exists()) for path in paths]
        missing = [path for path, on_disk in statuses if not on_disk]
        tracked: set[str] = set()
        if missing:
            # One batched probe for every missing path. No ``--error-unmatch`` (it
            # aborts on the first unmatched pathspec); ``-z`` neutralises
            # ``core.quotePath`` so non-ASCII paths round-trip verbatim.
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
        """Run ``vault-commit <message> [-- <paths>]``: stage, commit, rebase, push.

        Given ``paths``, they are the EXACT set staged for this commit, through the
        script's ``git add -- <paths>``: a single capture's own pages, raw sidecar,
        assets and ``log.md``, never the whole tree, so the commit cannot sweep a
        concurrent capture's untracked asset and orphan an embedded ``![[asset]]``
        (issue #85). With ``paths`` as ``None`` the script commits whatever is already
        staged in the index, which the batch path stages incrementally through
        :meth:`stage`. A path may name a deletion, such as a superseded ``inbox/`` hold.

        The script prefixes the commit subject with ``agent:`` and never pushes
        ``--force``. A clean run with no staged changes returns ``committed=False`` and
        does **not** raise.

        Args:
            message: The commit subject, passed as the script's first argument.
            paths: The exact vault-relative paths to stage, or ``None`` to commit the
                already-staged index.
            timeout: Seconds to allow the script before it raises
                :class:`subprocess.TimeoutExpired`.

        Returns:
            The :class:`GitResult`, whose ``committed`` is ``False`` when nothing was
            staged, else ``True``.

        Raises:
            VaultConflictError: on a rebase-conflict exit, where stderr carries the
                ``VAULT CONFLICT`` line, the rebase has been aborted and the remote is
                unchanged.
            GitSyncError: on any other non-zero exit.
        """
        script_args = (message, "--", *paths) if paths is not None else (message,)
        return self._run_checked(
            VAULT_COMMIT_SCRIPT, script_args, timeout=timeout, classify_conflict=True
        )

    def divergence(self, *, timeout: float = 30.0) -> Divergence:
        """Count local vault commits ahead of the rebase tracking ref.

        Measured against the ``THOTH_GIT_REMOTE`` and ``THOTH_GIT_BRANCH`` tracking ref
        the wrappers rebase onto, defaulting to ``origin`` and ``main``, rather than
        ``THOTH_PUSH_REMOTE``, which may differ. Only the conflict path calls it, where
        ``vault-pull``'s ``pull --rebase`` has just refreshed that ref, so the count is
        accurate at alert time.

        Runs read-only ``git`` directly rather than a sync script: ``rev-list --count
        <remote>/<branch>..HEAD`` for the ahead-count, and the author time of the oldest
        commit in that range for ``since``. Any failure, whether no remote tracking ref,
        no git tree or a git error, is swallowed and reported as :class:`Divergence`
        ``(commits_ahead=-1, since=None)``, so a conflict handler can call this without
        raising anew.

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
        if ahead == 0:
            return Divergence(commits_ahead=0, since=None)
        # Author time (Unix seconds) of the OLDEST unpushed commit (the first that
        # diverged) -> the "unpushed since T" timestamp.
        oldest = self._git_text(
            ("log", "--reverse", "--format=%at", rng), timeout=timeout
        )
        since = _parse_first_epoch(oldest)
        return Divergence(commits_ahead=ahead, since=since)

    def _git_text(self, args: Sequence[str], *, timeout: float) -> str | None:
        """Run ``git <args>`` read-only in the vault, returning stdout or ``None``.

        Returns ``None`` on any non-zero exit or spawn failure, so a caller inside an
        exception handler never sees a new exception.
        """
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
        """Run one child process in the vault root with the forced environment.

        Every subprocess this class spawns goes through here, with ``check=False`` so a
        failure returns for the caller's own classification, text-mode captured output,
        and the ``PKM_VAULT``-forced child environment.

        Args:
            argv: The full argument vector, never with ``shell=True``.
            timeout: Seconds before :class:`subprocess.TimeoutExpired`.

        Returns:
            The :class:`subprocess.CompletedProcess` of the run.
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
        # Retried on `.git/index.lock` contention (see :func:`_run_with_lock_retry`):
        # the commit/pull scripts stage and rebase, so a concurrent client holding the
        # index lock would otherwise fail the whole capture; a non-lock non-zero exit is
        # returned unchanged for the caller's own conflict/error classification.
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
        """Run one sync script via :meth:`_run` and raise on a non-zero exit.

        Args:
            script: The script filename (e.g. :data:`VAULT_COMMIT_SCRIPT`).
            args: Positional arguments passed to the script.
            timeout: Seconds before :class:`subprocess.TimeoutExpired`.
            classify_conflict: When ``True``, a failure whose stderr carries the
                ``VAULT CONFLICT`` sentinel raises :class:`VaultConflictError`
                (only ``vault-commit`` emits it).

        Returns:
            The :class:`GitResult` of a clean (zero-exit) run.

        Raises:
            VaultConflictError: on a rebase-conflict exit when ``classify_conflict``
                is set.
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
        """Build a diagnostic message embedding the script's exit code and output."""
        return (
            f"{script} failed (exit {result.returncode}). "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )


def _parse_first_epoch(text: str | None) -> datetime | None:
    """Parse the first line of git ``%at`` output (Unix seconds) into an aware datetime.

    Returns ``None`` for empty or unparseable input, so a divergence probe never
    raises.
    """
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
