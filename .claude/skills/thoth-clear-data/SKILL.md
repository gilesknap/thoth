---
name: thoth-clear-data
description: >-
  How to reset a thoth deployment to a clean slate — wipe the Obsidian vault
  content (keeping .obsidian + .git), wipe the Hindsight vector bank, and
  reseed the spine — without breaking the git two-way sync. Use when starting
  fresh testing, clearing a throwaway vault, or recovering a corrupted index.
---

# Clearing thoth's data to a clean slate

thoth holds state in three places, and a real reset must clear the right ones in
the right order:

- **The vault** (`$PKM_VAULT`, an Obsidian folder) — the **canonical** store. A git
  repo that two-way-syncs with a remote; the capture daemon `git pull`s it on every
  capture and `git push`es its commits.
- **The Hindsight bank** (the vector/semantic index) — a **rebuildable projection**
  of the vault. Never the source of truth.
- **`$THOTH_HOME/state.db`** — disposable liveness/dedupe markers. Rarely needs
  clearing; deleting it just resets the heartbeat/dedupe memory.

The vault is canonical, so "clear the data" means: empty the vault content, then
rebuild the index from the (now empty) vault. The reverse order would just re-index
the old content.

## What to keep

- **`.obsidian/`** — the user's Obsidian config/plugins/workspace. Never delete it.
- **`.git/`** — the vault's history and remote wiring. Never delete it; wipe the
  *working tree content*, not the repo.

Everything else under the vault is content/spine and is recreated by `thoth init`.

## Procedure

Run as the user that owns the vault and the venv (e.g. `pkm`). Stop the capture
daemon first so it cannot write or push mid-wipe; **leave the Hindsight daemon
running** — `reindex --full-rebuild` needs it to wipe the bank.

```bash
# 0. Stop the capture daemon (needs root for the system unit).
systemctl stop thoth-slack            # Hindsight stays up

# 1. Get the vault to a known base = the remote head (auth: see "Private remote").
cd "$PKM_VAULT"
git fetch origin main
git reset --hard origin/main

# 2. Wipe the working-tree CONTENT, keeping .git and .obsidian.
find . -maxdepth 1 -mindepth 1 ! -name .git ! -name .obsidian -exec rm -rf {} +

# 3. Reseed the spine (SCHEMA.md, index.md, log.md, _bases/, folders).
#    --force overwrites existing spine files; on a wiped vault it just recreates them.
thoth init --force

# 4. Wipe + rebuild the Hindsight bank from the (now empty) vault.
#    --full-rebuild does `hindsight bank delete -y <bank>` then re-retains every live
#    page; on an empty vault that re-retains nothing -> a clean bank
#    (expect `live=0 full_rebuild=True aborted=False`).
thoth reindex --full-rebuild

# 5. Commit the clean slate and push, so the daemon's next pull does NOT restore the
#    old content from the remote (the wipe MUST reach the remote — see "Why push").
git add -A
git commit -m "chore(vault): clear to clean slate"
git push origin main

# 6. Restart the capture daemon (root).
systemctl start thoth-slack
```

`thoth init` / `thoth reindex` read config from `$THOTH_HOME/.env` (so `$THOTH_HOME`
must point at the deployment's home) and need `PKM_VAULT` set; mirror the systemd
unit's `Environment=`/`EnvironmentFile=` when running them by hand. `reindex` also
honours `THOTH_HINDSIGHT_BINARY` / `THOTH_HINDSIGHT_PROFILE` from that env, so the
right `hindsight` CLI/profile is used.

## Why push the wipe (the trap)

The vault two-way-syncs. If you wipe **only locally** and don't push, the capture
daemon's orient pass (`git pull`) merges the still-populated remote back in and the
"clear" silently undoes itself. So a real reset **must** push the empty vault. This
also means any other clone (e.g. the user's local Obsidian with the Git plugin) will
pull the wipe — that is the intended clean slate, but warn the user their local vault
content will disappear and they should re-sync/re-clone. A normal commit (not a
force-push) keeps it revertible.

## Why `reindex --full-rebuild`, not a manual index delete

The clean Hindsight wipe is `bank delete -y <bank>`, isolated in
`Reindexer.reset_bank` (`src/thoth/reindex_from_vault.py`) and only reachable via
`thoth reindex --full-rebuild`. `clear-observations` was rejected upstream — it
leaves raw memory units and entity nodes behind, so it is **not** a clean wipe. Let
`--full-rebuild` do the delete-then-rebuild; do not hand-delete index files.

## Code pointers

- `src/thoth/__main__.py` — the `init` (`--force`) and `reindex` (`--full-rebuild`)
  subcommands and their handlers.
- `src/thoth/reindex_from_vault.py` — `Reindexer.reset_bank()` (the bank wipe) and
  `run(full_rebuild=...)` (wipe-then-re-retain).
- The packaged spine `thoth init` writes is described in
  [[thoth-codebase-map]] (the vault page-type model); deployment/VPS specifics
  (host, keys, the vault PAT for the private remote) are operational and live outside
  this repo, not in this skill.
