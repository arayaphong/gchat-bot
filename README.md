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
- gunicorn.conf.py: production single-worker lifecycle configuration
- helpers/chat_card_markdown_parser.py: markdown -> Google Chat card widgets
- helpers/outbound_attachment_watcher.py: durable inotify outbox watcher
- helpers/chat_target_store.py: fixed Google Chat destination persistence
- helpers/token_tools/get_token.py: OAuth token helper via local callback server
- helpers/token_tools/get_token_manual.py: OAuth token helper via manual redirect paste
- scripts/chat_history_admin.py: sanitized preflight/diagnostics/backup/restore CLI
- docs/chat-history-runbook.md: production operations and recovery runbook

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

Generate token.json from the repository root with either:

```bash
python helpers/token_tools/get_token.py
```

or

```bash
python helpers/token_tools/get_token_manual.py
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
- MAX_OUTBOUND_ATTACHMENT_BYTES: max size of one outbound file
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
- OPENCLAW_GATEWAY_TOKEN: gateway token, useful when connecting through a remote relay
- OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS: whether the active provider can read the
  bot's local attachment paths (`auto`, `allow`, or `deny`; default: `auto`).
  Auto allows loopback endpoints only. Use `allow` only when a remote provider
  has the same absolute download directory mounted.
- OPENCLAW_CONFIG_FILE: OpenClaw config file (default: ~/.openclaw/openclaw.json)
- GCHAT_BIND: production Gunicorn bind address (default: `127.0.0.1:8080`)
- GCHAT_HTTP_THREADS: threads in the one production worker (default: `8`)

Chat history variables (all boolean values must be exactly `true` or `false`):

- `GCHAT_HISTORY_ENABLED`: reserve and enable `/chat`; default `false`
- `GCHAT_HISTORY_DELETE_ENABLED`: enable preview/confirm/delete; default `false`
  and requires history to be enabled
- `GCHAT_HISTORY_ALLOWED_USER`: one canonical `users/...` identity; required when
  history is enabled
- `GCHAT_HISTORY_ALLOWED_SPACE`: one canonical `spaces/...` DM; required when
  history is enabled and must match `GCHAT_OUTBOUND_SPACE` if both are set
- `GCHAT_CARD_ACTION_URL`: full public HTTPS callback endpoint, normally the
  configured `/chat` URL; required only when delete is enabled
- `JINX_CHAT_HISTORY_STATE_DIR`: persistent local SQLite state directory
  (default `~/.openclaw/state/jinx-gchat/chat-history`; never use `/tmp` for delete)
- `GCHAT_HISTORY_CONFIRM_TTL_SECONDS`: confirmation lifetime, 1–3600 seconds;
  default 600

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
persisted session key remains active. Session rotation is serialized with active
message processing: `/new` receives the busy response while a turn is running;
use `/abort`, wait for that turn to release, then retry `/new`.

## Run

```bash
python app.py
```

On startup, the app prints GPL notice text and starts on:
- host: 0.0.0.0
- port: 8080

Endpoints:
- POST /chat
- GET / (liveness only)
- GET /readyz (sanitized local readiness and singleton-worker lease)

`python app.py` is for local development. Production uses the checked-in
Gunicorn configuration so background supervisors start after fork in exactly
one worker:

```bash
.venv/bin/gunicorn --config gunicorn.conf.py app:app
```

Do not increase `workers` or start the app with a pre-fork runner that bypasses
the lifecycle hooks. The history worker also holds a filesystem lease, but the
HTTP process topology is deliberately one process because the attachment and
trajectory supervisors are process-owned too.

## Chat history commands

The feature supports one allowlisted single-user bot DM only. It never supports
group/named spaces and never auto-enrolls an identity from the first webhook.

- `/chat` counts visible messages by human/bot/unknown sender and shows the
  first/last range in `Asia/Bangkok`.
- `/chat clear 30m`, `12h`, `7d`, `2w`, `3mo`, `1y`, or ordered combinations
  such as `1w2d` prepare a snapshot; they do not delete before confirmation.
- Absolute cutoffs accept `YYYY-MM-DD` and RFC 3339 forms documented in
  [PLAN.md](PLAN.md). A naive date/time means `Asia/Bangkok`.
- The boundary is strict: only messages with `createTime < cutoff` are
  candidates; a message exactly at cutoff stays.
- Confirmation expires after the configured TTL. Cancel works only while the
  job is pending. Once confirmation changes the job to `RUNNING`, there is no
  user cancel; the kill switch only prevents claiming the next delete item.
- Writes are paced per space at least 1.1 seconds apart. Large jobs therefore
  take time. A final result can be partial when a permission failure, revoked
  token, retry exhaustion, or circuit breaker stops one credential partition.

The operation stores only required message metadata and opaque-handle hashes.
It does not delete local `chat-in.jsonl`/`chat-out.jsonl`, OpenClaw session or
trajectory data, downloads, Drive files, Vault records, retention rules, or
legal holds.

The user token must be regenerated after adding
`https://www.googleapis.com/auth/chat.messages`; changing the source scope list
does not upgrade an old refresh token. This is a restricted scope and may need
Workspace administrator policy, OAuth verification, and an approved consent
configuration. Bot-authored deletes use the service account's `chat.bot` scope.
Before enabling history, run:

```bash
.venv/bin/python scripts/chat_history_admin.py preflight --remote
```

The output contains categories and booleans only, not tokens or allowlisted
resource names. See [docs/chat-history-runbook.md](docs/chat-history-runbook.md)
for queue inspection, kill switch, checkpoint-aware backup/restore, recovery,
exact-SHA deployment, and rollout gates.

## Reproducible release policy

Runtime and developer tools are directly pinned in `requirements.txt` and
`requirements-dev.txt`. Transitive dependencies are resolved fresh for each
supported Python version; CI tests that clean environment on Linux with Python
3.10 and the production version (3.14). Linux supplies the timezone database,
and CI explicitly proves `ZoneInfo("Asia/Bangkok")`; `tzdata` is intentionally
not added unless the production OS lacks it.

`deploy.sh` accepts only a full tested commit SHA, builds a new release
directory, runs dependency/test/lint/local+remote preflight gates, atomically
switches the `current` symlink, and rolls back the binary on failed liveness,
readiness, release-SHA, or singleton-owner checks. It never overlays a mutable
branch archive. Production configuration, credentials, and durable state live
outside release directories.

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
- Outbound sending accepts only private staged copies made from configured local
  output directories. Symlinks, directories, hidden/temporary files, and nested
  paths are not sent.
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

- Agent requests are submitted through OpenClaw HTTP, but the HTTP response text
  is never posted to Google Chat. A background watcher tails the active session's
  trajectory under `~/.openclaw/agents/main/sessions` and posts each completed
  assistant message to the fixed Google Chat target.
- Existing trajectory history is baselined when a session is first watched and
  is not replayed. `/new` and `/model` switch the watcher to the new session.
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

2. Drive download fails
- regenerate token.json with Drive scope
- verify attachment file is accessible by the authenticated user

3. Module not found errors
- install dependencies in the same Python environment used to run app.py
