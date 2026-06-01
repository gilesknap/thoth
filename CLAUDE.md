# thoth — working rules for Claude

## No backward-compatibility / migration prose

This is a **personal, single-user project in an early, iterative/experimental phase**. There
are no other users and no deployed setups worth preserving — the owner re-creates the
config, the vault, and the Slack app from scratch whenever needed.

So **do not** write backward-compatibility shims, migration guides, or "if you previously
set this up differently, do X to migrate" prose in docs or code. When something changes,
document only the new way and assume a clean slate.

## Verifying on the live appliance (VPS)

The strongest verification for boundary/SDK changes is running the branch against the real
services on the VPS — the **deploy-to-verify** procedure and the live-smoke suite are
documented in the **`thoth-testing` skill** (`.claude/skills/thoth-testing/`). The host and
credentials live only in the owner's private notes — **never commit them** (the docs use an
`<owner>`/host-agnostic placeholder).

SSH access does **not** survive across sandboxed sessions — any VPS key from a previous
session is gone. So when live access is needed, generate a **fresh** keypair that session
and ask the owner to install the public key (for both `pkm@` and `root@`); do not assume an
existing key is still present.
