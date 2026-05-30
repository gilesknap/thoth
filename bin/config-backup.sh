#!/usr/bin/env bash
# thoth/bin/config-backup.sh — push-only backup of the thoth app config repo.
#
# Carried forward from the SPEC (Appendix -> Backup/recovery): commits and pushes the
# THOTH_HOME working tree to the `thoth` config-backup repo every 6h (system cron).
# Secrets (.env) are NEVER committed — they are excluded by the repo .gitignore (the
# vault is untouched; it has its own per-ingest push via vault-commit). Most of what the
# framework script backed up (session/kanban DBs) ceases to exist in the thin app, so the
# DB-snapshot loop is dropped — the durable knowledge backup is the pkm-vault repo.
#
# Auth is gh's credential helper over HTTPS (never SSH, never a PAT-in-URL);
# GIT_CONFIG_GLOBAL=/dev/null neutralises any global insteadOf ssh-rewrite.
#
# Reconciliation for tests/CI ONLY: THOTH_CONFIG_PUSH_REMOTE defaults to the verbatim
# SPEC push URL and THOTH_GIT_BRANCH to "main", so production behaviour is byte-equivalent
# to the SPEC while a test can redirect the push at a LOCAL bare repo in tmp_path. The
# credential-helper line is a documented no-op for a local-path remote. Default GitHub
# owner: gilesknap.
set -euo pipefail

THOTH_HOME="${THOTH_HOME:-$HOME/.thoth}"
BRANCH="${THOTH_GIT_BRANCH:-main}"
PUSH_REMOTE="${THOTH_CONFIG_PUSH_REMOTE:-https://github.com/gilesknap/thoth.git}"
TS="$(date -u +%Y-%m-%dT%H:%MZ)"

cd "$THOTH_HOME"
git add -A
if git diff --cached --quiet; then
  echo "[$TS] no config changes to back up"
else
  git commit -m "backup $TS"
  GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
    push "$PUSH_REMOTE" "$BRANCH"
  echo "[$TS] config backup pushed"
fi
