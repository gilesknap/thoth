# Import existing files and folders (`thoth capture`)

`thoth capture <path>...` backfills content that already lives on disk — a single file or
a whole directory tree (an existing Obsidian vault, a folder of PDFs/images) — by walking
it and feeding each file through the same ingest pipeline a Slack capture uses. Pages are
filed under `source: import`.

Each `<path>` is a file or a directory. A directory is walked recursively in sorted
order. The walker always skips the `.obsidian/`, `.git/` and `_bases/` directories and
the spine files (`index.md`, `SCHEMA.md`, `log.md`), and skips any file whose extension
is not a known text / image / PDF / audio kind (so a stray binary never triggers a
surprise analyse call). Markdown/text files are filed as notes; images/PDFs/audio are
analysed and kept as assets.

## Running it on a deployed appliance

`thoth capture` pulls and pushes the vault git repo, so the shell you run it in needs the
same secrets the daemon uses — most importantly the vault remote's token. `load_config`
reads `~/.thoth/.env` into the configuration but never exports it into the process
environment, and the git sync inherits the real shell environment; the `thoth-slack`
service gets those values from systemd, but an interactive shell does not. So before a
manual import, source the env once:

```console
$ set -a; . ~/.thoth/.env; set +a
$ thoth capture ~/notes
```

Without this the run fails at the initial vault pull with `Authentication failed` for the
vault remote.

## Curate (default) vs as-is

```console
$ thoth capture ~/notes              # curate each file (the full value-add)
$ thoth capture ~/notes --as-is      # low-touch: route + file verbatim, skip curate
```

- **Default (curate):** every file runs the classify *and* curate LLM passes, so it is
  classified into the 4-folder model, given a `summary:`, wikilinked, and dedup-merged.
  Two LLM calls per file.
- **`--as-is`:** runs only the cheap classify call (for routing), then files the
  **original body verbatim** into the routed folder and indexes it — no curate call, no
  reshaping. Best for an already-clean Markdown vault you do not want re-authored. See
  [ADR 0010](../explanations/decisions/0010-capture-as-is-low-touch-import.md) for the
  exact semantics.

## Budget override

A bulk import is a real spend burst. `--budget N` overrides `THOTH_DAILY_LLM_BUDGET` for
**this run only** (it is never written back to the config):

```console
$ thoth capture ~/notes --budget 200   # cap this run at 200 combined LLM calls
$ thoth capture ~/notes --budget 0     # unlimited for this import (escape hatch)
```

`--budget 0` disables the cap for the run (the guard treats a non-positive limit as
disabled). With no flag, the configured daily budget applies unchanged.

## Trial runs and filtering

```console
$ thoth capture ~/notes --dry-run                  # list what would be filed; write nothing
$ thoth capture ~/notes --limit 5                  # process at most 5 files
$ thoth capture ~/notes --include '*.md'           # only Markdown (repeatable)
$ thoth capture ~/notes --exclude 'drafts/*'       # skip a subtree (repeatable)
```

`--dry-run` makes no LLM call, no vault pull, and no write — it only prints the planned
filings. `--include`/`--exclude` are `fnmatch` globs matched against each file's path
relative to the walk root; `--exclude` wins over `--include`.

## Commits and re-runs

The vault is pulled once up front and commits are **batched**: `--batch-size N` (default
25) commits+pushes every N files plus a final flush, instead of one commit per file.
Re-running over an unchanged tree is a true **no-op**. When a file's `raw/` source is
byte-identical to what's already on disk (the SHA-256 idempotency layer) **and** its
curated page already exists, the import short-circuits before the classify-routed
curate pass: nothing is re-spent against the budget and no page's `updated:` date is
bumped — the re-run reports those files as `unchanged`. So a re-run to finish an
import that tripped the daily budget cap (or that you Ctrl-C'd) costs nothing for the
parts already done and resumes only the rest. A Ctrl-C mid-run leaves the vault
uncommitted (but durable on disk); just re-run.
