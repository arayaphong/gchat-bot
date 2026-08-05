# gchat-bot

Google Chat bot webhook (Flask) that:
- receives Chat events at /chat
- verifies Google Chat bearer tokens
- downloads Drive, Google Chat media, and GIF attachments from incoming messages
- uses the OpenClaw HTTP provider by default for every session model
- renders markdown-like responses into Google Chat cards

License: GNU GPL v3.0 (see LICENSE).

## Project Files

- app.py: main webhook server
- helpers/md_to_gchat.py: markdown -> Google Chat card widgets
- helpers/outbound_attachment_watcher.py: durable inotify outbox watcher
- helpers/chat_target_store.py: fixed Google Chat destination persistence
- helpers/get_token.py: OAuth token helper via local callback server
- helpers/get_token_manual.py: OAuth token helper via manual redirect URL paste
- helpers/manual_token.py: one-off token fetch script with hardcoded code value

## Requirements

Python 3.10+ on Linux is required. The bot sends normal agent requests directly
to the OpenClaw HTTP endpoint. The `openclaw` CLI must also be available for
`/models`, `/abort`, and session administration commands. Outbound file delivery
uses Linux inotify through `inotify-simple`.

Install dependencies:

```fish
python -m pip install -r requirements.txt
```

## Credentials and Tokens

This project uses two auth paths:

1. Bot service account credentials (for Google Chat bot send API)
2. User OAuth token (for Google Drive download and outbound file upload)

Expected default files in project root:
- credentials.json (service account)
- client_secret.json (OAuth client)
- token.json (user OAuth token)

Generate token.json with either:

```bash
python helpers/get_token.py
```

or

```bash
python helpers/get_token_manual.py
```

## Environment Variables

Required:
- GCHAT_AUDIENCE: exact Chat webhook URL audience (recommended), example: https://your-domain/chat

Optional:
- GCHAT_PROJECT_NUMBER: Google Cloud Project Number fallback audience
- GCHAT_BOT_CRED: path to service account credentials file (default: ./credentials.json)
- GCHAT_TOKEN_FILE: path to user OAuth token file (default: ./token.json)
- MAX_ATTACHMENT_BYTES: max bytes per downloaded attachment (default: 20971520)
- MAX_ATTACHMENTS_PER_MESSAGE: max incoming files processed per message (default: 8)
- MAX_OUTBOUND_ATTACHMENT_BYTES: max size of one watched outbound file
  (default: 20971520)
- DRIVE_UPLOAD_FOLDER_ID: Drive folder used for Jinx file-preview cards
  (the OAuth identity must be able to write to it, and the folder must already
  be shared with the intended Chat recipients)
- GCHAT_OUTBOUND_SPACE and GCHAT_OUTBOUND_THREAD: fixed destination for watched
  files. Set both together in production. If omitted, the first authenticated
  Chat message fixes the destination.
- GCHAT_OUTBOUND_TARGET_FILE: persisted learned destination (default:
  `~/.openclaw/state/jinx-gchat/target.json`)
- JINX_OUTBOUND_STATE_DIR: SQLite ledger, process lock, and private staging root
  (default: `~/.openclaw/state/jinx-gchat`)
- GCHAT_PROVIDER: agent transport; only `openclaw` is supported (default: openclaw)
- OPENCLAW_GATEWAY_TOKEN: gateway token, useful when connecting through a remote relay
- OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS: whether the active provider can read the
  bot's local attachment paths (`auto`, `allow`, or `deny`; default: `auto`).
  Auto allows loopback endpoints only. Use `allow` only when a remote provider
  has the same absolute download directory mounted.
- OPENCLAW_CONFIG_FILE: OpenClaw config file (default: ~/.openclaw/openclaw.json)

The gateway token is read from `OPENCLAW_GATEWAY_TOKEN` first. If it is unset,
the provider loads `gateway.auth.token` from the OpenClaw config file.
The OpenClaw Gateway must expose `sessions.create` with initial `model` support
for `/new` and `/model <model-key>`.

Session keys are generated only as `agent:main:gchat:<uuid-6-hex>` and persisted
in `./session_key` when that file is missing or empty. The fixed
`agent:main:gchat:jinx` fallback and the `OPENCLAW_AGENT` /
`OPENCLAW_SESSION_KEY` overrides are no longer generated or used as defaults.
`/new` reads the current session's effective model and creates a new OpenClaw
session with that same model before persisting its new key. `/model <model-key>`
does the same with the requested model. If session creation fails, the existing
persisted session key remains active.

## Run

```bash
python app.py
```

On startup, the app prints GPL notice text and starts on:
- host: 0.0.0.0
- port: 8080

Endpoints:
- POST /chat
- GET /

## Google Chat Configuration Notes

In Google Chat API / Chat app settings:
- set bot endpoint URL to your public /chat URL
- ensure authentication token header is sent (Authorization: Bearer ...)
- use the same GCP project as GCHAT_PROJECT_NUMBER

## Security Notes

- Do not commit credentials.json, token.json, or client_secret.json.
- .gitignore already excludes these sensitive files.
- Incoming /chat requests are rejected unless JWT verification passes.
- Attachment and image sizes are capped to reduce abuse and memory pressure.
- Outbound sending accepts only private staged copies made from the two watched
  directories. Symlinks, directories, hidden/temporary files, and nested paths
  are not sent.
- The first learned Chat destination remains the fixed outbound file thread.
  Requests from other threads in the same space are accepted without changing
  that destination; requests from a different space are rejected. Explicit
  `GCHAT_OUTBOUND_SPACE` and
  `GCHAT_OUTBOUND_THREAD` configuration avoids first-message destination
  claiming in deployments where the app is installed in more than one space.
  Keep those two variables set consistently; if switching back to learned mode,
  reset `GCHAT_OUTBOUND_TARGET_FILE` deliberately so an older target cannot
  become active again.
- The bot identity authors the Chat card, but Drive access still follows the
  configured folder's sharing policy; posting a card does not grant Drive access.

## Known Behavior

- If model response is fast, reply returns inline.
- If model response exceeds timeout, bot posts follow-up message in thread asynchronously.
- Attachments are saved using the MIME type to determine file extension.
- Incoming files are downloaded to `/home/arme/.openclaw/workspace/downloads`.
  Jinx remains silent when attachment handling succeeds and reports only limits,
  skipped files, download failures, provider-access failures, or cleanup failures.
  Temporary files are removed after provider processing.
- Outbound deliverables are detected automatically when a completed file appears
  directly under either `~/.openclaw/workspace/uploads` or
  `~/.openclaw/media/tool-image-generation`. The watcher responds to completed
  writes and atomic renames; `[[ATTACH:...]]`, `[[FILE:...]]`, and outbound file
  tool calls are no longer interpreted.
- Existing files are baselined on the first watcher startup and are not sent.
  A durable startup cutover preserves this rule across an interrupted first
  launch. Later restarts reconcile files created while the bot was offline.
  SQLite state prevents duplicate event delivery and preserves pending retries.
- Each successfully detected file is copied to private staging, uploaded to
  Drive, and posted as a preview card by the Jinx bot identity. The private copy
  is removed after success; the original file remains producer-owned.
- Normal provider text is delivered first when a provider request is active.
  Files are separate asynchronous Jinx messages. Jinx emits no extra success
  notice and notifies the user only after delivery retries are exhausted or a
  completed file is empty, oversized, unreadable, unstable, or disappears before
  staging. Once an upload receipt is recorded, retries reuse it; a stable Google
  Chat request ID also makes repeated card-create requests idempotent.
- A completed provider run with no assistant text is shown as an informational
  provider message, not as a Jinx administrator error; any watched file still
  follows the normal outbound delivery path.
- Jinx administrator cards use an error icon in the header when the message is
  an error; informational and operational administrator cards keep the normal
  administrator icon.
- OpenClaw HTTP is the only provider for normal agent requests and uses the
  persisted session key. Model selection remains independent of the transport.
- OpenClaw HTTP returns one final reply to Google Chat; the current Chat transport
  does not edit messages live.
- `/model <model-key>` is checked against `openclaw models list --json` before it
  reaches the gateway. An exact key with `available: true` and without
  `missing: true` starts a fresh session through `sessions.create`, with the model
  selected atomically at session creation. The previous conversation context is
  not carried over. If creation or local key persistence fails, Jinx reports the
  error, keeps the existing session current, and does not fall back to changing
  that session's model.

## Troubleshooting

1. 401 unauthorized on /chat
- verify GCHAT_AUDIENCE matches the exact endpoint URL configured in Google Chat
- confirm Chat app is calling this endpoint and includes Authorization header

2. Drive download fails
- regenerate token.json with Drive scope
- verify attachment file is accessible by the authenticated user

3. Module not found errors
- install dependencies in the same Python environment used to run app.py
