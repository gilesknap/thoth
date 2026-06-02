[![CI](https://github.com/gilesknap/thoth/actions/workflows/ci.yml/badge.svg)](https://github.com/gilesknap/thoth/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/gilesknap/thoth/branch/main/graph/badge.svg)](https://codecov.io/gh/gilesknap/thoth)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# thoth

**A personal, single-user "second brain" appliance.** Capture anything — a URL, a PDF,
an image, a voice memo, or a quick note — by dropping it into one private Slack channel,
and thoth files it into a git-backed [Obsidian](https://obsidian.md) vault: classified,
curated into clean Markdown, cross-linked to your existing pages, and indexed for semantic
recall. Ask a question in the same channel and it answers **from your vault** (and,
optionally, the web), citing the pages it actually used. The same knowledge is exposed to
AI assistants over [MCP](https://modelcontextprotocol.io).

What            | Where
:---:           | :---:
Source          | <https://github.com/gilesknap/thoth>
Documentation   | <https://gilesknap.github.io/thoth>
Releases        | <https://github.com/gilesknap/thoth/releases>

## What it does

- **Capture from Slack.** Post a link, upload a file, or jot a note in a private channel;
  thoth fetches/transcribes/OCRs it, decides what kind of thing it is, writes a tidy page,
  and links it to related notes — replying in-thread with an `obsidian://` link.
- **Retrieve from Slack.** Ask a question and get a conversational answer grounded in your
  vault, with a short `Sources:` list of the pages it used (web-blended when you ask it to
  research).
- **Your knowledge is plain Markdown in git.** The Obsidian vault is the single source of
  truth — open it in Obsidian, edit it by hand, grep it, diff it. No lock-in.
- **Semantic recall.** A rebuildable vector index
  ([Hindsight](https://hindsight.vectorize.io)) sits over the vault so retrieval finds
  things by meaning, not just keywords. It is **disposable** — re-derived from the vault at
  any time.
- **MCP server.** Exposes `pkm_*` tools (search, ask, ingest, recent, todos, write) so
  Claude Desktop or any MCP client can read and write the same vault.

## How it works

A small set of injected boundaries do the heavy lifting: **Claude** classifies, curates,
and answers; **Whisper** (local) transcribes audio; **Exa** + **Firecrawl** handle web
search and URL→Markdown extraction; **Hindsight** provides the semantic index; and the
**Obsidian vault** (a two-way-synced git repo) is the canonical store. It runs unattended
on a small VPS as a single long-running Slack daemon (`thoth slack`) plus a handful of cron
jobs.

```console
$ thoth --version    # confirm the CLI is on your PATH
$ thoth slack        # run the capture/retrieve daemon (Socket Mode)
```

<!-- README only content. Anything below this line won't be included in index.md -->

## Documentation

Full documentation is published at <https://gilesknap.github.io/thoth>. The key guides
(source under [`docs/`](docs/)):

- **Deploy the appliance** (the main path) — a full, dependency-by-dependency setup
  including every API key and the `.env`:
  [`docs/how-to/deploy-appliance.md`](docs/how-to/deploy-appliance.md).
- **Set up the Slack app** — [`docs/how-to/slack-setup.md`](docs/how-to/slack-setup.md).
- **First-light smoke checklist** — verify each live boundary after deploy:
  [`docs/how-to/first-light.md`](docs/how-to/first-light.md).
- **Install for local development** — [`docs/tutorials/installation.md`](docs/tutorials/installation.md).
- **How it works / why** — [`docs/explanations/architecture.md`](docs/explanations/architecture.md)
  and [`docs/explanations/decisions.md`](docs/explanations/decisions.md).
