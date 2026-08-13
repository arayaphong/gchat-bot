# gchat-bot

Google Chat bot webhook (Flask) that:

- receives Chat message events at /chat and acknowledges other interactions
  without dispatching them to OpenClaw
- verifies Google Chat bearer tokens
- separates OpenClaw history by each Google Chat thread resource with deterministic keys
- starts fresh history when a user sends `/new` (a new thread in a named Space,
  or an in-place reset of the DM's deterministic thread session)
- downloads Drive, Google Chat media, and GIF attachments from incoming messages
- submits normal turns through the OpenClaw Gateway WebSocket API
- renders markdown-like responses into Google Chat cards

License: GNU GPL v3.0 (see LICENSE).

## Project Files

- app.py: main webhook server
- helpers/md_to_gchat.py: markdown -> Google Chat card widgets
- helpers/outbound_attachment_watcher/: durable inotify outbox watcher (ledger, staging capture, service)
- helpers/session_keys.py: Google Chat context -> deterministic OpenClaw key mapping
- helpers/thread_uploads.py: deterministic thread -> private upload-directory mapping
- helpers/session_trajectory_watcher.py: multi-session trajectory delivery
- helpers/chat_target_store.py: fallback destination for legacy unscoped ledger entries
- helpers/token_tools/get_token.py: OAuth token helper via local callback server
- helpers/token_tools/get_token_manual.py: OAuth token helper via manual redirect URL paste
- helpers/token_tools/manual_token.py: compatibility alias for the manual helper

## Requirements

Python 3.10+ on Linux is required. The bot sends normal agent requests directly
to the OpenClaw Gateway WebSocket API. The `openclaw` CLI must also be available
for `/models`, `/abort`, and session administration commands. Outbound file
delivery uses Linux inotify through `inotify-simple`.

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

`/new` does not use the user OAuth token. In a named Space it posts a root
message as the bot; in a direct message it only resets the matching OpenClaw
session and replies in that DM. Neither path needs Chat API user scopes or
Workspace administrator approval. A previously generated `token.json` that
also contains the old Chat grants remains usable; it does not need to be
regenerated solely for this change.

## Environment Variables

Required (configure at least one authentication mode):

- GCHAT_AUDIENCE: exact Chat webhook URL audience (recommended), example:
  https://your-domain/chat. Google-signed OIDC tokens for this mode must identify
  `chat@system.gserviceaccount.com`.
- GCHAT_PROJECT_NUMBER: Google Cloud Project Number audience for Google Chat's
  self-signed service-account JWT mode

Optional:

- GCHAT_BOT_CRED: path to service account credentials file (default: ./credentials.json)
- GCHAT_TOKEN_FILE: path to user OAuth token file (default: ./token.json)
- MAX_ATTACHMENT_BYTES: max bytes per downloaded attachment (default: 20971520)
- MAX_ATTACHMENTS_PER_MESSAGE: max incoming files processed per message (default: 8)
- MAX_OUTBOUND_ATTACHMENT_BYTES: max size of one outbound file
  (default: 20971520)
- DRIVE_UPLOAD_FOLDER_ID: Drive folder used for Jinx file-preview cards
  (the OAuth identity must be able to write to it, and the folder must already
  be shared with the intended Chat recipients)
- GCHAT_OUTBOUND_SPACE and GCHAT_OUTBOUND_THREAD: optional immutable fallback
  destination used only to finish legacy unscoped ledger entries. They do not
  control normal replies, deterministic sessions, thread-scoped uploads, or
  `/new`.
- GCHAT_OUTBOUND_TARGET_FILE: persisted learned fallback destination (default:
  `~/.openclaw/state/jinx-gchat/target.json`)
- JINX_OUTBOUND_STATE_DIR: SQLite ledger, process lock, durable inbound-message
  deduplication tombstones, durable session-output cursors, and private staging
  root (default: `~/.openclaw/state/jinx-gchat`)
- OPENCLAW_GATEWAY_TOKEN: gateway token, useful when connecting through a remote relay
- OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS: whether the active provider can read the
  bot's local attachment paths (`auto`, `allow`, or `deny`; default: `auto`).
  Auto allows loopback endpoints only. Use `allow` only when a remote provider
  has the same absolute download directory mounted.
- OPENCLAW_CONFIG_FILE: OpenClaw config file (default: ~/.openclaw/openclaw.json)

The gateway token is read from `OPENCLAW_GATEWAY_TOKEN` first. If it is unset,
the provider loads `gateway.auth.token` from the OpenClaw config file.
The OpenClaw Gateway must expose `agent`, `agent.wait`, `sessions.create`,
`sessions.reset`, and `sessions.abort`. Model inspection also uses the OpenClaw
CLI.

Session identity is derived from the authenticated Chat event. Every message,
including a top-level/root message and a direct message, must supply its
canonical `message.thread.name` and is mapped to:

`agent:main:gchat:<space-id>:<thread-id>`

There is no synthetic `:main` context. A named-Space root message and replies
inside that root's thread therefore share the same deterministic key. A direct
message also keeps its real Google-assigned thread ID as both its key identity
and reply route. The bot passes the exact inbound `message.thread.name` back to
Google Chat instead of clearing it for direct messages.

Space and thread IDs are kept case-sensitive and treated as opaque identifiers.
OpenClaw canonicalizes ordinary session keys to lowercase, so Jinx serializes
each ID component with reversible lowercase percent escapes for bytes outside
`[a-z0-9._-]`. For example, `AAQAjEa3Dp8` is stored in the session key as
`%41%41%51%41j%45a3%44p8`, then decoded back to the exact original ID for Chat
routing. This prevents case collisions while making OpenClaw's lowercase
normalization a no-op. The final encoded key is limited to 512 characters.

The legacy `./session_key`, `OPENCLAW_AGENT`, and `OPENCLAW_SESSION_KEY` values
do not select a conversation session; an existing `./session_key` file is left
untouched but ignored. Histories stored under earlier random keys are not
renamed or merged into the deterministic keys; each root/thread begins using
its deterministic history the next time it is addressed. A session created by
an earlier build from an unencoded mixed-case Chat ID might remain in OpenClaw
under a lossy lowercase key; Jinx does not migrate or reuse that ambiguous key.

`/new` accepts an optional trailing model key: `/new <model-key>`. When given,
Jinx validates the key against `list_models()` (must exist, `available: true`,
`missing` not `true`) before doing anything else; an unknown or unavailable key
is rejected with a notice and no session is touched. Without an argument it
falls back to the invoking context's current effective model, as before. In a
named Space it creates a root message and ensures the exact deterministic
session for that new thread exists with the resolved model; the source context
keeps its own history and remains independently usable. In a Google Chat
direct message, where usable reply threads are unavailable, it calls
`sessions.reset` for the exact `agent:main:gchat:<space-id>:<thread-id>` key
derived from that DM, passing the resolved model. OpenClaw keeps that session
key and model override while assigning fresh history/a fresh `sessionId`. If
the DM thread key does not exist yet, Jinx creates that exact key with the
resolved model instead.
It does not create a Google Chat root or thread for the DM path — the Chat API's
`messageReplyOption` thread-routing control is only supported in named spaces,
so a bot cannot reliably create a new thread and route replies into it inside a
DM. Jinx does not interpret `/model` or `/model <model-key>` as
session-management commands. They are forwarded unchanged to OpenClaw as
ordinary user messages and never call the removed `sessions.patch`
model-mutation path.
Session-changing commands remain serialized with active message processing:
`/new` (with or without a model key) receives the busy response while a turn
is running; use `/abort`, wait for that turn to release, then retry `/new`.

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
- ensure the bot is installed in the Space and can post messages there; in a
  named Space `/new` uses the existing bot authentication to create a new root
  thread, while in a DM it resets the existing deterministic session and sends
  its notice to the inbound Google thread
- follow-up messages use `REPLY_MESSAGE_OR_FAIL`, so an invalid or missing
  target thread fails instead of silently appearing as another root message;
  the returned message route is also checked against the requested thread

## Security Notes

- Do not commit credentials.json, token.json, or client_secret.json.
- .gitignore already excludes these sensitive files.
- OAuth helpers write `token.json` atomically with mode `0600`.
- Incoming /chat requests are rejected unless JWT verification passes.
- Attachment and image sizes are capped to reduce abuse and memory pressure.
- Outbound sending accepts only private staged copies. Symlinks, directories,
  hidden/temporary files, and nested entries discovered by auto-watch are not
  sent. An explicit `MEDIA:` directive is separate from auto-watch: it may select
  any absolute regular-file path readable by the Jinx process, including paths
  reached through symbolic links. The configured source roots do not restrict
  explicit `MEDIA:` references.
- Normal text replies, explicit `MEDIA:` files, and thread-scoped outbox files
  carry their originating Space/thread route and never use the learned fallback
  target. The fallback target is retained only so older unscoped ledger entries
  can finish delivery after an upgrade; new bare files under the shared uploads
  root are ignored.
- The bot identity authors the Chat card, but Drive access still follows the
  configured folder's sharing policy; posting a card does not grant Drive access.

## Known Behavior

- Agent requests are submitted through the OpenClaw Gateway. Jinx waits for
  `agent.wait` to report a terminal result before allowing another turn, but
  Gateway RPC response text is never posted to Google Chat. A background watcher
  tails every registered deterministic session under
  `~/.openclaw/agents/main/sessions` and sends completed messages to the
  Space/root/thread bound to that session.
- Existing trajectory history is baselined when each session is first watched
  and is not replayed. Registering another session does not replace or stop the
  cursors for previously used root/thread sessions. Registered routes and the
  last successfully delivered offsets are persisted under
  `JINX_OUTBOUND_STATE_DIR`, so a restart resumes pending output instead of
  rebasing at the end of each trajectory.
- Trajectory delivery uses a stable Google Chat request ID for each source line,
  so a retry does not intentionally create a second Chat message.
- Authenticated message events are durably claimed by their exact
  `message.name`. Simultaneous or later redeliveries are ignored. Before a
  normal provider turn is sent, its deterministic OpenClaw run ID is persisted;
  recovery waits for that run and never dispatches it again. This is strict
  at-most-once behavior: a process crash after persisting the intent but before
  sending can drop that turn rather than risk creating a duplicate.
- Google Chat notice IDs for one `/new` command are stable, and completed
  redeliveries are suppressed by the inbound-message tombstone. The OpenClaw
  `sessions.reset` RPC itself has no command-delivery idempotency token, so a
  process crash after reset but before the tombstone is committed can still
  leave an ambiguous DM state. Check that state before manually retrying `/new`.
- Attachments are saved using the MIME type to determine file extension.
- Incoming files are downloaded to `~/.openclaw/workspace/downloads`.
  Jinx remains silent when attachment handling succeeds and reports only limits,
  skipped files, download failures, or provider-access failures. Agent dispatch
  is asynchronous (OpenClaw reads the downloaded `localPath` during its run),
  so downloaded files are not deleted after a turn is dispatched;
  they accumulate in that directory and need external retention/cleanup.
- Outbound deliverables should normally be attached explicitly by a completed
  assistant message using a full line such as
  `MEDIA:/tmp/image-1.png`
  (any absolute path visible and readable inside the Jinx sandbox is accepted;
  explicit paths are not limited to the automatic-discovery roots and may pass
  through symbolic links). The directive line is removed from the Google Chat
  text, and the referenced file enters the durable staging, retry, and delivery
  pipeline with the originating session route. Repeated paths in one message are
  deduplicated; inline `MEDIA:` text is left unchanged.
- Each deterministic Chat thread has a stable private outbox at
  `~/.openclaw/workspace/uploads/thread-<sha256-of-session-key>/`. The exact
  path is included in every ordinary OpenClaw request. A completed file placed
  directly in that directory is staged and sent only to its registered Space
  and thread. Files placed directly in the shared `uploads` root are ignored,
  because they have no unambiguous thread identity.
- Thread-directory mappings are stored in the attachment SQLite ledger. The
  folder name hashes both Space and thread identity, stays below filesystem
  component limits, and is validated against the canonical session key before
  it is watched. Existing files are baselined when a thread directory is first
  registered and are not sent.
  A durable startup cutover preserves this rule across an interrupted first
  launch. Later restarts reconcile files created in registered thread folders
  while the bot was offline.
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
- OpenClaw Gateway is the only provider for normal agent requests and receives
  the deterministic key derived from each Chat event.
- Only completed trajectory messages are sent; incremental streaming deltas and
  Gateway RPC response payloads are ignored.
- `/model` and `/model <model-key>` are ordinary agent messages. Jinx forwards
  them unchanged through the normal attachment, watcher, and trajectory path;
  it does not mutate the session model. `/models` remains a distinct read-only
  bot command.

## Troubleshooting

1. 401 unauthorized on /chat
- verify GCHAT_AUDIENCE matches the exact endpoint URL configured in Google Chat
- when using project-number JWT mode, verify GCHAT_PROJECT_NUMBER matches the
  Chat app's Google Cloud project number
- confirm Chat app is calling this endpoint and includes Authorization header

2. `/new` cannot start a fresh session

- in a named Space, verify the bot is already installed and can post a root
  message there
- in a DM, verify the OpenClaw Gateway supports `sessions.reset`; `/new` reuses
  the inbound Google thread instead of creating another one
- verify the Gateway supports `sessions.create` with an explicit key and model
  for a new Space thread or a DM key that does not yet exist
- inspect the Google Chat and OpenClaw errors in the service log

3. Drive download fails

- regenerate token.json with both configured Drive scopes
- verify attachment file is accessible by the authenticated user

4. Module not found errors

- install dependencies in the same Python environment used to run app.py
