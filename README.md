# gchat-bot

Google Chat bot webhook (Flask) that:

- receives Chat events at /chat
- verifies Google Chat bearer tokens
- creates and activates a fresh thread in the current Space when a user sends `/new`
- downloads Drive, Google Chat media, and GIF attachments from incoming messages
- uses the OpenClaw HTTP provider by default for every session model
- renders markdown-like responses into Google Chat cards

License: GNU GPL v3.0 (see LICENSE).

## Project Files

- app.py: main webhook server
- helpers/md_to_gchat.py: markdown -> Google Chat card widgets
- helpers/outbound_attachment_watcher.py: durable inotify outbox watcher
- helpers/chat_target_store.py: fixed Google Chat destination persistence
- helpers/token_tools/get_token.py: OAuth token helper via local callback server
- helpers/token_tools/get_token_manual.py: OAuth token helper via manual redirect URL paste
- helpers/token_tools/manual_token.py: compatibility alias for the manual helper

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

1. Bot service account credentials (for Google Chat messaging)
2. User OAuth token (for Google Drive access)

Expected default files in project root:

- credentials.json (service account)
- client_secret.json (OAuth client)
- token.json (user OAuth token)

The token helpers request these scopes:

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/drive.file`

Generate the token with the local callback helper:

```bash
python helpers/token_tools/get_token.py
```

If the machine has no runnable browser, `get_token.py` switches to the manual
redirect flow automatically. It can also be started directly:

```bash
python helpers/token_tools/get_token_manual.py
```

`/new` does not use the user OAuth token. It posts a root message as the bot in
the current Space, so no Chat API user scopes or Workspace administrator
approval are required. A previously generated `token.json` that also contains
the old Chat grants remains usable; it does not need to be regenerated solely
for this change.

## Environment Variables

Required:

- GCHAT_AUDIENCE: exact Chat webhook URL audience (recommended), example: https://your-domain/chat

Optional:

- GCHAT_PROJECT_NUMBER: Google Cloud Project Number fallback audience
- GCHAT_BOT_CRED: path to service account credentials file (default: ./credentials.json)
- GCHAT_TOKEN_FILE: path to user OAuth token file (default: ./token.json)
- MAX_ATTACHMENT_BYTES: max bytes per downloaded attachment (default: 20971520)
- MAX_ATTACHMENTS_PER_MESSAGE: max incoming files processed per message (default: 8)
- MAX_OUTBOUND_ATTACHMENT_BYTES: max size of one outbound file
  (default: 20971520)
- DRIVE_UPLOAD_FOLDER_ID: Drive folder used for Jinx file-preview cards
  (the OAuth identity must be able to write to it, and the folder must already
  be shared with the intended Chat recipients)
- GCHAT_OUTBOUND_SPACE and GCHAT_OUTBOUND_THREAD: immutable destination for
  watched files. When set, `/new` cannot create and activate a different thread.
  Omit both to let the first authenticated message establish the destination
  and let `/new` rotate the active thread later.
- GCHAT_OUTBOUND_TARGET_FILE: persisted learned destination (default:
  `~/.openclaw/state/jinx-gchat/target.json`)
- JINX_OUTBOUND_STATE_DIR: SQLite ledger, process lock, and private staging root
  (default: `~/.openclaw/state/jinx-gchat`)
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
`/new` reads the current session's effective model, creates a root message in
the current Google Chat Space, activates the returned thread, and then
creates a new OpenClaw session with the same model. If session creation fails,
the persisted Chat target is rolled back and the existing session remains
active.

`/model <model-key>` creates only a new OpenClaw session with the requested
model in the current Space. Session rotation is serialized with active message
processing: `/new` receives the busy response while a turn is running; use
`/abort`, wait for that turn to release, then retry `/new`.

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
- ensure the bot is installed in the Space and can post messages there; `/new`
  uses the existing bot authentication to create a new root thread

## Security Notes

- Do not commit credentials.json, token.json, or client_secret.json.
- .gitignore already excludes these sensitive files.
- OAuth helpers write `token.json` atomically with mode `0600`.
- Incoming /chat requests are rejected unless JWT verification passes.
- Attachment and image sizes are capped to reduce abuse and memory pressure.
- Outbound sending accepts only private staged copies made from configured local
  output directories. Symlinks, directories, hidden/temporary files, and nested
  paths are not sent.
- The learned Chat destination remains fixed until `/new` explicitly activates
  the newly created thread. Requests from other threads in the active
  Space are accepted without changing the destination; requests from any other
  Space are rejected. Explicit `GCHAT_OUTBOUND_SPACE` and
  `GCHAT_OUTBOUND_THREAD` disable this rotation and avoid first-message
  destination claiming in deployments where the app is installed in more than
  one Space. Keep those two variables set consistently; if switching back to
  learned mode, reset `GCHAT_OUTBOUND_TARGET_FILE` deliberately so an older
  target cannot become active again.
- The bot identity authors the Chat card, but Drive access still follows the
  configured folder's sharing policy; posting a card does not grant Drive access.

## Known Behavior

- Agent requests are submitted through OpenClaw HTTP, but the HTTP response text
  is never posted to Google Chat. A background watcher tails the active session's
  trajectory under `~/.openclaw/agents/main/sessions` and posts each completed
  assistant message to the fixed Google Chat target.
- Existing trajectory history is baselined when a session is first watched and
  is not replayed. `/new` switches both the watcher session and active Chat
  thread in the same Space; `/model` switches only the watcher session.
- Trajectory delivery uses a stable Google Chat request ID for each source line,
  so a retry does not intentionally create a second Chat message.
- Attachments are saved using the MIME type to determine file extension.
- Incoming files are downloaded to `/home/arme/.openclaw/workspace/downloads`.
  Jinx remains silent when attachment handling succeeds and reports only limits,
  skipped files, download failures, or provider-access failures. Agent dispatch
  is asynchronous (OpenClaw reads the downloaded `localPath` on its own
  schedule), so downloaded files are not deleted after a turn is dispatched;
  they accumulate in that directory and need external retention/cleanup.
- Outbound deliverables are detected automatically only when a completed file
  appears directly under `~/.openclaw/workspace/uploads`. A completed assistant
  message can also explicitly attach any file under the home directory or `/tmp`
  with a full line such as
  `MEDIA:/home/arme/.openclaw/media/tool-image-generation/image-1.png`
  (the resolved path must stay inside those roots; a symlink anywhere along the
  path - including an intermediate directory, not just the final component - is
  rejected outright, so a symlinked folder under the home directory cannot be
  used in a `MEDIA:` reference even if it points somewhere safe. The bot's own
  credentials, OAuth token, session key, and internal state/ledger directory are
  always excluded, regardless of where they live). The
  directive line is removed from the Google Chat text, and the referenced file
  enters the same durable staging, retry, and delivery pipeline. Repeated paths
  in one message are deduplicated; inline `MEDIA:` text is left unchanged.
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
- A completed provider run with no assistant text produces no provider text
  message. A reply containing only valid `MEDIA:` lines still submits those files
  without posting an empty Chat message.
- Jinx administrator cards use an error icon in the header when the message is
  an error; informational and operational administrator cards keep the normal
  administrator icon.
- OpenClaw HTTP is the only provider for normal agent requests and uses the
  persisted session key. Model selection remains independent of the transport.
- Only completed trajectory messages are sent; incremental streaming deltas and
  the OpenClaw HTTP response body are ignored.
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

2. `/new` cannot create or activate a thread

- unset both `GCHAT_OUTBOUND_SPACE` and `GCHAT_OUTBOUND_THREAD`; a configured
  target is intentionally immutable
- verify the bot is already installed in the current Space and can post a root
  message there
- inspect the Google Chat API error in the service log

3. Drive download fails

- regenerate token.json with both configured Drive scopes
- verify attachment file is accessible by the authenticated user

4. Module not found errors

- install dependencies in the same Python environment used to run app.py
