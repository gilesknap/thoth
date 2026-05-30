# Vault Log

> Chronological record of all agent actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, create, update, query, lint, archive, delete, reindex
> Rotate when this file exceeds 500 entries OR at year end: rename to log-YYYY.md, start fresh.

## [2026-05-30] create | Vault initialized
- Migrated from documents/ proto-vault
- Structure: raw/{articles,papers,transcripts,assets}, entities/, concepts/, comparisons/,
  queries/, actions/, media/, memories/, people/, _bases/, _archive/, inbox/
