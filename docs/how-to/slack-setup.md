# Set up the Slack app

thoth's primary capture and retrieve surface is a Slack bot (SPEC sections 6, 7, 10). In one dedicated private channel, holding just you and the bot, you post a URL, a file, a note or a question, and thoth files it to your vault or answers from it. Each post is handled in its own thread (issue #61).

This guide creates that Slack app from an embedded manifest, turns on Socket Mode, wires the two tokens thoth reads, and points thoth at the capture channel. It is the prerequisite for {doc}`first-light` section 3, the live round-trip.

thoth connects over Socket Mode, which is an outbound WebSocket, so the app needs no public URL, no inbound webhook and no request-URL verification. It runs fine on a VPS behind a firewall.

Two tokens are involved: a bot token (`xoxb-…`) which is the app's identity, and an app-level token (`xapp-…`, scope `connections:write`) which opens the Socket Mode connection.

A private channel with just you and the bot renders the same across mobile and web, gives a clean per-topic timeline, and lets per-conversation state be keyed by thread, so two interleaved topics never clobber each other (issue #61).

## 1. Create the app from the manifest

1. Go to <https://api.slack.com/apps>, then **Create New App**, then **From an app manifest**.
2. Pick your workspace.
3. Paste the manifest below into the JSON tab and create the app.

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

The scopes and events are trimmed to what `thoth.slack_app` actually does, so the consent screen claims nothing the bot does not use:

- `chat:write` posts the reply in-thread, and edits it in place while a slow capture or answer runs. That is the placeholder followed by a `chat.update` for processing feedback, described in section 6, and `chat.update` needs no extra scope beyond `chat:write`.
- `groups:history` receives the `message.groups` event for messages in the private channel the bot is invited to, meaning your captures, questions and replies.
- `groups:read` reads the private channel's basic metadata, which is needed alongside `groups:history` to subscribe to its message events.
- `files:read` downloads an uploaded file's bytes from its private URL. An upload arrives as a `message` or `file_share` event carrying the file objects, and thoth fetches the bytes server-side with an authenticated `GET`.
- The `message.groups` event carries the private-channel text, upload or thread reply that the bot routes.
- The `file_shared` event is a stub Slack also emits for every upload. thoth acks it as a no-op, because it carries only a file id with no download URL or conversation channel, so the upload is ingested from the `message` or `file_share` event instead. It is subscribed only so that Bolt does not log it as unhandled.

The bot deliberately omits four scopes it has no use for: `im:*` because it has no DM surface, `channels:*` because it lives in a private channel rather than a public one, `reactions:write` because the baseline feedback edits the message rather than reacting, and `assistant:write` because there is no Slack Assistant pane.

## 2. Enable Socket Mode and mint the app-level token

1. Go to **Settings → Socket Mode** and toggle **Enable Socket Mode** on. The manifest already requests it, so confirm it is on.
2. When prompted, or under **Settings → Basic Information → App-Level Tokens**, generate an app-level token with the `connections:write` scope. Copy the `xapp-…` value, which is `SLACK_APP_TOKEN`.

## 3. Install to the workspace and copy the bot token

1. Go to **Settings → Install App**, then **Install to Workspace**, then authorise.
2. Copy the **Bot User OAuth Token** (`xoxb-…`), which is `SLACK_BOT_TOKEN`.

## 4. Create the private capture channel and invite the bot

thoth listens and replies in one dedicated private channel (issue #61), and ignores every other conversation it happens to be in. Create it and add the bot:

1. In Slack, create a private channel, `#thoth` for example. Use *Create channel*, then set it to **Private**. Keep it to just you and the bot, because this is your capture and retrieve surface.
2. Invite the bot by typing `/invite @Thoth` in that channel, or through *channel name → Integrations → Add apps*.
3. Copy the channel id, which is a `C…` or `G…` id rather than the `#name`. Click the channel name, scroll to the bottom of the **About** tab, and click **Copy** on the channel ID. This is `SLACK_CAPTURE_CHANNEL`.

The bot replies in a thread under each message you post, so a capture or answer stays together with its topic.

## 5. Set the environment variables thoth reads

thoth reads its configuration from the environment, optionally seeded from `~/.thoth/.env` at chmod 600.

The Slack-related variables, verified against `src/thoth/config/`, are:

| Variable | Required? | What it is |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | yes (for `thoth slack`) | The bot token, `xoxb-…`, from step 3. |
| `SLACK_APP_TOKEN` | yes (for `thoth slack`) | The app-level token, `xapp-…`, scope `connections:write`, from step 2. |
| `SLACK_CAPTURE_CHANNEL` | yes (for `thoth slack`) | The private channel id (`C…` or `G…`) the bot listens and replies in, from step 4. |
| `SLACK_ALLOWED_USERS` | yes (fail-closed) | Comma or space separated Slack **member ids** (`U…`, not a `D…` or `C…` id) allowed to use the bot. Empty means nobody. |
| `SLACK_SUMMARY_CHANNEL` | only for `thoth summary` | The channel or DM id the daily and weekly digest is posted to. |

`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_CAPTURE_CHANNEL` are all required to start the daemon. `Config.require_slack` raises naming whichever token is missing, and `Config.require_slack_capture_channel` raises if the channel is unset. This is a pure cutover with no DM fallback, so the daemon fails fast rather than listening nowhere.

`SLACK_ALERT_CHANNEL` is an optional unattended-error target. When it is unset, alerts fall back to the first id in `SLACK_ALLOWED_USERS` as a DM target, as described in SPEC section 10.

```{warning}
`SLACK_ALLOWED_USERS` is fail-closed. An unset or blank value denies everyone, so set it
before you expect a reply. In a two-member private channel it is largely moot, but it is
kept.
```

Example `~/.thoth/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
SLACK_CAPTURE_CHANNEL=C0123CAPTURE
SLACK_ALLOWED_USERS=U0123ABCD
SLACK_SUMMARY_CHANNEL=U0123ABCD
```

### Find your Slack user id

The allow-list is keyed by your member id, which always starts with `U`, giving a value like `U0123ABCD`.

In Slack, click your avatar, then **Profile**, then the **⋮** (More) menu, then **Copy member ID**. The parser tolerates `@U…` and `<@U…>` mention wrappers, so a pasted mention works too.

```{warning}
It must be a `U…` member id and not a conversation id. A `D…` direct-message id, a `C…` or
`G…` channel id, or a display name or `@handle` will never match the id Slack puts on your
message, so every message is refused with *"Sorry, you are not authorised to use this
assistant."* even though the daemon is otherwise healthy.

`D…` ids are an easy slip, because they look channel-ish and one may be lying around from
`SLACK_CAPTURE_CHANNEL` or `SLACK_SUMMARY_CHANNEL`. If you hit a persistent "not
authorised", check the `U` prefix first.
```

With no GUI handy, ask Slack for the capture channel's members from the box. There are two, you and the bot, and the human `U…` is yours:

```bash
curl -s "https://slack.com/api/conversations.members?channel=$SLACK_CAPTURE_CHANNEL" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN"
```

## 6. Processing feedback (what you will see)

A capture is a multi-step chain of `git pull`, classify, extract, curate, Hindsight retain and probe, then commit and push, and it can take 5 to 15 seconds.

So the bot posts an immediate placeholder the instant it receives your message, then edits that same message in place with the final result using `chat.update`. You see a working signal within about a second rather than a dead pause:

- a capture shows `⏳ Filing…`, then becomes the filed-page confirmation.
- a question shows `🔎 Looking…`, then becomes the answer with its sources.

This needs no extra scope beyond `chat:write`. If the edit cannot be performed for any reason, the bot falls back to posting the reply as a normal message, so you always get the answer.

## 7. How answers read

Answers come back as clean, conversational prose. The model refers to your pages by title, never a raw file path and never a `[[wikilink]]`, which is dead in Slack.

Every reference is collected into one concise `Sources:` block at the end, giving a clickable `obsidian://` link plus the vault-relative path per page.

For a vault-only question the `Sources:` list shows only the pages the answer actually used rather than the whole retrieval candidate set, so the list stays short and honest. How many pages were consulted against how many were used is recorded in the operator logs, for tuning recall.

## 8. Connect and verify

Start the daemon, then post in the capture channel from an allow-listed account:

```console
$ thoth slack
```

Then work through {doc}`first-light` section 3, the live round-trip, where you post in the channel, get a threaded reply, watch the page land in the vault, and ask a `research:` question.

A `ConfigError` naming `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` or `SLACK_CAPTURE_CHANNEL` means that variable is missing.

Silence usually means one of four things: Socket Mode is off, the app-level token lacks `connections:write`, the bot was not `/invite`d to the channel, or `SLACK_CAPTURE_CHANNEL` points at a different channel than the one you posted in.
