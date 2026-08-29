[![CI](https://github.com/gilesknap/thoth/actions/workflows/ci.yml/badge.svg)](https://github.com/gilesknap/thoth/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/gilesknap/thoth/branch/main/graph/badge.svg)](https://codecov.io/gh/gilesknap/thoth)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# thoth

**A personal, single-user "second brain" appliance.** You capture anything by dropping it into one private Slack channel, be it a URL, a PDF, an image, a voice memo or a quick note.

thoth files what you drop into a git-backed [Obsidian](https://obsidian.md) vault that follows the [Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/). Each page arrives classified, curated into clean Markdown, cross-linked to your existing pages and indexed for semantic recall.

Ask a question in the same channel and it answers from your vault, citing the pages it actually used. It will blend in the web when you ask it to research.

The same knowledge is exposed to AI assistants over [MCP](https://modelcontextprotocol.io).

What            | Where
:---:           | :---:
Source          | <https://github.com/gilesknap/thoth>
Docker          | `docker run ghcr.io/gilesknap/thoth:latest`
Documentation   | <https://gilesknap.github.io/thoth>
Releases        | <https://github.com/gilesknap/thoth/releases>

## What it does

- **Capture from Slack.** Post a link, upload a file or jot a note in a private channel. thoth fetches, transcribes or OCRs it, decides what kind of thing it is, writes a tidy page, links it to related notes and replies in-thread with an `obsidian://` link.
- **Retrieve from Slack.** Ask a question and get a conversational answer grounded in your vault, with a short `Sources:` list of the pages it used.
- **Your knowledge is plain Markdown in git.** The Obsidian vault is the single source of truth. Open it in Obsidian, edit it by hand, grep it, diff it. There is no lock-in.
- **Semantic recall.** A rebuildable vector index ([Hindsight](https://hindsight.vectorize.io)) sits over the vault so retrieval finds things by meaning rather than by keyword alone. The index is disposable and is re-derived from the vault at any time.
- **MCP server.** Seven `pkm_*` tools (`ingest`, `search`, `todos`, `recent`, `write_page`, `read_page`, `edit_page`) let Claude Desktop or any MCP client read and write the same vault.

## How it works

A small set of injected boundaries do the heavy lifting. Claude classifies, curates and answers, Whisper transcribes audio locally, Exa and Firecrawl handle web search and URL to Markdown extraction, Hindsight provides the semantic index, and the Obsidian vault is the canonical store as a two-way-synced git repo.

It runs unattended on a small VPS as a single long-running Slack daemon (`thoth slack`) plus a handful of cron jobs.

```console
$ thoth --version    # confirm the CLI is on your PATH
$ thoth slack        # run the capture/retrieve daemon (Socket Mode)
```

It is built for one person and one vault. There is no multi-user mode and no hosted service, and running it costs you an Anthropic API key of your own.

<!-- README only content. Anything below this line won't be included in index.md -->

## Documentation

Full documentation is published at <https://gilesknap.github.io/thoth>, with the source under [`docs/`](docs/).

The key guides are:

- **Deploy the appliance** is the main path, a dependency-by-dependency setup covering every API key and the `.env`: [`docs/how-to/deploy-appliance.md`](docs/how-to/deploy-appliance.md).
- **Set up the Slack app**: [`docs/how-to/slack-setup.md`](docs/how-to/slack-setup.md).
- **First-light smoke checklist**, to verify each live boundary after a deploy: [`docs/how-to/first-light.md`](docs/how-to/first-light.md).
- **Install for local development**: [`docs/tutorials/installation.md`](docs/tutorials/installation.md).
- **How it works and why**: [`docs/explanations/architecture.md`](docs/explanations/architecture.md) and [`docs/explanations/decisions.md`](docs/explanations/decisions.md).
