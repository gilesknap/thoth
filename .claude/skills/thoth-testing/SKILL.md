---
name: thoth-testing
description: >-
  How to test and verify changes to thoth — the tox GATE, the injected-fakes
  testing model (and why green CI does not prove a boundary change works), the
  live-smoke marker, and how to deploy a branch to the live VPS appliance and
  verify it before merge (the gold standard: git checkout + uv sync + systemd
  restart, plus the deploy-to-verify gotchas). Use when running the test suite,
  adding tests, deciding whether a change is safe to merge, or deploying /
  testing / verifying a branch on the VPS (the live appliance / production box)
  — e.g. "test this on the vps", "deploy the branch to the appliance", "verify
  live before merge", "restart thoth-slack".
---

# Testing & verifying thoth changes

## The GATE

Before opening or merging a PR, run the full gate:

```bash
uv run --locked tox -p -r
```

This runs lint (`ruff check` + `ruff format --check`), type-checking (`pyright`),
the pytest suite (configured testpaths include `docs`, `src`, `tests`), and the
docs build. For a quicker inner loop: `uv run pytest`, `uv run ruff check src/
tests/`, `uv run pyright`.

**Known flake:** `test_cli_version` can fail under parallel (`-p`) runs; if it
fails, confirm it passes standalone before treating it as a real regression.

**Pyright "X cannot be assigned to X" duplicate-module errors** mean a stale
`build/` tree (an editable/sdist build artifact) is shadowing `src/` — pyright
sees two copies of every type. `build/` is gitignored, so just `rm -rf build`
and re-run; it is never a real type error.

## Rebasing a PR onto main before merge

When `main` has moved under a PR (e.g. a sibling PR merged first), GitHub may
report a conflict at merge time. Rebase the branch locally, resolve, re-run the
GATE, force-push, then merge. Two foot-guns that bit a real session:

- **Commit *every* post-rebase fix before you force-push.** `git rebase
  --continue` commits the conflict resolution — but any edits you make *after*
  that to get the GATE green (a lint wrap, a stale-kwarg test fix) are
  uncommitted working-tree changes. Force-pushing the rebase commit ships a tree
  that is **not** what you just GATE-tested, leaving `main` red after merge.
  Always `git status` clean + re-run the GATE on the *committed* tip before
  `push --force`. (Recovery is a follow-up "repair GATE" PR — avoidable.)
- **Convergent designs merge, they don't fight.** Two parallel branches can
  independently introduce the *same* concept (e.g. both #129 and #134 added an
  `is_transcript` flag). Resolve by taking the richer base and threading the
  branch's unique delta through it (mirror the symmetric call site), not by
  picking one side wholesale.

## First verification step: `THOTH_LOG_LEVEL=DEBUG`

Before probing the vault bytes or the state DB by hand, set
`THOTH_LOG_LEVEL=DEBUG` and reproduce the capture: the ingest pipeline emits a
DEBUG trail at every decision point (issue #125) — downscale (fired vs
under-threshold, before→after bytes), analyse (kind, image count, bytes sent,
OCR/text length, model), classify (chosen type/slug/title), write-page
(created vs updated-by-slug — page reuse), dedup short-circuit, defer/hold
(reason + permanent-vs-transient + HTTP status), and the budget guard
(allowed/blocked + spend vs cap). Default `INFO` output is unchanged, so this is
opt-in and quiet by default. Read the log to confirm *what fired* — it answers
"did the downscale run?" / "which type did classify pick?" / "did this merge an
existing page?" instantly, without an out-of-band probe.

## Why CI is necessary but not sufficient

Every external boundary (Slack, Anthropic, Hindsight, Exa, Firecrawl, the git
remote, Postgres) is exercised in tests against an **injected fake** (see SPEC
section 12). That makes the suite fast and deterministic — but it means **green CI
cannot catch SDK/boundary drift**. A dependency that changes its real API surface
(client method shapes, response models, CLI flags) passes every mocked test and
still breaks against the live service.

So for any change that touches a real boundary — an SDK bump, a new external call,
a change to how a CLI is invoked — CI passing is not enough. The boundary must be
exercised against the real service.

## Live smoke

The repo has a `live` pytest marker for tests that hit real services with real
keys:

```bash
THOTH_LIVE_SMOKE=1 uv run --extra runtime pytest -m live -k "<area>"
```

These are skipped by default (no keys in CI). Run them where real credentials and
the real Hindsight CLI are available.

### Testing a tool-use repair/retry path live

Some live-only failures are about **message shape**, not the happy path. The
canonical case (issue #110): after a forced tool call, the repair/retry user turn
must lead with a `tool_result` block keyed to the `tool_use` id, or the real
Messages API returns HTTP 400. Injected fakes ignore that precondition, so only a
real round-trip proves the shape — exactly the gap above.

Two things make this hard to exercise: it fires only on a **validation failure**
(not the happy path), and you **cannot** trigger it by exhausting the budget —
`--budget 1` defers the second call *before* the repair turn fires (wrong lever).
Instead, force exactly one failure deterministically: wrap the validator
(`_parse_and_validate_plan`) to raise on the first call and delegate to the real
one on the second, stub the downstream side effect (`_write_planned_page`), wire
**no** budget guard so two real calls are allowed, then assert the call recovers.
See `tests/test_live_smoke.py::test_live_curate_repair_turn_round_trips_after_tool_use_rejection`.
The pattern generalises: to test any "second attempt" boundary live, inject a
one-shot failure rather than relying on the model or the budget to produce one.

## Verify live before merge (the gold standard)

For boundary/SDK changes, the strongest verification is running the **feature
branch** against the real services and observing real behavior — *before* merging,
not after. Treat "green CI + verified live" as the bar for those changes; pure
internal refactors can rely on the suite alone.

### Deploy-to-verify gotchas

When deploying a branch to a running appliance to verify it (see the operator's
private deployment notes for host/credentials — never commit those):

- The appliance is an **editable install** (`.pth` on `sys.path`), so a
  `git checkout` + service restart picks up new source *and* templates with no
  reinstall.
- After re-pointing, always run `uv sync --extra runtime`. It's a no-op when
  dependencies are unchanged, but it (a) installs any dependency bump the target
  branch introduced and (b) rebuilds the editable metadata so the startup-log
  version string reflects the real HEAD. **Don't trust the startup version string
  alone to prove a deploy** — it can lag the checked-out HEAD until `uv sync`
  rebuilds it; confirm by checking the source-tree git HEAD instead.
- The daemon runs under systemd (`thoth-slack`, `thoth-hindsight`); restart after
  the checkout.

### Verify a subprocess boundary under the unit's real sandbox

`thoth-slack.service` is hardened (`ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, a narrow `ReadWritePaths`). Code that shells out (e.g. the `whisper`
STT subprocess in `extract.py`) can pass a bare `pytest`/manual run yet **fail only
under that confinement** — a CLI that writes to its cwd dies on the read-only
`WorkingDirectory=/opt/thoth`, and a tool that downloads to `~/.cache` dies on
read-only home. CI cannot catch this (it has no runtime deps, no sandbox).

Reproduce the *exact* confinement without touching the live daemon using
`systemd-run` with the same hardening directives the unit sets:

```
systemd-run --pipe --wait --collect -q \
  -p User=pkm -p Group=pkm -p WorkingDirectory=/opt/thoth \
  -p ProtectSystem=strict -p ProtectHome=read-only -p PrivateTmp=true \
  -p ReadWritePaths="/opt/pkm-vault /home/pkm/.thoth /home/pkm/.hindsight /home/pkm/.pg0" \
  -p Environment=HOME=/home/pkm \
  <the-exact-argv-the-code-runs>
```

A foot-gun this caught: the `whisper` CLI **catches** its own output-file write
error, logs `Skipping …`, and still **exits 0** — so a returncode-only check reads
a sandbox failure as an empty success. Direct such a tool's output to a temp dir
(writable via `PrivateTmp`) and read the file, rather than scraping stdout. Note
`PrivateTmp` gives the run its *own* `/tmp`, so stage any input file the test reads
somewhere the sandbox can see it (a `ReadWritePaths` dir, or `/home/pkm` which is
readable under `ProtectHome=read-only`), not `/tmp`.

### Smoke without the live pipeline

Some checks don't need a real Slack message:

- `thoth init` into a throwaway `PKM_VAULT=/tmp/...` dir, then inspect the seeded
  `index.md` / `SCHEMA.md` — verifies template/spine changes.
- `thoth lint` over a vault — exercises the maintenance invariants.

There is **no CLI capture path** — the full ingest pipeline only runs through
Slack, so end-to-end capture verification needs a real posted message.
