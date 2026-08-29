# Import existing files and folders (`thoth capture`)

`thoth capture <path>...` backfills content that already lives on disk, be it a single file or a whole directory tree such as an existing Obsidian vault or a folder of PDFs and images.

It walks the tree and feeds each file through the same ingest pipeline a Slack capture uses, and pages are filed under `source: import`.

Each `<path>` is a file or a directory, and a directory is walked recursively in sorted order.

The walker always skips the `.obsidian/`, `.git/` and `_bases/` directories and the spine files `index.md`, `SCHEMA.md` and `log.md`. It also skips any file whose extension is not a known text, image, PDF or audio kind, so a stray binary never triggers a surprise analyse call.

Markdown and text files are filed as notes, while images, PDFs and audio are analysed and kept as assets.

## Running it on a deployed appliance

`thoth capture` pulls and pushes the vault git repo, so the shell you run it in needs the same secrets the daemon uses, most importantly the vault remote's token.

`load_config` reads `~/.thoth/.env` into the configuration but never exports it into the process environment, and the git sync inherits the real shell environment. The `thoth-slack` service gets those values from systemd, but an interactive shell does not.

So before a manual import, source the env once:

```console
$ set -a; . ~/.thoth/.env; set +a
$ thoth capture ~/notes
```

```{warning}
Without this the run fails at the initial vault pull with `Authentication failed` for the
vault remote.
```

## Curate (the default) against as-is

```console
$ thoth capture ~/notes              # curate each file (the full value-add)
$ thoth capture ~/notes --as-is      # low-touch: route + file verbatim, skip curate
```

- **The default, curate.** Every file runs the classify *and* curate LLM passes, so it is classified into the 4-folder model, given a `summary:`, wikilinked and dedup-merged. That is two LLM calls per file.
- **`--as-is`.** This runs only the cheap classify call for routing, then files the original body verbatim into the routed folder and indexes it, with no curate call and no reshaping. It is best for an already-clean Markdown vault you do not want re-authored. See [ADR 0010](../explanations/decisions/0010-capture-as-is-low-touch-import.md) for the exact semantics.

## Budget override

A bulk import is a real spend burst. `--budget N` overrides `THOTH_DAILY_LLM_BUDGET` for this run only, and is never written back to the config:

```console
$ thoth capture ~/notes --budget 200   # cap this run at 200 combined LLM calls
$ thoth capture ~/notes --budget 0     # unlimited for this import (escape hatch)
```

`--budget 0` disables the cap for the run, because the guard treats a non-positive limit as disabled. With no flag, the configured daily budget applies unchanged.

## Drain the inbox (bare `thoth capture`, no path)

A capture that could not be curated when it arrived, through an LLM outage or a bulk import that hit the daily budget cap partway, is held durably as `inbox/hold-<sha>.md` with its body and original intent intact.

Running `thoth capture` with no path re-files every recoverable hold from its stored body through the same ingest pipeline:

```console
$ thoth capture                      # drain the inbox: re-file every text hold
$ thoth capture --dry-run            # list what would be re-filed; write nothing
$ thoth capture --budget 0           # drain with the cap disabled for this run
```

This is source-independent, so it works even for Slack and MCP captures whose original source is long gone, because the hold body is the source.

Each hold is re-filed with the mode it was captured under. A hold deferred during an `--as-is` import re-files as-is and a normal one re-curates, so you do not have to remember which is which.

A hold is removed only once its page is genuinely filed. A hold that defers again, still with no LLM, or that is already curated and unchanged, is left in place, so the drain is safely resumable across budget days.

Binary holds, meaning an image or PDF whose bytes were never durably kept, are skipped and logged rather than re-filed from a content-free stub. Only re-running the original `thoth capture <path>` over the source file recovers those.

## Trial runs and filtering

```console
$ thoth capture ~/notes --dry-run                  # list what would be filed; write nothing
$ thoth capture ~/notes --limit 5                  # process at most 5 files
$ thoth capture ~/notes --include '*.md'           # only Markdown (repeatable)
$ thoth capture ~/notes --exclude 'drafts/*'       # skip a subtree (repeatable)
```

`--dry-run` makes no LLM call, no vault pull and no write. It only prints the planned filings.

`--include` and `--exclude` are `fnmatch` globs matched against each file's path relative to the walk root, and `--exclude` wins over `--include`.

## Commits and re-runs

The vault is pulled once up front and commits are batched. `--batch-size N`, defaulting to 25, commits and pushes every N files plus a final flush, instead of one commit per file.

Re-running over an unchanged tree is a true no-op. When a file's `raw/` source is byte-identical to what is already on disk, through the SHA-256 idempotency layer, *and* its curated page already exists, the import short-circuits before the classify-routed curate pass. Nothing is re-spent against the budget, no page's `updated:` date is bumped, and the re-run reports those files as `unchanged`.

So a re-run to finish an import that tripped the daily budget cap, or that you interrupted, costs nothing for the parts already done and resumes only the rest.

A `Ctrl-C` mid-run leaves the vault uncommitted, though durable on disk, so just re-run.
