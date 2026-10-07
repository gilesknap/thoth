# 16. Keep the sensitive vault's MCP on SSH-to-loopback; defer public Cloudflare-OAuth exposure

Date: 2026-06-14

## Status

Accepted

Amends [ADR 0011](0011-mcp-http-transport-and-tiered-auth.md). That ADR assumed claude.ai's
custom connector could reach the MCP server through Cloudflare Access "Managed OAuth". As of
June 2026 that path is broken for the claude.ai **web/mobile** connector (it still works for
Claude Code), so the exposure decision below supersedes 0011's implicit "expose the sensitive
box via the tunnel + Managed OAuth" plan. (A self-hosted OAuth server was later added in
PR #161 — itself a deviation from 0011's "no OAuth server in thoth"; this ADR does not remove
it, it records why the **sensitive** deployment relies on no public OAuth at all.)

## Context

There are two thoth deployments with very different stakes:

- **Sensitive (live).** The owner's real vault. The MCP server binds loopback
  (`127.0.0.1:8765`, per ADR 0011) and is reached only by tunnelling that port over SSH. It
  is not exposed to the internet at all.
- **Demo (non-sensitive).** A throwaway vault on the home cluster, fronted publicly by a
  Cloudflare tunnel + Cloudflare Access with a GitHub identity provider (so collaborators
  with GitHub accounts can be invited). This is the only place public OAuth has been proven.

The open question was whether to expose the **sensitive** MCP publicly behind Cloudflare
Access "Managed OAuth" — the same OTP + OAuth wall the cluster's web UIs use — so it could be
reached from claude.ai on mobile and from a remote Claude Code without an SSH tunnel.

Research into the current (June 2026) state settled it:

1. **Cloudflare Managed OAuth is the blessed pattern.** It puts an Access-protected app
   behind an agent-compatible OAuth interface (RFC 9728 discovery, DCR, PKCE) and is
   explicitly marketed for clients like Claude's.
2. **But the claude.ai web + mobile connector currently fails against it.**
   `anthropics/claude-ai-mcp#410` has multiple independent reproductions through 2026-06-13:
   the connector fails instantly at *Connect*, with no Access login screen ever rendered. The
   issue was closed attributing it to Cloudflare bot-challenge rules, but the latest reporter
   disproved that (Anthropic's own egress requests were allowed; no challenge fired). The
   forensic root cause points at the Managed-OAuth `401` omitting the `WWW-Authenticate`
   header — which the connector requires and Claude Code tolerates. It is an
   Anthropic↔Cloudflare interop bug, not a server-side misconfiguration.
3. **Claude Code (CLI) works against the identical Managed-OAuth URL** — confirmed by every
   reporter.

The asymmetry is decisive. The sensitive box is driven only from Claude Code, and for the
CLI, SSH-to-loopback and a public Managed-OAuth endpoint are *functionally identical*.
Exposing the sensitive vault publicly would therefore gain **no** functionality while
**enlarging the attack surface** — from "no public listener; the only door is the SSH key" to
"a public OAuth endpoint behind an OTP wall". The one prize behind public OAuth, mobile
claude.ai, is exactly the path that is currently broken.

## Decision

- **The sensitive vault's MCP stays loopback-bound, reached only over SSH.** It is not
  exposed to the internet. SSH key access is the gate; the closed `pkm_*` surface (ADR 0011)
  still governs *what* a caller can do.
- **Public Cloudflare Access "Managed OAuth" is validated on the demo cluster only**, where
  the vault is non-sensitive. This proves the Managed-OAuth → Claude Code flow end to end and
  keeps the pattern ready to adopt.
- **Revisit exposing the sensitive box when `anthropics/claude-ai-mcp#410` is fixed.** That
  is the trigger: once the claude.ai connector completes OAuth against Cloudflare Managed
  OAuth, mobile access becomes a real reason to expose the box, the CLI path already works, so
  the cutover is low-risk.
- **Interim second layer, *if* any public exposure is ever added:** a Cloudflare
  WAF/firewall rule on the `/mcp` path that allows only Anthropic's published egress range
  (`160.79.104.0/23` and `2607:6bc0::/48`; the older `/21` is deprecated) plus the owner's
  own IPs. This is connector-compatible (the tool calls genuinely originate from that range)
  and makes a stolen bearer useless from anywhere else. It gates the data path, not the
  browser authorization step, so it is defense-in-depth on top of OAuth, never a replacement
  for it.

## Consequences

- **No functionality lost.** Claude Code keeps full access to the sensitive vault over the
  SSH tunnel, exactly as before.
- **Smallest attack surface for the data that matters.** The sensitive vault has no
  internet-facing listener and no public auth code — hand-rolled or managed — to attack.
- **Mobile claude.ai is deferred, not abandoned.** It is blocked upstream; the demo-cluster
  validation plus the documented revisit trigger make adopting it later a small, planned step
  rather than a redesign.
- **A clear watch item.** `anthropics/claude-ai-mcp#410` is the thing to monitor; its fix is
  what flips this decision for the sensitive box.
- **GitHub 2FA is the de-facto second factor** for any GitHub-OAuth path that is used,
  because thoth delegates identity to GitHub.
