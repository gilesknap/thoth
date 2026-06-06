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
  live before merge", "restart thoth-slack". Also covers packaging/publishing the
  container image + OCI Helm chart (Charts/thoth, _helm.yml, the helm-schema hook
  foot-guns, verifying a ghcr publish is public) — e.g. "test the chart publish",
  "tag a beta", "why does the helm-schema lint hook fail".
---

# Testing & verifying thoth changes

## The GATE

Before opening or merging a PR, run the full gate:

```bash
uv run --locked tox -p -r
```

This runs lint (`ruff check` + `ruff format --check`), type-checking (`pyright`),
the pytest suite (testpaths: `docs`, `src`, `tests`), and the docs build. Quicker
inner loop: `uv run pytest`, `uv run ruff check src/ tests/`, `uv run pyright`.

- **`test_cli_version` flakes under `-p`** — confirm it passes standalone before
  treating a failure as real.
- **Pyright "X cannot be assigned to X" duplicate-module errors** mean a stale
  `build/` tree shadows `src/`. `rm -rf build` and re-run; never a real type error.

**Docs inner loop** (run after any docs edit, without the full tox run):

```bash
uv run --group dev sphinx-build --fresh-env --fail-on-warning --keep-going docs build/html
```

`dev` is a **dependency-group**, not an extra (`--extra dev` fails). `--fail-on-warning`
turns broken `{doc}` xrefs and **orphaned pages** (a new page in no toctree) into
failures. MyST cross-page markdown links need the `.md` extension or a `{doc}` role.

## Why CI is necessary but not sufficient

Every external boundary (Slack, Anthropic, Hindsight, Firecrawl, the git remote,
Postgres) is exercised against an **injected fake** (SPEC §12). That makes the suite
fast and deterministic — but **green CI cannot catch SDK/boundary drift**. A
dependency that changes its real API surface (method shapes, response models, CLI
flags) passes every mocked test and still breaks live. So any change touching a real
boundary — an SDK bump, a new external call, a changed CLI invocation — must be
exercised against the real service before merge. Pure internal refactors can rely on
the suite alone.

## Debugging a capture

Set `THOTH_LOG_LEVEL=DEBUG` and reproduce the capture **before** probing vault bytes
or the state DB by hand. The ingest pipeline emits a DEBUG trail at every decision
point (#125): downscale, analyse (kind/image-count/bytes/model), classify
(type/slug/title), write-page (created vs updated-by-slug), dedup, defer/hold, budget
guard. Default `INFO` is unchanged. Read the log to confirm *what fired*.

**Don't read vault git state mid-capture.** A capture writes its page + asset to the
working tree before it commits, and the commit lands seconds after `analyse done` —
so a `git status` in that window shows `??` untracked files that look exactly like an
orphaned-asset bug. Always wait for the terminal `ingest filed: <paths>` line (or a
fresh `git log -1`) before concluding anything about atomicity or orphans.

## Live smoke

The repo has a `live` pytest marker for tests that hit real services with real keys
(skipped by default — no keys in CI):

```bash
THOTH_LIVE_SMOKE=1 uv run --extra runtime pytest -m live -k "<area>"
```

Some live-only failures are about **message shape**, not the happy path — e.g. the
tool-use repair/retry turn must lead with a `tool_result` block or the real Messages
API returns HTTP 400 (#110; injected fakes ignore this). To exercise a "second
attempt" boundary live, inject a one-shot failure deterministically rather than
relying on the model or the budget to produce one. See
`tests/test_live_smoke.py::test_live_curate_repair_turn_round_trips_after_tool_use_rejection`.

## Verify live on the VPS (the gold standard)

For boundary/SDK changes, run the **feature branch** against the real services and
observe real behavior *before* merging. Host/credentials are in the operator's
private notes — never commit them.

**Deploy:**
- The appliance is an **editable install** (`.pth` on `sys.path`), so a `git checkout`
  + service restart picks up new source *and* templates with no reinstall.
- Always run `uv sync --extra runtime` after re-pointing (no-op if deps unchanged;
  installs any dep bump and rebuilds editable metadata). **Don't trust the startup
  version string to prove a deploy** — it lags HEAD until `uv sync` rebuilds it;
  confirm with the source-tree git HEAD (`git -C /opt/thoth log -1`).
- The daemon runs under systemd (`thoth-slack`, `thoth-hindsight`); restart after the
  checkout.

**Verify a subprocess boundary under the unit's real sandbox.** `thoth-slack.service`
is hardened (`ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, narrow
`ReadWritePaths`). Code that shells out (e.g. the `whisper` STT subprocess) can pass a
bare run yet **fail only under confinement** — a CLI that writes to cwd dies on the
read-only `WorkingDirectory=/opt/thoth`; one downloading to `~/.cache` dies on
read-only home. CI can't catch this. Reproduce the exact confinement without touching
the live daemon via `systemd-run --pipe --wait --collect` with the same `-p` hardening
directives the unit sets. Foot-gun: `whisper` **catches** its own write error, logs
`Skipping …`, and still **exits 0** — so a returncode-only check reads a sandbox
failure as success. Direct such a tool's output to a writable temp dir and read the
file rather than scraping stdout/returncode.

**Smoke without the live pipeline:**
- `thoth init` into a throwaway `PKM_VAULT=/tmp/...`, then inspect the seeded
  `index.md` / `SCHEMA.md` — verifies template/spine changes.
- `thoth lint` over a vault — exercises the maintenance invariants.
- There is **no CLI capture path** (ingest runs only through Slack) and **no `query`
  CLI** (MCP or Slack only). To drive `QueryEngine.answer` live, run a snippet on the
  appliance with the env sourced and the systemd vars set:

  ```bash
  cd /opt/thoth
  set -a; . ~/.thoth/.env; set +a
  export PKM_VAULT=/opt/pkm-vault THOTH_HOME=$HOME/.thoth OBSIDIAN_VAULT_NAME=pkm-vault
  uv run python - <<'PY'
  import logging, sys; logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
  from thoth.config import load_config; from thoth.hindsight import Hindsight
  from thoth.vault import Vault; from thoth.query import QueryEngine
  cfg = load_config(); qe = QueryEngine(cfg, Vault(cfg), Hindsight(cfg))
  r = qe.answer("<query>", max_pages=12)
  for p in r.provenance: print(p.rank, p.path, p.methods)
  PY
  ```

  `QueryResult.provenance` (`PageProvenance(path, methods, rank)`) shows which source
  — `grep` / `wikilink` / `recall` — surfaced each cited page; the DEBUG `query blend:`
  line adds the semantic pass's wall-clock.

## Kubernetes / Helm chart packaging & publish

thoth ships a container image + a **published OCI Helm chart** (`Charts/thoth/`, built
by `_container.yml` + `_helm.yml`, the `epics-containers/ec-helm-charts` pattern). Both
publish to ghcr **on a git tag** (`X.Y.Z` or `X.Y.Z-{alpha,beta,rc}.N` — `_helm.yml`
asserts exactly that regex). Push is gated `publish && ref_type == 'tag'` (publish =
tests green); a PR build packages but does not push.

**Test the publish path with a beta tag.** Tag the tip `0.x.0-beta.1` and push the tag
— CI runs the full matrix, then pushes `ghcr.io/<owner>/thoth:<tag>` + `latest` and the
chart to `oci://ghcr.io/<owner>/charts`. `_release.yml` has **no PyPI step**, so a beta
tag is safe to throw away.

**Verify a publish is public — anonymously, the way ArgoCD pulls** (ghcr packages are
**private by default**; flip both `thoth` and `charts/thoth` to public in the ghcr UI):

```bash
helm registry logout ghcr.io
helm pull oci://ghcr.io/<owner>/charts/thoth --version <tag>      # chart: succeeds = public
tok=$(curl -s "https://ghcr.io/token?scope=repository:<owner>/thoth:pull" | jq -r .token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $tok" \
  https://ghcr.io/v2/<owner>/thoth/tags/list                      # image: 200 = public
```

Two `helm-schema` hook foot-guns (`Charts/thoth/values.schema.json` is generated by the
`losisin/helm-values-schema-json` hook, which shells out to the `helm schema` plugin):

- **The plugin must exist everywhere pre-commit runs.** Bare-ubuntu CI fails the hook
  (`Please install … plugin!`). The fix (ec pattern): install the plugin in both the
  devcontainer (`Dockerfile` developer stage) and a dedicated `_precommit.yml` CI job
  (`ci.yml`'s `lint` uses it; `type-checking` stays on `_tox.yml`). Don't delete a
  failing schema hook — install the plugin where it runs. **A fresh agent sandbox is
  often NOT the built devcontainer** (no `helm`/`gpg`, no `/cache` mount, no
  `pre-commit`, read-only `/usr/local/bin`) — so don't try to live-install helm there;
  the Dockerfile already has it, so **rebuild the container** to pick it up.
- **`@schema` annotation comments must not contain `;`.** The parser treats `;` as the
  annotation separator, so a `;` in a description makes the generator error and write
  **nothing** — the committed schema silently stops updating. Keep descriptions
  semicolon-free; regenerate with `helm schema --config Charts/thoth/.schema.config.yaml`
  and commit. It's an idempotent fixpoint, so the CI hook then passes as a no-op.

## Verifying retrieval / recall changes

Three traps when judging a semantic-retrieval change live:

- **Test the dense, on-topic domain that dominates the bank — not sparse one-off
  topics.** Live recall is low-resolution: it tends to return the bank's dominant
  cluster regardless of query. So an on-topic query in a dense domain surfaces real
  grep-missed wins, while a sparse/off-topic query (one dog photo) collapses into that
  cluster as tail-rank noise the answer LLM ignores. Judging a recall change on a
  sparse worst-case query reads as "no gain" and is misleading. (Embedding resolution,
  not staleness — `--full-rebuild` doesn't change it.)
- **`thoth reindex --full-rebuild` is silent per-page at INFO** (one fact-extraction
  per page; ~225 pages ≈ 30 min), logging only a final summary. Don't read the quiet
  log as stuck — confirm progress with `pgrep -af 'memory retain'`. Run detached and
  poll; `--budget 0` ignores the daily LLM cap so it can't be throttled mid-run.
- **Most "retrieval got worse/better" reports measure the CLIENT, not thoth.** thoth
  doesn't decide the output — the calling Claude session does. Control for three
  confounds: (1) **did `pkm_search` run at all** — a session may answer from
  training/web; compare `pkm_search` to `pkm_search`; (2) **file-grep cheating** — a
  session whose cwd *is* the vault checkout reads `.md` directly via `Grep`/`Read` even
  when told "MCP only" (soft instructions don't disable built-in file tools); run the
  eval in a dir with no vault, or `permissions.deny` `Read`/`Grep`/`Glob`; (3) **the
  `max_pages=5` cap** structurally limits the MCP path — bump it to see more. A fair
  comparison fixes the tool, kills file access, bumps the cap, and reads `provenance`.
