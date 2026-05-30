#!/usr/bin/env bash
# thoth/bin/hindsight-backup.sh — OPTIONAL, config-gated, best-effort fast-restore
# snapshot of the Hindsight semantic-index bank, taken after a SUCCESSFUL nightly
# reindex (chained `thoth reindex && hindsight-backup.sh` in deploy/crontab).
#
# SPEC section 10 is unchanged: the index is DISPOSABLE — `pkm-vault` is the durable
# knowledge backup and `reindex --full-rebuild` deterministically re-derives the bank
# from the canonical vault. This snapshot is *subordinate* to that full rebuild: it only
# buys a faster cold-start restore than a from-scratch re-embed, and a missing/empty
# snapshot is never an error (the rebuild path always works).
#
# What it captures (logical, NOT a Postgres data-dir copy):
#   1. a logical `pg_dump` of the Hindsight bank's Postgres database, and
#   2. a copy of the reindex manifest (~/.thoth/hindsight/reindex-manifest.json).
# It keeps ~3 generations and prunes older ones.
#
# Everything pg-specific is VPS-time. The local_embedded daemon's exact socket / DSN is
# not known here, so the dump command is fully configurable and GUARDED: if pg_dump is
# not on PATH, or backups are not enabled, or the dump fails/produces nothing, the script
# logs and exits 0 — so CI and a dev box (no Postgres, no daemon) stay green.
#
# Enable on the appliance by setting THOTH_HINDSIGHT_BACKUP=1 (and, if needed, the
# pg connection vars below) in the cron environment / .env. Disabled by default.
set -euo pipefail

THOTH_HOME="${THOTH_HOME:-$HOME/.thoth}"
TS="$(date -u +%Y-%m-%dT%H-%MZ)"

# --- gate: opt-in only --------------------------------------------------------------
# Default OFF. Any value other than 1/true/yes (case-insensitive) is treated as off.
ENABLED="${THOTH_HINDSIGHT_BACKUP:-0}"
case "${ENABLED,,}" in
  1 | true | yes) ;;
  *)
    echo "[$TS] hindsight-backup: disabled (set THOTH_HINDSIGHT_BACKUP=1 to enable); skipping"
    exit 0
    ;;
esac

# --- where snapshots live -----------------------------------------------------------
BACKUP_DIR="${THOTH_HINDSIGHT_BACKUP_DIR:-$THOTH_HOME/hindsight/backups}"
MANIFEST_SRC="${THOTH_HINDSIGHT_MANIFEST:-$THOTH_HOME/hindsight/reindex-manifest.json}"
# How many timestamped generations to retain (older are pruned).
GENERATIONS="${THOTH_HINDSIGHT_BACKUP_GENERATIONS:-3}"

# --- pg_dump configuration (all VPS-time, all overridable) --------------------------
# The bank's Postgres database name and the pg_dump connection. For the local_embedded
# daemon these point at its unix socket / DSN on the VPS; left at neutral defaults here
# so the guard below no-ops cleanly where Postgres is absent.
PG_DUMP_BIN="${THOTH_HINDSIGHT_PG_DUMP:-pg_dump}"
PG_DATABASE="${THOTH_HINDSIGHT_PG_DATABASE:-hindsight}"
# Optional explicit DSN (e.g. postgresql:///hindsight?host=/run/hindsight). When set it
# is passed as the sole pg_dump connection argument; otherwise libpq env vars (PGHOST,
# PGPORT, PGUSER, ...) from the environment apply.
PG_DSN="${THOTH_HINDSIGHT_PG_DSN:-}"

mkdir -p "$BACKUP_DIR"

# --- 1) logical pg_dump (guarded) ---------------------------------------------------
dump_file="$BACKUP_DIR/bank-$TS.sql.gz"
if ! command -v "$PG_DUMP_BIN" >/dev/null 2>&1; then
  echo "[$TS] hindsight-backup: '$PG_DUMP_BIN' not found; skipping bank dump (index is rebuildable)"
elif ! command -v gzip >/dev/null 2>&1; then
  echo "[$TS] hindsight-backup: gzip not found; skipping bank dump"
else
  dump_args=()
  if [ -n "$PG_DSN" ]; then
    dump_args+=("$PG_DSN")
  else
    dump_args+=(--dbname "$PG_DATABASE")
  fi
  # Best-effort: a failed dump (daemon down, db absent, auth) must not fail the script.
  if "$PG_DUMP_BIN" --no-owner --no-privileges "${dump_args[@]}" 2>/dev/null | gzip >"$dump_file"; then
    # Drop an empty/near-empty dump (pipefail-suppressed failures can leave a stub).
    if [ -s "$dump_file" ] && [ "$(wc -c <"$dump_file")" -gt 64 ]; then
      echo "[$TS] hindsight-backup: bank dumped -> $dump_file"
    else
      rm -f "$dump_file"
      echo "[$TS] hindsight-backup: pg_dump produced no usable output; skipped (index is rebuildable)"
    fi
  else
    rm -f "$dump_file"
    echo "[$TS] hindsight-backup: pg_dump failed; skipped (index is rebuildable)"
  fi
fi

# --- 2) copy the reindex manifest ---------------------------------------------------
if [ -f "$MANIFEST_SRC" ]; then
  cp "$MANIFEST_SRC" "$BACKUP_DIR/reindex-manifest-$TS.json"
  echo "[$TS] hindsight-backup: manifest copied"
else
  echo "[$TS] hindsight-backup: no manifest at $MANIFEST_SRC; skipping manifest copy"
fi

# --- 3) prune older generations -----------------------------------------------------
# Keep the newest $GENERATIONS of each artifact kind; delete the rest. `ls -1t` orders
# newest-first; `tail -n +N` selects everything past the keep window.
prune() {
  local glob="$1"
  local keep="$2"
  # Nullglob-safe: feed the listing through ls and guard the empty case.
  local files
  files="$(ls -1t $glob 2>/dev/null || true)"
  if [ -n "$files" ]; then
    echo "$files" | tail -n "+$((keep + 1))" | while IFS= read -r old; do
      [ -n "$old" ] && rm -f "$old"
    done
  fi
}
prune "$BACKUP_DIR/bank-*.sql.gz" "$GENERATIONS"
prune "$BACKUP_DIR/reindex-manifest-*.json" "$GENERATIONS"

echo "[$TS] hindsight-backup: done (retaining $GENERATIONS generations in $BACKUP_DIR)"
