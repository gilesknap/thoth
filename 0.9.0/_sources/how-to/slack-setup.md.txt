# Set up the Slack app

thoth's primary capture/retrieve surface is a Slack bot (SPEC sections 6, 7, 10): in **one
dedicated private channel** (just you and the bot) you post a URL, a file, a note, or a
question, and it files to (or answers from) your vault — each post handled in **its own
thread** (issue #61). This guide creates that Slack app **from an embedded manifest**,
turns on Socket Mode, wires the two tokens thoth reads, and points thoth at the capture
channel. It is the prerequisite for {doc}`first-light` §3 (the live round-trip).

thoth connects over **Socket Mode** (an outbound WebSocket), so the app needs *no* public
URL, no inbound webhook, and no request-URL verification -- it runs fine on a VPS behind a
firewall. Two tokens are involved: a **bot token** (`xoxb-…`, the app's identity) and an
**app-level token** (`xapp-…`, scope `connections:write`, which opens the Socket Mode
connection).

**Why a private channel?** A private channel with just you and the bot renders the same
across mobile and web, gives a clean per-topic timeline, and lets per-conversation state be
keyed by **thread** — so two interleaved topics never clobber each other's "reply *y* to
save" (issue #61).

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
        "groups:history",
        "groups:read",
        "files:read"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "message.groups",
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

- `chat:write` — post the reply (in-thread), and **edit it in place** while a slow
  capture/answer runs (the placeholder → `chat.update` processing feedback, see §6).
  `chat.update` needs no extra scope beyond `chat:write`.
- `groups:history` — receive the `message.groups` event for messages in the **private
  channel** the bot is invited to (the capture/question/reply).
- `groups:read` — read the private channel's basic metadata (needed alongside
  `groups:history` to subscribe to its message events).
- `files:read` — download an uploaded file's bytes from its private URL (an upload arrives
  as a `message`/`file_share` event carrying the file objects; thoth fetches the bytes
  server-side with an authenticated `GET`).
- event `message.groups` — the private-channel text/upload/thread-reply the bot routes.
- event `file_shared` — Slack also emits this stub for every upload; thoth **acks it as a
  no-op** (it carries only a file id, no download URL or conversation channel), so the
  upload is ingested from the `message`/`file_share` event instead. It is subscribed only
  so Bolt does not log it as unhandled.

The bot does **not** use `im:*` (it has no DM surface), `channels:*` (it lives in a
*private* channel, not public ones), `reactions:write` (the baseline feedback edits the
message rather than reacting), or `assistant:write` (no Slack Assistant pane), so those
scopes are deliberately omitted.

## 2. Enable Socket Mode and mint the app-level token

1. **Settings → Socket Mode** → toggle **Enable Socket Mode** on (the manifest already
   requests it; confirm it is on).
2. When prompted (or under **Settings → Basic Information → App-Level Tokens**), generate
   an app-level token with the **`connections:write`** scope. Copy the `xapp-…` value —
   this is `SLACK_APP_TOKEN`.

## 3. Install to the workspace and copy the bot token

1. **Settings → Install App** → **Install to Workspace** → authorise.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) — this is `SLACK_BOT_TOKEN`.

## 4. Create the private capture channel and invite the bot

thoth listens and replies in **one dedicated private channel** (issue #61) — it ignores
every other conversation it happens to be in. Create it and add the bot:

1. In Slack, **create a private channel** (e.g. `#thoth`) — *Create channel*, then set it
   **Private**. Keep it to just you and the bot; this is your capture/retrieve surface.
2. **Invite the bot**: in that channel, type `/invite @Thoth` (or *channel name → Integrations
   → Add apps*).
3. **Copy the channel id** (a `C…` / `G…` id, *not* the `#name`): click the channel name →
   scroll to the bottom of the **About** tab → **Copy** the channel ID. This is
   `SLACK_CAPTURE_CHANNEL`.

The bot replies **in a thread** under each message you post, so a capture/answer and its
"reply *y* to save" confirmation stay together per topic. A `y` typed at the channel top
level (not in the thread) is treated as a new message and will not confirm — reply *in the
thread* to save.

## 5. Set the environment variables thoth reads

thoth reads its configuration from the environment, optionally seeded from
`~/.thoth/.env` (chmod 600). The Slack-related variables (verified against
`src/thoth/config/`):

| Variable | Required? | What it is |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | yes (for `thoth slack`) | The bot token, `xoxb-…`, from step 3. |
| `SLACK_APP_TOKEN` | yes (for `thoth slack`) | The app-level token, `xapp-…`, scope `connections:write`, from step 2. |
| `SLACK_CAPTURE_CHANNEL` | yes (for `thoth slack`) | The private channel id (`C…`/`G…`) the bot listens/replies in, from step 4. |
| `SLACK_ALLOWED_USERS` | yes (fail-closed) | Comma/space-separated Slack **member ids** (`U…`, not a `D…`/`C…` id) allowed to use the bot. Empty = nobody (fail-closed). |
| `SLACK_SUMMARY_CHANNEL` | only for `thoth summary` | The channel/DM id the daily/weekly digest is posted to. |

`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_CAPTURE_CHANNEL` are all required to start
the daemon — `Config.require_slack` raises naming whichever token is missing, and
`Config.require_slack_capture_channel` raises if the channel is unset (a pure cutover: there
is no DM fallback, so the daemon fails fast rather than listen nowhere). `SLACK_ALLOWED_USERS`
is **fail-closed**: an unset/blank value denies everyone, so set it before you expect a reply
(in a two-member private channel it is largely moot, but kept). (`SLACK_ALERT_CHANNEL` is an
optional unattended-error target; when unset, alerts fall back to the first id in
`SLACK_ALLOWED_USERS` as a DM target — see SPEC section 10.)

Example `~/.thoth/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
SLACK_CAPTURE_CHANNEL=C0123CAPTURE
SLACK_ALLOWED_USERS=U0123ABCD
SLACK_SUMMARY_CHANNEL=U0123ABCD
```

### Find your Slack user id

The allow-list is keyed by your **user (member) id**, which always starts with **`U`** (a
`U…` value like `U0123ABCD`). In Slack: click your avatar → **Profile** → the **⋮** (More)
menu → **Copy member ID**. The parser tolerates `@U…` and `<@U…>` mention wrappers, so a
pasted mention works too.

**It must be a `U…` member id, not a conversation id.** A `D…` (direct-message), `C…` /
`G…` (channel), or a display name / `@handle` will **never** match the id Slack puts on your
message, so every message is refused with *"Sorry, you are not authorised to use this
assistant."* even though the daemon is otherwise healthy. (`D…` ids are an easy slip — they
look channel-ish, and a DM id may be lying around from `SLACK_CAPTURE_CHANNEL` /
`SLACK_SUMMARY_CHANNEL`.) If you hit a persistent "not authorised", check the **`U`** prefix
first.

No GUI handy? Ask Slack for the capture channel's members from the box (you + the bot — the
human `U…` is yours):

```bash
curl -s "https://slack.com/api/conversations.members?channel=$SLACK_CAPTURE_CHANNEL" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN"
```

## 6. Processing feedback (what you will see)

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

## 7. How answers read

Answers come back as clean, conversational prose. The model refers to your pages by
**title** (never a raw file path or a dead-in-Slack `[[wikilink]]`), and every reference
is collected into one concise `Sources:` block at the end — a clickable `obsidian://`
link plus the vault-relative path per page.

For a vault-only question the `Sources:` list shows only the pages the answer actually
**used**, not the whole retrieval candidate set, so the list stays short and honest. (How
many pages were consulted versus used is recorded in the operator logs for tuning recall.)

## 8. Connect and verify

Start the daemon, then post in the capture channel from an allow-listed account:

```console
$ thoth slack
```

Then work through {doc}`first-light` §3 — the live round-trip (post in the channel →
threaded reply, the page lands in the vault, a `research:` question answers and offers to
save *in the thread*). A `ConfigError` naming `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` /
`SLACK_CAPTURE_CHANNEL` means that variable is missing; silence usually means Socket Mode is
off, the app-level token lacks `connections:write`, the bot was not `/invite`d to the
channel, or `SLACK_CAPTURE_CHANNEL` points at a different channel than the one you posted in.
