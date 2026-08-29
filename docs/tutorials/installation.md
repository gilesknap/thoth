# Installation

This tutorial gets the `thoth` CLI onto a machine for local use or development.

To stand up the unattended appliance, meaning the Slack daemon, the semantic index, systemd, cron and every API key, follow the {doc}`../how-to/deploy-appliance` how-to instead. This page is the lightweight, single-machine path.

thoth is not published to PyPI. You install it from the git repository with [`uv`](https://docs.astral.sh/uv/).

## Prerequisites

- **Python 3.11 or later.** Check with:

  ```console
  $ python3 --version
  ```

- **[`uv`](https://docs.astral.sh/uv/)**, the project's environment and dependency manager:

  ```console
  $ curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **`ffmpeg`**, *only* if you want local audio transcription with Whisper. On Debian and Ubuntu that is `sudo apt-get install -y ffmpeg`. You can skip it, because thoth raises a clear error if you try to transcribe without it.

## Clone and install

```console
$ git clone https://github.com/gilesknap/thoth.git
$ cd thoth
$ uv sync --extra runtime
```

`uv sync` creates a `.venv/` and installs thoth as an editable install.

The base install is import-safe and dependency-light. The `runtime` extra adds the live clients (`anthropic`, `slack-bolt`, `firecrawl-py`, `mcp`) that the appliance needs at run time but that CI does not install, so omit `--extra runtime` for a docs-only or test-only checkout.

Confirm the CLI is on your path:

```console
$ uv run thoth --version
```

Activate the venv with `source .venv/bin/activate` if you prefer to call `thoth` directly.

## What you get

`thoth --help` lists the subcommands. These are the ones you will use most:

| Command | What it does |
| --- | --- |
| `thoth slack` | Run the capture and retrieve daemon in Socket Mode, the appliance's only long-running process. |
| `thoth mcp --transport http` | Serve the `pkm_*` tools over the bearer-authenticated MCP HTTP socket for Claude Code and claude.ai (see {doc}`../how-to/mcp-server-setup`). |
| `thoth reindex [--full-rebuild]` | Build or rebuild the Hindsight semantic index from the vault. |
| `thoth summary daily\|weekly` | Compose and post the digest to the Slack summary channel. |

All of these need configuration: a vault, an Anthropic key, Slack tokens and so on.

For a quick local poke you can point `PKM_VAULT` at a throwaway directory. For the real, unattended setup, continue to {doc}`../how-to/deploy-appliance`.

## Next steps

- {doc}`../how-to/deploy-appliance` for the full production install on a VPS.
- {doc}`../how-to/slack-setup` to create the Slack app and wire the tokens.
- {doc}`../how-to/first-light` to verify every live boundary once deployed.
