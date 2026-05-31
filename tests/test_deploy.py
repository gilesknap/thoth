"""Tests for the Phase-3 deploy artifacts (SPEC section 4 rows, section 13, Appendix).

The shipped artifacts are the push-only ``bin/config-backup.sh`` (Appendix ->
Backup/recovery), the ``deploy/thoth-slack.service`` systemd unit (section 4 row,
Appendix step 6), and the ``deploy/crontab`` with the four scheduled jobs (section 2
diagram + Appendix cron block). These tests assert each exists, is syntactically valid,
and carries the load-bearing SPEC content, and they run ``config-backup.sh`` for real
against a LOCAL bare repo in ``tmp_path`` (no network, no real ``gh``) to prove the
commit/skip/push behaviour, mirroring the git-wrapper tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from thoth.git_sync import bin_dir

CONFIG_BACKUP_SCRIPT = "config-backup.sh"
HINDSIGHT_BACKUP_SCRIPT = "hindsight-backup.sh"
MAIN = "main"


def _repo_root() -> Path:
    """Return the repo root (the parent of the resolved ``bin/`` directory)."""
    return bin_dir().parent


def _deploy_dir() -> Path:
    """Return the ``deploy/`` directory holding the unit + crontab."""
    return _repo_root() / "deploy"


def _git(cwd: Path, *args: str) -> str:
    """Run git with global/system config neutralised; return stdout."""
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


# --- bin/config-backup.sh: presence + syntax --------------------------------------


def test_config_backup_script_exists_and_is_executable() -> None:
    """bin/config-backup.sh is shipped and has the executable bit set."""
    script = bin_dir() / CONFIG_BACKUP_SCRIPT
    assert script.is_file()
    assert os.access(script, os.X_OK)


def test_config_backup_script_has_valid_bash_syntax() -> None:
    """``bash -n`` parses the script without error."""
    script = bin_dir() / CONFIG_BACKUP_SCRIPT
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_config_backup_uses_gh_helper_and_nulls_global_config() -> None:
    """The push uses gh's credential helper over HTTPS and nulls global git config."""
    text = (bin_dir() / CONFIG_BACKUP_SCRIPT).read_text(encoding="utf-8")
    assert "GIT_CONFIG_GLOBAL=/dev/null" in text
    assert "credential.helper='!gh auth git-credential'" in text
    # Default push target is the thoth config repo, not the vault repo.
    assert "thoth.git" in text
    assert "pkm-vault.git" not in text


# --- bin/config-backup.sh: behaviour against a local bare repo --------------------


@dataclass(frozen=True)
class _ConfigRepo:
    """A local config-backup playground: a bare ``origin`` + a THOTH_HOME work clone."""

    bare: Path
    home: Path
    env: dict[str, str]


@pytest.fixture
def config_repo(tmp_path: Path) -> _ConfigRepo:
    """Build a bare ``origin`` and a THOTH_HOME clone wired for offline push."""
    bare = tmp_path / "thoth-config.git"
    _git(tmp_path, "init", "--bare", "-b", MAIN, str(bare))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.email", "tester@example.invalid")
    _git(seed, "config", "user.name", "thoth-test")
    (seed / "README.md").write_text("# thoth config\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed config repo")
    _git(seed, "push", "origin", MAIN)

    home = tmp_path / "home"
    _git(tmp_path, "clone", str(bare), str(home))
    _git(home, "config", "user.email", "tester@example.invalid")
    _git(home, "config", "user.name", "thoth-test")

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["THOTH_HOME"] = str(home)
    env["THOTH_GIT_BRANCH"] = MAIN
    env["THOTH_CONFIG_PUSH_REMOTE"] = str(bare)
    return _ConfigRepo(bare=bare, home=home, env=env)


def _run_backup(repo: _ConfigRepo) -> subprocess.CompletedProcess[str]:
    """Run the shipped config-backup.sh against the local playground."""
    script = bin_dir() / CONFIG_BACKUP_SCRIPT
    return subprocess.run(
        ["bash", str(script)],
        env=repo.env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_config_backup_commits_and_pushes_changes(config_repo: _ConfigRepo) -> None:
    """A new file in THOTH_HOME is committed and pushed to the config bare repo."""
    (config_repo.home / "config.toml").write_text("model = 'x'\n")
    result = _run_backup(config_repo)
    assert "config backup pushed" in result.stdout
    # The bare origin now carries the backup commit.
    subject = _git(config_repo.bare, "log", "-1", "--format=%s", MAIN).strip()
    assert subject.startswith("backup ")
    files = _git(config_repo.bare, "ls-tree", "--name-only", MAIN).split()
    assert "config.toml" in files


def test_config_backup_noop_when_no_changes(config_repo: _ConfigRepo) -> None:
    """With a clean tree the script reports no changes and pushes nothing."""
    before = _git(config_repo.bare, "rev-parse", MAIN).strip()
    result = _run_backup(config_repo)
    assert "no config changes" in result.stdout
    after = _git(config_repo.bare, "rev-parse", MAIN).strip()
    assert before == after


def test_config_backup_does_not_commit_gitignored_env(
    config_repo: _ConfigRepo,
) -> None:
    """A gitignored .env is never committed (secrets stay out of the repo)."""
    (config_repo.home / ".gitignore").write_text(".env\n")
    (config_repo.home / ".env").write_text("SLACK_BOT_TOKEN=test-token\n")
    _run_backup(config_repo)
    files = _git(config_repo.bare, "ls-tree", "-r", "--name-only", MAIN).split()
    assert ".env" not in files
    assert ".gitignore" in files


# --- bin/hindsight-backup.sh: presence + syntax + gating + behaviour --------------


def test_hindsight_backup_script_exists_and_is_executable() -> None:
    """bin/hindsight-backup.sh is shipped and has the executable bit set."""
    script = bin_dir() / HINDSIGHT_BACKUP_SCRIPT
    assert script.is_file()
    assert os.access(script, os.X_OK)


def test_hindsight_backup_script_has_valid_bash_syntax() -> None:
    """``bash -n`` parses the hindsight-backup script without error."""
    script = bin_dir() / HINDSIGHT_BACKUP_SCRIPT
    subprocess.run(["bash", "-n", str(script)], check=True)


def _run_hindsight_backup(
    home: Path, *, enabled: bool, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the shipped hindsight-backup.sh against a tmp THOTH_HOME (no Postgres)."""
    script = bin_dir() / HINDSIGHT_BACKUP_SCRIPT
    env = dict(os.environ)
    env["THOTH_HOME"] = str(home)
    if enabled:
        env["THOTH_HINDSIGHT_BACKUP"] = "1"
    else:
        env.pop("THOTH_HINDSIGHT_BACKUP", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_hindsight_backup_disabled_by_default_is_a_clean_noop(tmp_path: Path) -> None:
    """With THOTH_HINDSIGHT_BACKUP unset the script exits 0 and writes nothing."""
    home = tmp_path / "home"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "reindex-manifest.json").write_text("{}\n")
    result = _run_hindsight_backup(home, enabled=False)
    assert "disabled" in result.stdout
    # No backups dir created when disabled.
    assert not (home / "hindsight" / "backups").exists()


def test_hindsight_backup_enabled_copies_manifest_and_noops_without_pg_dump(
    tmp_path: Path,
) -> None:
    """Enabled but pg_dump absent: manifest copied, bank dump skipped (stays green)."""
    home = tmp_path / "home"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "reindex-manifest.json").write_text('{"page": 1}\n')
    # Point pg_dump at a binary that cannot exist so the guard takes the skip path.
    result = _run_hindsight_backup(
        home,
        enabled=True,
        extra_env={"THOTH_HINDSIGHT_PG_DUMP": "thoth-no-such-pg-dump-binary"},
    )
    assert "skipping bank dump" in result.stdout
    assert "manifest copied" in result.stdout
    backups = home / "hindsight" / "backups"
    manifests = list(backups.glob("reindex-manifest-*.json"))
    assert len(manifests) == 1
    # No bank dump landed (pg_dump absent).
    assert list(backups.glob("bank-*.sql.gz")) == []


def test_hindsight_backup_dumps_bank_and_prunes_generations(tmp_path: Path) -> None:
    """A fake pg_dump path produces a gzipped dump; pruning keeps N generations."""
    home = tmp_path / "home"
    backups = home / "hindsight" / "backups"
    backups.mkdir(parents=True)
    (home / "hindsight" / "reindex-manifest.json").write_text('{"page": 1}\n')

    # A fake pg_dump that emits >64 bytes of "SQL" so the dump is kept.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    pg_dump = fakebin / "pg_dump"
    pg_dump.write_text(
        "#!/usr/bin/env bash\n"
        'echo "-- fake dump $* --"\n'
        'for i in 1 2 3 4 5; do echo "INSERT INTO t VALUES ($i);"; done\n'
    )
    pg_dump.chmod(0o755)

    # Pre-seed 4 older generations of each artifact so the run prunes down to 2.
    for n in range(4):
        stamp = f"2026-05-2{n}T00-00Z"
        (backups / f"bank-{stamp}.sql.gz").write_bytes(b"x" * 128)
        (backups / f"reindex-manifest-{stamp}.json").write_text("{}\n")

    env = {
        "THOTH_HINDSIGHT_PG_DUMP": str(pg_dump),
        "THOTH_HINDSIGHT_BACKUP_GENERATIONS": "2",
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
    }
    result = _run_hindsight_backup(home, enabled=True, extra_env=env)
    assert "bank dumped" in result.stdout
    # Exactly the kept window survives for both artifact kinds.
    assert len(list(backups.glob("bank-*.sql.gz"))) == 2
    assert len(list(backups.glob("reindex-manifest-*.json"))) == 2
    # The newest bank dump is the one this run produced and is gzip (starts with 1f 8b).
    newest = max(backups.glob("bank-*.sql.gz"), key=lambda p: p.stat().st_mtime)
    assert newest.read_bytes()[:2] == b"\x1f\x8b"


def test_hindsight_backup_is_subordinate_to_full_rebuild_in_docstring() -> None:
    """The script documents the index stays disposable (SPEC section 10 unchanged)."""
    text = (bin_dir() / HINDSIGHT_BACKUP_SCRIPT).read_text(encoding="utf-8")
    assert "disposable" in text.lower()
    assert "--full-rebuild" in text
    # Logical dump, not a data-dir copy.
    assert "pg_dump" in text


def test_crontab_chains_hindsight_backup_after_successful_reindex() -> None:
    """The reindex cron line chains the (gated) hindsight snapshot on success."""
    text = (_deploy_dir() / "crontab").read_text(encoding="utf-8")
    reindex_lines = [
        line
        for line in text.splitlines()
        if "thoth reindex" in line and not line.lstrip().startswith("#")
    ]
    assert len(reindex_lines) == 1
    line = reindex_lines[0]
    # `&&` so the snapshot only runs when reindex exits 0.
    assert "&&" in line
    assert HINDSIGHT_BACKUP_SCRIPT in line


# --- deploy/thoth-slack.service ---------------------------------------------------


def test_systemd_unit_exists_and_runs_thoth_slack() -> None:
    """The unit is shipped, runs ``thoth slack``, and sets the resolution env vars."""
    unit = _deploy_dir() / "thoth-slack.service"
    assert unit.is_file()
    text = unit.read_text(encoding="utf-8")
    assert "ExecStart=" in text and "thoth slack" in text
    assert "Environment=PKM_VAULT=/opt/pkm-vault" in text
    assert "Environment=OBSIDIAN_VAULT_NAME=pkm-vault" in text
    # Secrets come from the chmod-600 .env, never inlined in the tracked unit.
    assert "EnvironmentFile=" in text
    assert "SLACK_BOT_TOKEN=" not in text
    # Unprivileged service account (SPEC section 12).
    assert "User=" in text
    assert "User=root" not in text


def test_systemd_unit_name_discrepancy_is_resolved_to_thoth_slack() -> None:
    """The unit ships as thoth-slack.service (the resolved name), not pkm-slack."""
    assert (_deploy_dir() / "thoth-slack.service").is_file()
    assert not (_deploy_dir() / "pkm-slack.service").exists()


def test_systemd_unit_restart_is_rate_limited(tmp_path: Path) -> None:
    """The unit restarts on failure but bounds flapping (StartLimit*, issue #15).

    Acceptance: repeated restart failure is bounded and reportable. The directives are
    parsed with a real config parser (systemd unit syntax is INI-like) rather than a
    substring scan, so a malformed key is caught.
    """
    import configparser

    text = (_deploy_dir() / "thoth-slack.service").read_text(encoding="utf-8")
    parser = configparser.ConfigParser(strict=False)
    parser.read_string(text)
    # Restart pacing lives in [Service]; the burst limit lives in [Unit] (systemd moved
    # the StartLimit* keys there).
    assert parser.get("Service", "Restart") == "on-failure"
    assert int(parser.get("Service", "RestartSec")) >= 1
    assert int(parser.get("Unit", "StartLimitBurst")) >= 1
    assert int(parser.get("Unit", "StartLimitIntervalSec")) >= 1


def test_systemd_unit_documents_how_flapping_surfaces() -> None:
    """The unit documents the backstops (errors-to-Slack + heartbeat) for flapping."""
    text = (_deploy_dir() / "thoth-slack.service").read_text(encoding="utf-8")
    lowered = text.lower()
    # The flapping path surfaces via the alert channel and/or the stale heartbeat.
    assert "slack_alert_channel" in lowered
    assert "heartbeat" in lowered
    # The optional hard backstop (an OnFailure= dead-man's switch) is named.
    assert "onfailure" in lowered


# --- deploy/thoth-hindsight.service -----------------------------------------------


def test_hindsight_unit_is_oneshot_remain_after_exit_and_unprivileged() -> None:
    """The hindsight daemon ships as a oneshot+RemainAfterExit unit run as pkm.

    The `hindsight-embed daemon` CLI has no foreground mode and double-forks, so the
    unit starts/stops the self-detaching daemon rather than supervising it (Type=oneshot
    + RemainAfterExit=yes). It must run unprivileged (embedded Postgres initdb refuses
    to run as root).
    """
    import configparser

    unit = _deploy_dir() / "thoth-hindsight.service"
    assert unit.is_file()
    text = unit.read_text(encoding="utf-8")
    parser = configparser.ConfigParser(strict=False)
    parser.read_string(text)
    assert parser.get("Service", "Type") == "oneshot"
    assert parser.getboolean("Service", "RemainAfterExit") is True
    assert parser.get("Service", "User") == "pkm"
    assert "daemon start" in parser.get("Service", "ExecStart")
    assert "daemon stop" in parser.get("Service", "ExecStop")
    # No secret is inlined: the Gemini key lives in the per-profile ~/.hindsight env.
    assert "API_KEY" not in text


def test_slack_unit_orders_after_and_wants_hindsight() -> None:
    """The Slack daemon depends on the hindsight unit (talks to it, never spawns it)."""
    text = (_deploy_dir() / "thoth-slack.service").read_text(encoding="utf-8")
    assert "After=thoth-hindsight.service" in text
    assert "Wants=thoth-hindsight.service" in text


def test_slack_unit_grants_rw_to_hindsight_state_dirs() -> None:
    """ProtectHome=read-only punches through ~/.hindsight + ~/.pg0 for the CLI calls."""
    text = (_deploy_dir() / "thoth-slack.service").read_text(encoding="utf-8")
    assert "ProtectHome=read-only" in text
    rw_line = next(
        line for line in text.splitlines() if line.startswith("ReadWritePaths=")
    )
    for path in (
        "/opt/pkm-vault",
        "/home/pkm/.thoth",
        "/home/pkm/.hindsight",
        "/home/pkm/.pg0",
    ):
        assert path in rw_line


# --- deploy/crontab ---------------------------------------------------------------


def test_crontab_has_the_four_scheduled_jobs() -> None:
    """The crontab schedules reindex, daily + weekly summary, and config-backup."""
    text = (_deploy_dir() / "crontab").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    schedule = "\n".join(lines)
    # 06:30 reindex.
    assert "30 6 * * *" in schedule and "thoth reindex" in schedule
    # 07:00 daily summary.
    assert "0 7 * * *" in schedule and "thoth summary daily" in schedule
    # Mon 07:00 weekly summary (dow 1 = Monday).
    assert "0 7 * * 1" in schedule and "thoth summary weekly" in schedule
    # Every 6h config-backup.
    assert "0 */6 * * *" in schedule and CONFIG_BACKUP_SCRIPT in schedule
    # Europe/London so the digests fire at the local 07:00 (SPEC section 9).
    assert "CRON_TZ=Europe/London" in schedule


def test_crontab_invokes_only_real_cli_subcommands() -> None:
    """Every active `thoth <cmd>` cron line names a subcommand the CLI actually has."""
    text = (_deploy_dir() / "crontab").read_text(encoding="utf-8")
    real = {"slack", "mcp", "reindex", "summary"}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "thoth " not in stripped:
            continue
        # Find the token right after the (bare) `thoth` invocation.
        parts = stripped.split()
        idx = parts.index("thoth")
        assert parts[idx + 1] in real, f"unknown subcommand in cron line: {stripped}"


# --- sanity: real bash + git are available in this environment --------------------


def test_environment_has_bash_and_git() -> None:
    """The deploy-script tests need real bash + git on PATH."""
    assert shutil.which("bash") is not None
    assert shutil.which("git") is not None
