# Recover from a lost VPS

The backup model follows from the source-of-truth decision in SPEC section 10. The `pkm-vault` git repo *is* the durable knowledge backup, the Hindsight semantic index is disposable because it is rebuilt from the vault, the `thoth` repo backs up code and config, and secrets live only in `~/.thoth/.env` at chmod 600 and in a password manager.

So a full recovery is a handful of clones plus a reindex.

## Canonical recovery (always correct)

This path needs nothing but the two repos and the secrets, and it never depends on any index snapshot.

1. Provision a new VPS (Ubuntu 24.04+, 2 cores, 8 GB RAM, 50 GB+ disk), then install the prerequisites and `uv`.
2. Authenticate `gh`, then clone `thoth` with the inline `gh` credential helper and a nulled global config, so that a user `insteadOf` ssh-rewrite cannot hijack the HTTPS URL:

   ```bash
   echo "$PAT" | gh auth login --with-token
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/thoth.git /opt/thoth
   ```

3. Clone the canonical vault. This *is* the knowledge restore, and nothing else is needed:

   ```bash
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/pkm-vault.git /opt/pkm-vault
   ```

4. Re-add secrets by hand from the password manager, into `~/.thoth/.env` at `chmod 600`.
5. Rebuild the index from the vault, which is the canonical, always-correct step:

   ```bash
   PKM_VAULT=/opt/pkm-vault thoth reindex --full-rebuild
   ```

   A full rebuild of a large vault is a real spend burst against the daily LLM budget. To let the rebuild run to completion in one pass, override the cap for that run with `--budget`, which is a transient override and is never written back to the config:

   ```bash
   PKM_VAULT=/opt/pkm-vault thoth reindex --full-rebuild --budget 0   # uncapped
   PKM_VAULT=/opt/pkm-vault thoth reindex --full-rebuild --budget 500 # cap this run
   ```

   Without `--budget`, a rebuild that hits the cap stops cleanly mid-walk, with the pages retained so far recorded, and resumes on the next day's run.

6. Re-enable the systemd unit (`thoth-slack.service`) and the system cron, then work through the [first-light smoke checklist](first-light.md).

Recovery takes roughly one to two hours, dominated by package installs and the reindex pass. The knowledge itself is restored the instant the vault clone completes.

## Recall provenance round-trips by `document_id` after a restore

`thoth reindex --full-rebuild` re-stores every vault page with the vault-relative path carried as the memory's `document_id`, and the page type carried as a `document_tag`.

Recall recovers the source path from each hit's `document_id`, falling back to the in-band `SOURCE: <rel-path>` sentinel line only when it is absent. Hindsight runs LLM fact-extraction, so the sentinel can be stranded on one atomic fact or on none, which is why the `document_id` is preferred (SPEC section 8).

Both channels are restored by the rebuild, so retrieval keeps citing the right vault page after recovery.

```text
- [ ] confirm the restore by checking `thoth reindex --full-rebuild` completed
      (document_id re-attached), not by grepping for SOURCE: lines
```

## Optional fast restore (an optimisation, never a substitute)

The optional gated snapshot is `bin/hindsight-backup.sh`, which takes a logical `pg_dump` of the Hindsight bank plus a copy of `reindex-manifest.json` after a successful nightly reindex, retains about 3 generations, and is enabled with `THOTH_HINDSIGHT_BACKUP=1`.

When that snapshot exists, step 5 above may be replaced with a faster cold start that *restores* the dump instead of re-embedding from scratch:

1. Restore the most recent `pg_dump` into the Hindsight bank's Postgres database, and copy the matching `reindex-manifest-<TS>.json` back to `~/.thoth/hindsight/reindex-manifest.json`.
2. Run an incremental reindex, with no `--full-rebuild`, so that any vault drift since the snapshot is caught. Unchanged pages are skipped via the body-`sha256` manifest, changed and new pages are re-retained, and deleted pages are pruned:

   ```bash
   PKM_VAULT=/opt/pkm-vault thoth reindex
   ```

3. Then start the unit and cron as in canonical step 6.

This buys a faster restore on a large bank.

```{warning}
The fast restore is strictly subordinate to `--full-rebuild`. The index is disposable and the
vault is the durable backup (SPEC section 10), so a missing, stale or unrestorable snapshot is
never an error. Fall back to the canonical step 5, `thoth reindex --full-rebuild`, which
deterministically re-derives the entire bank and its `rel` provenance tags from the vault.
```

## What is *not* recovered, and why that is fine

| Asset | On VPS loss | Recoverable from |
|---|---|---|
| Knowledge (vault markdown and `raw/assets/` binaries) | safe in the `pkm-vault` repo | `git clone`, where any commit is a point-in-time snapshot |
| Hindsight semantic index | rebuilt, because it is disposable | `thoth reindex --full-rebuild`, or the optional snapshot for a faster cold start |
| App code and config (`thoth` repo) | safe in the `thoth` repo | `git clone` of `thoth` |
| Transient state (`~/.thoth/state.db`) | not backed up, so start fresh | only dedupe history and mid-flight captures are lost, and both are cheap |
| Secrets (`~/.thoth/.env`) | never in any repo | manual re-entry from the password manager |

Losing the transient state DB loses nothing canonical, because knowledge is safe in the vault repo.

Plain git is good to about 1 GB. When `raw/assets/` growth pushes the repo toward that, migrate binaries to Git LFS, which is free to 10 GB, or move the asset tree to restic against Backblaze B2 while keeping the markdown in plain git. That is a later optimisation rather than an upfront requirement.
