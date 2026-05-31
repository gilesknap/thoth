# Set up the Slack app

thoth's primary capture/retrieve surface is a Slack bot (SPEC sections 6, 7, 10): you DM
it a URL, a file, a note, or a question, and it files to (or answers from) your vault. This
guide creates that Slack app **from an embedded manifest**, turns on Socket Mode, and wires
the two tokens thoth reads. It is the prerequisite for {doc}`first-light` §3 (the live DM
round-trip).

thoth connects over **Socket Mode** (an outbound WebSocket), so the app needs *no* public
URL, no inbound webhook, and no request-URL verification -- it runs fine on a VPS behind a
firewall. Two tokens are involved: a **bot token** (`xoxb-…`, the app's identity) and an
**app-level token** (`xapp-…`, scope `connections:write`, which opens the Socket Mode
connection).

## 1. Create the app from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace.
3. Paste the manifest below (JSON tab) and create the app.

```json
{
  "display_information": {
    "name": "Thoth",
    "description": "Your thoth PKM on Slack"
  },
  "features": {
    "bot_user": {
      "display_name": "Thoth",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "chat:write",
        "im:history",
        "files:read"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "message.im",
        "file_shared"
      ]
    },
    "interactivity": {
      "is_enabled": false
    },
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

**Why exactly these scopes/events (no dead scopes).** They are trimmed to what
`thoth.slack_app` actually does, so the consent screen claims nothing the bot does not use:

- `chat:write` — post the reply, and **edit it in place** while a slow capture/answer runs
  (the placeholder → `chat.update` processing feedback, see §5). `chat.update` needs no
  extra scope beyond `chat:write`.
- `im:history` — receive the `message.im` event when you DM the bot (the capture/question).
- `files:read` — download an uploaded file's bytes from its private URL (a DM upload
  arrives as a `message`/`file_share` event carrying the file objects; thoth fetches the
  bytes server-side with an authenticated `GET`).
- event `message.im` — the DM text/upload the bot routes.
- event `file_shared` — Slack also emits this stub for every upload; thoth **acks it as a
  no-op** (it carries only a file id, no download URL or conversation channel), so the
  upload is ingested from the `message`/`file_share` event instead. It is subscribed only
  so Bolt does not log it as unhandled.

The bot does **not** use `im:read`, `im:write`, or `reactions:write` — it never lists or
opens conversations and the baseline feedback edits the message rather than reacting, so
those scopes are deliberately omitted.

## 2. Enable Socket Mode and mint the app-level token

1. **Settings → Socket Mode** → toggle **Enable Socket Mode** on (the manifest already
   requests it; confirm it is on).
2. When prompted (or under **Settings → Basic Information → App-Level Tokens**), generate
   an app-level token with the **`connections:write`** scope. Copy the `xapp-…` value —
   this is `SLACK_APP_TOKEN`.

## 3. Install to the workspace and copy the bot token

1. **Settings → Install App** → **Install to Workspace** → authorise.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) — this is `SLACK_BOT_TOKEN`.

## 4. Set the environment variables thoth reads

thoth reads its configuration from the environment, optionally seeded from
`~/.thoth/.env` (chmod 600). The Slack-related variables (verified against
`src/thoth/config.py`):

| Variable | Required? | What it is |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | yes (for `thoth slack`) | The bot token, `xoxb-…`, from step 3. |
| `SLACK_APP_TOKEN` | yes (for `thoth slack`) | The app-level token, `xapp-…`, scope `connections:write`, from step 2. |
| `SLACK_ALLOWED_USERS` | yes (fail-closed) | Comma/space-separated Slack **user ids** allowed to use the bot. Empty = nobody (fail-closed). |
| `SLACK_SUMMARY_CHANNEL` | only for `thoth summary` | The channel/DM id the daily/weekly digest is posted to. |

`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are both required to start the daemon
(`Config.require_slack` raises naming whichever is missing). `SLACK_ALLOWED_USERS` is
**fail-closed**: an unset/blank value denies everyone, so set it before you expect a reply.
(`SLACK_ALERT_CHANNEL` is an optional unattended-error target; when unset, alerts fall back
to the first id in `SLACK_ALLOWED_USERS` as a DM target — see SPEC section 10.)

Example `~/.thoth/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
SLACK_ALLOWED_USERS=U0123ABCD
SLACK_SUMMARY_CHANNEL=U0123ABCD
```

### Find your Slack user id

The allow-list is keyed by **user id** (`U…`), not by handle. In Slack: click your avatar →
**Profile** → the **⋮** (More) menu → **Copy member ID**. The parser tolerates `@U…` and
`<@U…>` mention wrappers, so a pasted mention works too.

## 5. Processing feedback (what you will see)

A capture is a multi-step chain (`git pull` → classify → extract → curate → Hindsight
retain+probe → commit+push) and can take 5–15s. So the bot posts an **immediate
placeholder** the instant it receives your message and then **edits that same message in
place** with the final result (`chat.update`) — you see a working signal within ~1s rather
than a dead pause:

- a capture shows `⏳ Filing…`, then becomes the filed-page confirmation;
- a question shows `🔎 Looking…`, then becomes the answer with its sources.

This needs no extra scope beyond `chat:write`. (If the edit cannot be performed for any
reason, the bot falls back to posting the reply as a normal message — you always get the
answer.)

## 6. How answers read

Answers come back as clean, conversational prose. The model refers to your pages by
**title** (never a raw file path or a dead-in-Slack `[[wikilink]]`), and every reference
is collected into one concise `Sources:` block at the end — a clickable `obsidian://`
link plus the vault-relative path per page.

For a vault-only question the `Sources:` list shows only the pages the answer actually
**used**, not the whole retrieval candidate set, so the list stays short and honest. (How
many pages were consulted versus used is recorded in the operator logs for tuning recall.)

## 7. Connect and verify

Start the daemon and DM the bot from an allow-listed account:

```console
$ thoth slack
```

Then work through {doc}`first-light` §3 — the live DM round-trip (capture → reply, the
page lands in the vault, a `research:` question answers and offers to save). A `ConfigError`
naming `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` means a token is missing; silence usually means
Socket Mode is off, the app-level token lacks `connections:write`, or the bot is not in the
DM.

## Future: native Slack Assistant status (Slice C — deferred)

Slack offers a **native Assistant API** (scope `assistant:write`; events
`assistant_thread_started` / `assistant_thread_context_changed`; the `assistant_view`
feature) that shows a first-class "thinking…" status via `assistant.threads.setStatus`.
Adopting it would move the interaction into Slack's Assistant pane/thread model rather than
plain DMs — a real **interaction-surface change**, so it is intentionally **out of scope**
here and needs the owner's sign-off before adoption (tracked under issue #34, Slice C). The
placeholder + `chat.update` feedback above is the recommended baseline and needs none of
those scopes.
