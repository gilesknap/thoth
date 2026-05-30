"""Tests for :mod:`thoth.git_sync`.

These run REAL ``git`` against a LOCAL bare repo created in ``tmp_path`` — no
network, no GitHub, no ``gh`` credential helper for the local-path remote. The
shipped bash scripts honour ``THOTH_PUSH_REMOTE`` / ``THOTH_GIT_REMOTE`` /
``THOTH_GIT_BRANCH`` overrides (defaulting to the verbatim SPEC values), so the
tests redirect pull and push at the bare repo while production stays byte-equal
to the SPEC. A poisoned credential helper proves the gh helper is never invoked.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from thoth.config import Config, load_config
from thoth.git_sync import (
    VAULT_COMMIT_SCRIPT,
    VAULT_PULL_SCRIPT,
    GitResult,
    GitSync,
    GitSyncError,
    VaultConflictError,
    _resolve_bin_dir,
    bin_dir,
)

# A repo-local credential helper that fails loudly if git ever tries to use it.
# For a local-path remote git needs no credentials, so this must never run; if it
# does, the push exits non-zero and prints the marker (assertable in tests).
POISON_MARKER = "POISONED-CREDENTIAL-HELPER-CALLED"
POISON_HELPER = f"!echo {POISON_MARKER} >&2; exit 17"

# Branch the fixtures build on; matches the scripts' default and the SPEC.
MAIN = "main"


def _git(cwd: Path, *args: str) -> str:
    """Run a git command with global/system config neutralised; return stdout.

    ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` are nulled so the user's global
    ``insteadOf`` ssh-rewrite (and any ambient identity) cannot affect the test
    repos. Commits rely on repo-local identity set by the fixture.
    """
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


def _set_identity(repo: Path) -> None:
    """Configure a repo-local committer identity so commits work without globals."""
    _git(repo, "config", "user.email", "tester@example.invalid")
    _git(repo, "config", "user.name", "thoth-test")


@dataclass(frozen=True)
class GitVault:
    """A local git playground for one test.

    Attributes:
        bare: Path to the bare repo acting as ``origin``.
        work: Path to the appliance's working clone (the vault root).
        other: Path to a second clone simulating the Obsidian workstation.
        config: A :class:`Config` whose ``vault_path`` is ``work``.
        env: The base child environment redirecting pull+push at ``bare``.
        sync: A :class:`GitSync` wired to ``config`` and ``env``.
    """

    bare: Path
    work: Path
    other: Path
    config: Config
    env: dict[str, str]
    sync: GitSync

    def origin_head(self) -> str:
        """Return the bare repo's current HEAD commit subject."""
        return _git(self.bare, "log", "-1", "--format=%s", MAIN).strip()

    def origin_sha(self) -> str:
        """Return the bare repo's current HEAD sha."""
        return _git(self.bare, "rev-parse", MAIN).strip()

    def push_from_other(self, filename: str, content: str, message: str) -> None:
        """Commit a file in the ``other`` clone and push it to ``origin``."""
        _git(self.other, "pull", "origin", MAIN)
        (self.other / filename).write_text(content)
        _git(self.other, "add", "-A")
        _git(self.other, "commit", "-m", message)
        _git(self.other, "push", "origin", MAIN)


@pytest.fixture
def git_vault(tmp_path: Path) -> GitVault:
    """Build a bare ``origin`` + work clone + second clone, seeded on ``main``.

    The child env points ``THOTH_GIT_REMOTE``/``THOTH_GIT_BRANCH`` at ``origin``/
    ``main`` and ``THOTH_PUSH_REMOTE`` at the bare repo path, so the scripts run
    fully offline. ``PKM_VAULT`` is set by :class:`GitSync` from the config.
    """
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", "-b", MAIN, str(bare))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _set_identity(seed)
    (seed / "index.md").write_text("# index\n")
    (seed / "log.md").write_text("# Vault Log\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init vault spine")
    _git(seed, "push", "origin", MAIN)

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _set_identity(work)

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    _set_identity(other)

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["THOTH_GIT_REMOTE"] = "origin"
    env["THOTH_GIT_BRANCH"] = MAIN
    env["THOTH_PUSH_REMOTE"] = str(bare)
    # Ensure no ambient override leaks in; PKM_VAULT is forced by GitSync anyway.
    env.pop("PKM_VAULT", None)

    config = load_config({"PKM_VAULT": str(work)})
    sync = GitSync(config, env=env)
    return GitVault(
        bare=bare, work=work, other=other, config=config, env=env, sync=sync
    )


# --------------------------------------------------------------------------- #
# Shipped bash scripts: existence, syntax, and SPEC-verbatim guards.
# --------------------------------------------------------------------------- #


def test_bin_dir_contains_both_scripts() -> None:
    """bin_dir() resolves to a directory holding both shipped scripts."""
    directory = bin_dir()
    assert directory.is_dir()
    assert (directory / VAULT_PULL_SCRIPT).is_file()
    assert (directory / VAULT_COMMIT_SCRIPT).is_file()


def test_script_filenames_match_spec() -> None:
    """The script-name constants are the SPEC filenames."""
    assert VAULT_PULL_SCRIPT == "vault-pull"
    assert VAULT_COMMIT_SCRIPT == "vault-commit"


def test_resolve_bin_dir_finds_scripts_in_an_ancestor(tmp_path: Path) -> None:
    """_resolve_bin_dir returns the nearest ancestor/bin holding vault-pull."""
    bin_here = tmp_path / "repo" / "bin"
    bin_here.mkdir(parents=True)
    (bin_here / VAULT_PULL_SCRIPT).write_text("#!/usr/bin/env bash\n")
    module = tmp_path / "repo" / "src" / "thoth" / "git_sync.py"
    module.parent.mkdir(parents=True)
    assert _resolve_bin_dir(module) == bin_here


def test_resolve_bin_dir_fallback_when_no_scripts(tmp_path: Path) -> None:
    """With no vault-pull anywhere above, _resolve_bin_dir falls back to a guess.

    Mirrors the ``src/thoth/git_sync.py`` layout: the guess is ``parents[2]/bin``
    (the repo root), even though nothing exists there yet.
    """
    module = tmp_path / "src" / "thoth" / "git_sync.py"
    module.parent.mkdir(parents=True)
    # No bin/ directory created anywhere; the fallback path is parents[2]/bin.
    assert _resolve_bin_dir(module) == tmp_path / "bin"


@pytest.mark.parametrize("script", [VAULT_PULL_SCRIPT, VAULT_COMMIT_SCRIPT])
def test_scripts_pass_bash_syntax_check(script: str) -> None:
    """`bash -n` accepts each script (syntax check, no shellcheck needed)."""
    path = bin_dir() / script
    completed = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("script", [VAULT_PULL_SCRIPT, VAULT_COMMIT_SCRIPT])
def test_scripts_contain_spec_verbatim_invariants(script: str) -> None:
    """Each script carries the load-bearing SPEC strings and never --force."""
    text = (bin_dir() / script).read_text()
    assert "GIT_CONFIG_GLOBAL=/dev/null" in text
    assert "credential.helper=" in text
    # No force-push in any form (the dangerous flag spellings and the bare token).
    assert "--force" not in text
    assert "push -f" not in text
    assert "--force-with-lease" not in text


def test_commit_script_defaults_push_to_origin_not_a_hardcoded_owner() -> None:
    """vault-commit pushes to the vault's own remote, with no hardcoded owner URL.

    Issue #4: the individual-centric default push URL is gone. The push target defaults
    to THOTH_GIT_REMOTE / origin (the place the rebase pulled from), so no repository
    owner is baked into the script.
    """
    text = (bin_dir() / VAULT_COMMIT_SCRIPT).read_text()
    assert "gilesknap" not in text
    assert "pkm-vault.git" not in text
    # The push target defaults to the vault's own remote, not a separate hardcoded URL.
    assert 'PUSH_REMOTE="${THOTH_PUSH_REMOTE:-$REMOTE}"' in text
    assert 'push "$PUSH_REMOTE" "$BRANCH"' in text


def test_commit_script_prefixes_agent_and_handles_empty() -> None:
    """vault-commit prefixes the subject with 'agent:' and short-circuits empty."""
    text = (bin_dir() / VAULT_COMMIT_SCRIPT).read_text()
    assert 'commit -m "agent: $MSG"' in text
    assert "nothing to commit" in text


# --------------------------------------------------------------------------- #
# GitSync wiring: env forcing, vault root, bin path.
# --------------------------------------------------------------------------- #


def test_vault_root_and_bin_path_properties(git_vault: GitVault) -> None:
    """The wrapper exposes the config vault root and a real bin path."""
    assert git_vault.sync.vault_root == git_vault.config.vault_path
    assert (git_vault.sync.bin_path / VAULT_COMMIT_SCRIPT).is_file()


def test_pkm_vault_forced_to_config_even_with_ambient_env(tmp_path: Path) -> None:
    """PKM_VAULT in the child env is the config path, not an ambient value.

    We point a different vault at the config but seed the ambient env with a bogus
    PKM_VAULT; the constructed child env must carry the config path.
    """
    work = tmp_path / "vault"
    work.mkdir()
    config = load_config({"PKM_VAULT": str(work)})
    ambient = {"PKM_VAULT": "/somewhere/else", "PATH": os.environ["PATH"]}
    sync = GitSync(config, env=ambient)
    # The forced child env carries the config path, not the ambient PKM_VAULT.
    assert sync._child_env["PKM_VAULT"] == str(config.vault_path)


def test_custom_bin_path_is_honoured(git_vault: GitVault, tmp_path: Path) -> None:
    """An explicit bin_path overrides the auto-resolved bin_dir()."""
    custom = tmp_path / "mybin"
    custom.mkdir()
    sync = GitSync(git_vault.config, env=git_vault.env, bin_path=custom)
    assert sync.bin_path == custom


# --------------------------------------------------------------------------- #
# pull()
# --------------------------------------------------------------------------- #


def test_pull_brings_in_remote_commit(git_vault: GitVault) -> None:
    """A commit pushed to origin by the other clone is pulled into the work vault."""
    git_vault.push_from_other("from-other.md", "hello from obsidian\n", "obsidian edit")
    assert not (git_vault.work / "from-other.md").exists()

    result = git_vault.sync.pull()

    assert isinstance(result, GitResult)
    assert result.returncode == 0
    assert (git_vault.work / "from-other.md").read_text() == "hello from obsidian\n"


def test_pull_clean_when_up_to_date(git_vault: GitVault) -> None:
    """pull() on an up-to-date vault returns cleanly (returncode 0)."""
    result = git_vault.sync.pull()
    assert result.returncode == 0


def test_pull_raises_gitsyncerror_on_missing_remote(git_vault: GitVault) -> None:
    """A nonexistent local remote path makes pull() raise GitSyncError with output."""
    bad_env = dict(git_vault.env)
    bad_env["THOTH_GIT_REMOTE"] = str(git_vault.work / "does-not-exist.git")
    sync = GitSync(git_vault.config, env=bad_env)
    with pytest.raises(GitSyncError) as exc_info:
        sync.pull()
    # The message surfaces captured output for debugging.
    assert "vault-pull" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# commit()
# --------------------------------------------------------------------------- #


def test_commit_happy_path_pushes_and_advances_origin(git_vault: GitVault) -> None:
    """Writing a file then commit() stages+commits+pushes; origin HEAD advances."""
    before = git_vault.origin_sha()
    (git_vault.work / "entities" / "foo.md").parent.mkdir(parents=True, exist_ok=True)
    (git_vault.work / "entities" / "foo.md").write_text("# Foo\n")

    result = git_vault.sync.commit("add foo")

    assert result.returncode == 0
    assert result.committed is True
    assert git_vault.origin_sha() != before
    # The commit subject is prefixed 'agent:' by the script.
    assert git_vault.origin_head() == "agent: add foo"


def test_commit_no_changes_returns_not_committed(git_vault: GitVault) -> None:
    """commit() with nothing staged returns committed=False and does not raise."""
    before = git_vault.origin_sha()
    result = git_vault.sync.commit("nothing here")
    assert result.returncode == 0
    assert result.committed is False
    assert "nothing to commit" in result.stdout
    # Origin is untouched.
    assert git_vault.origin_sha() == before


def test_commit_conflict_raises_and_leaves_clean_tree(git_vault: GitVault) -> None:
    """A same-line conflict raises VaultConflictError; rebase aborted, origin intact."""
    # Origin (via the other clone) writes a file...
    git_vault.push_from_other("clash.md", "OBSIDIAN-LINE\n", "obsidian writes clash")
    origin_after_other = git_vault.origin_sha()

    # ...and the appliance writes the SAME path with different content, without
    # pulling first, so the rebase collides.
    (git_vault.work / "clash.md").write_text("APPLIANCE-LINE\n")

    with pytest.raises(VaultConflictError) as exc_info:
        git_vault.sync.commit("appliance writes clash")

    # The conflict sentinel is surfaced for the Slack report.
    assert "VAULT CONFLICT" in str(exc_info.value)
    # Rebase was aborted: no rebase state dir, working tree clean.
    assert not (git_vault.work / ".git" / "rebase-merge").exists()
    assert not (git_vault.work / ".git" / "rebase-apply").exists()
    status = _git(git_vault.work, "status", "--porcelain")
    assert status.strip() == ""
    # Never clobbered: origin HEAD is still the other clone's commit.
    assert git_vault.origin_sha() == origin_after_other


def test_commit_conflict_is_subclass_of_gitsyncerror(git_vault: GitVault) -> None:
    """VaultConflictError is a GitSyncError so broad except clauses still catch it."""
    git_vault.push_from_other("clash.md", "A\n", "obsidian")
    (git_vault.work / "clash.md").write_text("B\n")
    with pytest.raises(GitSyncError):
        git_vault.sync.commit("appliance")


def test_commit_generic_failure_is_not_conflict(git_vault: GitVault) -> None:
    """A non-conflict failure (bad push remote) raises plain GitSyncError.

    The commit and rebase succeed, but the push targets a nonexistent local path,
    so the script exits non-zero AFTER the conflict check — WITHOUT the VAULT
    CONFLICT sentinel. (Note: a *pull/rebase* failure of any kind, including an
    unreachable rebase remote, is reported by the script as a conflict by design;
    the push step is where generic failures surface.)
    """
    bad_env = dict(git_vault.env)
    bad_env["THOTH_PUSH_REMOTE"] = str(git_vault.work / "nope.git")
    sync = GitSync(git_vault.config, env=bad_env)
    (git_vault.work / "note.md").write_text("content\n")

    with pytest.raises(GitSyncError) as exc_info:
        sync.commit("will fail at push")
    assert not isinstance(exc_info.value, VaultConflictError)
    assert "VAULT CONFLICT" not in str(exc_info.value)
    assert "vault-commit" in str(exc_info.value)


def test_commit_pushes_to_origin_when_push_remote_unset(git_vault: GitVault) -> None:
    """With THOTH_PUSH_REMOTE unset, commit() pushes to the vault's own origin (#4).

    No owner URL is hardcoded: the work clone's ``origin`` already points at the bare
    repo, so dropping the explicit push override must still advance origin HEAD.
    """
    env = dict(git_vault.env)
    env.pop("THOTH_PUSH_REMOTE", None)  # fall back to the vault's own remote (origin)
    sync = GitSync(git_vault.config, env=env)
    before = git_vault.origin_sha()
    (git_vault.work / "via-origin.md").write_text("filed via origin\n")

    result = sync.commit("file via origin")

    assert result.committed is True
    assert git_vault.origin_sha() != before
    assert git_vault.origin_head() == "agent: file via origin"


def test_commit_fails_loudly_when_no_origin_and_push_remote_unset(
    tmp_path: Path,
) -> None:
    """commit() refuses to push when origin and THOTH_PUSH_REMOTE are both unset (#4).

    A fresh repo with no ``origin`` remote and no push override must fail loudly (so the
    appliance can never silently target someone else's repo) rather than guess a target.
    The failure happens before any commit, so nothing is committed.
    """
    work = tmp_path / "no-origin"
    work.mkdir()
    _git(work, "init", "-b", MAIN, ".")
    _set_identity(work)
    (work / "note.md").write_text("orphan vault\n")

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("THOTH_PUSH_REMOTE", None)
    env.pop("THOTH_GIT_REMOTE", None)  # default 'origin', which is not configured
    env.pop("PKM_VAULT", None)

    config = load_config({"PKM_VAULT": str(work)})
    sync = GitSync(config, env=env)
    with pytest.raises(GitSyncError) as exc_info:
        sync.commit("should refuse")
    assert "VAULT PUSH ERROR" in str(exc_info.value)
    # Nothing was committed (the guard fires before `git add`/`git commit`): the fresh
    # repo still has no commits, so HEAD does not resolve.
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode != 0, "guard must fire before any commit is made"


def test_commit_pull_then_commit_round_trip(git_vault: GitVault) -> None:
    """Disjoint files from both sides merge: pull, write, commit, origin has both."""
    git_vault.push_from_other("a.md", "alpha\n", "obsidian adds a")
    git_vault.sync.pull()
    (git_vault.work / "b.md").write_text("beta\n")
    result = git_vault.sync.commit("appliance adds b")
    assert result.committed is True
    # Fresh verification clone sees both files.
    verify = git_vault.work.parent / "verify"
    _git(git_vault.work.parent, "clone", str(git_vault.bare), str(verify))
    assert (verify / "a.md").read_text() == "alpha\n"
    assert (verify / "b.md").read_text() == "beta\n"


# --------------------------------------------------------------------------- #
# Offline / no-network guarantees.
# --------------------------------------------------------------------------- #


def test_credential_helper_never_invoked_for_local_remote(git_vault: GitVault) -> None:
    """A poisoned credential helper is never called for the local-path remote.

    We install a repo-local credential.helper that prints a marker and exits
    non-zero. Because the remote is a local path, git needs no credentials, so the
    push must still succeed and the marker must never appear in captured output.
    """
    _git(git_vault.work, "config", "credential.helper", POISON_HELPER)
    (git_vault.work / "safe.md").write_text("safe\n")

    result = git_vault.sync.commit("push without credentials")

    assert result.committed is True
    assert result.returncode == 0
    assert POISON_MARKER not in result.stderr
    assert POISON_MARKER not in result.stdout


def test_run_operates_on_vault_root_not_pytest_cwd(git_vault: GitVault) -> None:
    """The wrapper acts on the config vault root, independent of pytest's cwd.

    pytest runs from the repo root (a different git tree); a commit must still
    stage and push the work vault's file because :meth:`GitSync._run` passes
    ``cwd=vault_root`` and the script ``cd "$VAULT"``s. Proven by a fresh clone
    of origin containing the file.
    """
    assert Path.cwd() != git_vault.work
    (git_vault.work / "cwd-proof.md").write_text("x\n")
    result = git_vault.sync.commit("cwd proof")
    assert result.committed is True
    verify = git_vault.work.parent / "verify-cwd"
    _git(git_vault.work.parent, "clone", str(git_vault.bare), str(verify))
    assert (verify / "cwd-proof.md").exists()
