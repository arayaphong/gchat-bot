# Chat history operations runbook

This runbook applies to the single persistent Linux host topology represented by
`deploy/gchat-bot.service`: one Gunicorn worker, threads inside that worker,
local persistent SQLite storage, and `flock`. Before using it on another host,
confirm the service account, working directory, mount type, environment-file
injection, restart behavior, worker/process count, and that the state directory
survives releases and reboots. Never put delete state on `/tmp`, NFS, overlay,
or another unsupported/ephemeral filesystem.

## Normal operation

The service is a user unit:

```bash
systemctl --user enable --now gchat-bot.service
systemctl --user status gchat-bot.service
systemctl --user restart gchat-bot.service
systemctl --user stop gchat-bot.service
journalctl --user-unit gchat-bot.service --since today
```

Gunicorn preloads application code in its master, then `post_fork` starts the
attachment supervisor and history supervisor in the only worker. The history
supervisor must acquire `history-worker.lock`, run interrupted-state recovery,
write its durable heartbeat, and poll immediately. Recovery therefore does not
need a new webhook. Do not increase Gunicorn's worker count or start another
copy against the same attachment state.

`GET /` proves only that the HTTP process is alive. `GET /readyz` additionally
requires process lifecycle startup, the attachment watcher, local
configuration/schema/timezone preflight, and the exact fresh worker lease when
history is enabled. It returns only sanitized categories. Credential scopes and
canonical DM access are an explicit network preflight:

```bash
cd "$HOME/gchat-bot/current"
.venv/bin/python scripts/chat_history_admin.py preflight --remote
.venv/bin/python scripts/chat_history_admin.py preflight --require-worker
.venv/bin/python scripts/chat_history_admin.py diagnostics
```

Remote preflight performs `spaces.get`, not message listing. It proves the user
token has all granted scopes, the configured resource is the allowlisted
single-user bot DM, and—when delete is enabled—the bot credential can obtain a
`chat.bot` token. An `error_code` is safe to copy into an incident ticket.

Diagnostics intentionally omit operation IDs, user/space/message names, token
data, and message content. Watch:

- job/item counts by state and oldest active age;
- `worker_lease=false` or a heartbeat older than 20 seconds;
- user/bot partition failure counts and pending final notifications;
- sustained 429/retry/pacer errors and unusual WAL growth.

All mutable runtime files must live outside `~/gchat-bot/releases/*`. The
production environment must set `GCHAT_BOT_CRED`, `GCHAT_TOKEN_FILE`,
`JINX_SESSION_KEY_FILE`, `JINX_CHAT_IN_LOG_FILE`, and
`JINX_CHAT_OUT_LOG_FILE` to private shared paths. This preserves credentials,
the active OpenClaw conversation, and local logs across atomic release switches
and binary rollback.

## Queue and recovery

Expected job states are `PREVIEW_QUEUED`, `PREPARING`,
`PENDING_CONFIRMATION`, `DELETE_QUEUED`, `RUNNING`, and terminal
`COMPLETED`, `PARTIAL_FAILED`, `FAILED`, `CANCELLED`, or `EXPIRED`. Items are
`PENDING`, `RUNNING`, `DELETED`, `ALREADY_ABSENT`, `SKIPPED`, or `FAILED`.

- Pending confirmations expire through worker maintenance; do not edit their
  TTL or action hashes in SQLite.
- On restart, incomplete preview snapshots are rebuilt, stale item claims return
  to pending, counts reconcile from terminal items, and final notifications
  retry independently. A delete that succeeded remotely but crashed before its
  commit becomes `ALREADY_ABSENT` after the idempotent retry.
- For a stuck worker, collect `status`, readiness, diagnostics, and recent
  journal categories; restart the unit once. If the lease remains unavailable,
  find and stop the duplicate process. Never delete the DB, WAL, SHM, or lock
  file to acquire ownership.
- A revoked/missing user token or missing granted scope requires reauthorization
  with `helpers/token_tools/get_token.py` (or the manual helper), using the same
  allowlisted Workspace user, then remote preflight and restart. Never fall back
  from user credentials to bot credentials or vice versa.
- Permission failures and circuit-breaker stops remain partial/failed ledger
  results. Fix policy/token access and follow the supported retry/recovery path;
  do not rewrite item status manually.

Schema migration runs under a private migration lock and fails closed on a
newer/unknown version. Before deploying a migration, take a verified backup and
prove restore in a disposable directory. Roll back only to binaries documented
as compatible with the current schema and the security routing/redaction
baseline.

## Kill switch and destructive limitations

To stop new delete-item claims, set this exact value in
`~/.config/gchat-bot/env` and restart:

```text
GCHAT_HISTORY_DELETE_ENABLED=false
```

Verify `/readyz` and diagnostics afterward. This does not undo messages already
deleted and does not interrupt a single API request already in flight. Once a
user confirms and the job reaches `RUNNING`, there is no user cancel; the kill
switch prevents the next item claim. Leave `GCHAT_HISTORY_ENABLED=true` during
an incident when read-only stats and secure history-command routing should stay
available. To disable all history behavior, set both flags false and restart.

Google Chat deletion cannot be undone by this service. It also cannot remove or
restore Drive files, local JSONL/OpenClaw data, Vault-retained copies, retention
rules, or legal holds.

## Checkpoint-aware backup and restore

Never copy `history.sqlite3` while WAL mode is active. The backup command first
runs a passive checkpoint, then uses SQLite's online backup API and verifies
integrity/schema before returning:

```bash
backup_root="$HOME/.openclaw/backups/jinx-gchat"
mkdir -p "$backup_root"
chmod 700 "$backup_root"
cd "$HOME/gchat-bot/current"
.venv/bin/python scripts/chat_history_admin.py backup \
  --output "$backup_root/history-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

Keep the output outside the release and state directories. A nonzero result is
not a backup. For a restore drill, use a copied environment whose
`JINX_CHAT_HISTORY_STATE_DIR` points to a disposable local directory.

Production restore is offline and explicit:

```bash
systemctl --user stop gchat-bot.service
cd "$HOME/gchat-bot/current"
.venv/bin/python scripts/chat_history_admin.py restore \
  --input /absolute/path/to/verified-history-backup.sqlite3 \
  --confirm-service-stopped
systemctl --user start gchat-bot.service
.venv/bin/python scripts/chat_history_admin.py preflight --require-worker
```

Restore refuses a held worker lease, verifies the source, and creates a private
`history.pre-restore-*.sqlite3` safety backup before replacing pages through
SQLite. Preserve that file until post-restore acceptance completes. Do not
manually remove WAL/SHM files or copy a main DB over them.

## Exact-SHA deployment and rollback

From the confirmed target host, deploy only a commit that passed CI:

```bash
./deploy.sh 0123456789abcdef0123456789abcdef01234567
```

Before merging, the T495 test environment may temporarily deploy the
`google-chat-history` branch archive with an explicit non-production flag:

```bash
./deploy.sh --test-google-chat-history
```

This mode resolves and records the branch HEAD, then downloads
`https://github.com/arayaphong/gchat-bot/archive/refs/heads/google-chat-history.zip`.
It aborts if the branch changes during the download. Because the URL is mutable,
do not use this mode as a production release mechanism; remove it after testing.

Normal deployment refuses mutable branches, and all modes refuse existing
release directories. Normal mode also requires the supplied SHA to be the
current HEAD of `development`. The script builds an isolated release, installs
pinned direct requirements, runs `pip check`, the full unit suite, Ruff,
release identity/timezone checks, and remote preflight before switching
`~/gchat-bot/current`. After restart it verifies liveness, readiness, the served
SHA, and a fresh singleton worker lease. A failed gate switches the symlink
back and restarts the prior service. Credentials, env, OpenClaw workspace, and
history ledger are never overlaid.

Binary rollback is allowed only to a schema-compatible release that includes
auth-before-log, callback binding, redaction, and reserved history routing. First
turn delete off, inspect/preserve the ledger, then switch the release. Do not
reset state to make an older binary boot. Already successful remote deletes
remain irreversible.

## Enablement checklist

Keep both flags false until all of these are recorded:

1. Exact release SHA, CI matrix, clean-install gate, remote preflight, and host
   topology evidence.
2. OAuth restricted-scope/admin policy and the Workspace Add-on Chat feature
   entitlement. The relevant Workspace Add-on send path is Developer Preview;
   documentation alone does not prove the production project is enrolled.
3. Sanitized web, Android, and iOS event/card fixtures plus live callback
   message binding and create-timeout reconciliation.
4. Local backup/restore and kill-switch drills.
5. Read-only `/chat` comparison with independent inspection.
6. Preview/cancel tests including expiry, forged/wrong actor, double click, and
   log/SQLite sentinel scans.
7. A tiny known-boundary destructive DM canary, strict cutoff inspection,
   restart-during-job recovery, token-revoke/partial failure, and final-message
   retry independent of deletion.

Only after the manual acceptance checklist in `PLAN.md` passes may delete stay
enabled for the one allowlisted DM.
