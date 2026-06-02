# thoth — working rules for Claude

## No backward-compatibility / migration prose

This is a **personal, single-user project in an early, iterative/experimental phase**. There
are no other users and no deployed setups worth preserving — the owner re-creates the
config, the vault, and the Slack app from scratch whenever needed.

So **do not** write backward-compatibility shims, migration guides, or "if you previously
set this up differently, do X to migrate" prose in docs or code. When something changes,
document only the new way and assume a clean slate.

## Public repo — never commit personal infra (leak-scan first)

This repository is **public**. Nothing committed — code, docs, tests, or the project
skills under `.claude/skills/` — may contain personal infrastructure: the VPS host/IP,
SSH key paths, API tokens or keys, vault paths under `/home/...`, or Slack member/channel
IDs. Those belong only in the owner's private notes / local config, never in a tracked
file. Use a host-agnostic `<owner>`/placeholder in committed text and reference "the
owner's private notes" instead.

**Leak-scan before every commit** — especially when promoting a lesson into a skill or
writing docs, since those are where real values tend to creep in from a working session.

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

After a `git checkout` on the appliance, confirm the deploy with `git -C /opt/thoth log -1`,
**not** the startup version string: the `.pth` editable install runs the code at HEAD
regardless, and plain `uv sync --extra runtime` often leaves that version string stale
(use `--reinstall-package thoth` only if you want it to match — purely cosmetic).
