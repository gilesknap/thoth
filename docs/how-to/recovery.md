# Recover from a lost VPS

The backup model follows from the source-of-truth decision (SPEC section 10): **the
`pkm-vault` git repo *is* the durable knowledge backup**, the Hindsight semantic index is
**disposable** (rebuilt from the vault), the `thoth` repo backs up code + config, and
secrets live only in `~/.thoth/.env` (chmod 600) and a password manager. So a full
recovery is a handful of clones plus a reindex.

## Canonical recovery (always correct)

This path needs nothing but the two repos and the secrets; it never depends on any index
snapshot.

1. Provision a new VPS (Ubuntu 24.04+, 2 cores, 8 GB RAM, 50 GB+ disk); install the
   prerequisites and `uv`.
2. Authenticate `gh`, then clone `thoth` with the inline `gh` credential helper and a
   nulled global config (so a user `insteadOf` ssh-rewrite cannot hijack the HTTPS URL):

   ```bash
   echo "$PAT" | gh auth login --with-token
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/thoth.git /opt/thoth
   ```

3. Clone the canonical vault -- this *is* the knowledge restore; nothing else is needed:

   ```bash
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/pkm-vault.git /opt/pkm-vault
   ```

4. Re-add secrets by hand from the password manager (`~/.thoth/.env`, `chmod 600`).
5. **Rebuild the index** from the vault (the canonical, always-correct step):

   ```bash
   PKM_VAULT=/opt/pkm-vault thoth reindex --full-rebuild
   ```

6. Re-enable the systemd unit (`thoth-slack.service`) and the system cron, then work
   through the [first-light smoke checklist](first-light.md).

Estimated recovery time ~1-2 h, dominated by package installs and the reindex pass; the
knowledge itself is restored the instant the vault clone completes.

## Recall provenance is tag-keyed after a restore

`thoth reindex --full-rebuild` re-stores every vault page with the vault-relative path
carried as the primary `rel` **tag** (alongside the page type). Recall recovers the source
path from each hit's `rel` tag, falling back to the in-band `SOURCE: <rel-path>` sentinel
line only when tags are absent (Hindsight runs LLM fact-extraction, so the sentinel can be
stranded on one atomic fact or none -- tags are therefore preferred; SPEC section 8). Both
channels are restored by the rebuild, so retrieval keeps citing the right vault page after
recovery.

```text
- [ ] confirm the restore by checking `thoth reindex --full-rebuild` completed (tags
      re-attached) -- not by grepping for SOURCE: lines
```

## Optional fast restore (an optimisation, never a substitute)

When the optional gated snapshot exists -- `bin/hindsight-backup.sh` takes a logical
`pg_dump` of the Hindsight bank plus a copy of `reindex-manifest.json` after a successful
nightly reindex, retaining ~3 generations, enabled with `THOTH_HINDSIGHT_BACKUP=1` -- step
5 above may be replaced with a faster cold start that *restores* the dump instead of
re-embedding from scratch:

1. restore the most recent `pg_dump` into the Hindsight bank's Postgres database, and copy
   the matching `reindex-manifest-<TS>.json` back to
   `~/.thoth/hindsight/reindex-manifest.json`;
2. run an **incremental** reindex (note: **no** `--full-rebuild`) so any vault drift since
   the snapshot is caught -- unchanged pages are skipped via the body-`sha256` manifest,
   changed/new pages are re-retained, and deleted pages are pruned:

   ```bash
   PKM_VAULT=/opt/pkm-vault thoth reindex
   ```

3. then start the unit + cron as in canonical step 6.

This buys a faster restore on a large bank, but it is **strictly subordinate to
`--full-rebuild`**: the index is **disposable** and the vault is the durable backup
(SPEC section 10), so a missing, stale, or unrestorable snapshot is **never** an error --
fall back to the canonical step 5 (`thoth reindex --full-rebuild`), which deterministically
re-derives the entire bank (and its `rel` provenance tags) from the vault.

## What is *not* recovered (and why that is fine)

| Asset | On VPS loss | Recoverable from |
|---|---|---|
| Knowledge (vault markdown + `raw/assets/` binaries) | safe in the `pkm-vault` repo | `git clone` -- any commit is a point-in-time snapshot |
| Hindsight semantic index | **rebuilt** (disposable) | `thoth reindex --full-rebuild`, or the optional snapshot for a faster cold start |
| App code + config (`thoth` repo) | safe in the `thoth` repo | `git clone` of `thoth` |
| Transient state (`~/.thoth/state.db`) | **not backed up** -- start fresh | only dedupe history + mid-flight captures lost, both cheap |
| Secrets (`~/.thoth/.env`) | **never** in any repo | manual re-entry from the password manager |

Losing the transient state DB loses nothing canonical. Knowledge is safe in the vault repo.

**Scaling note:** plain git is good to ~1 GB; when `raw/assets/` growth pushes the repo
toward ~1 GB, migrate binaries to Git LFS (10 GB free) or move the asset tree to restic to
Backblaze B2 while keeping the markdown in plain git. A later optimisation, not an upfront
requirement.
