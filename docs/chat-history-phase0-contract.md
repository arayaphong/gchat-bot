# Google Chat history Phase 0 contract

Status: local contract locked; external Phase 0 evidence pending
Decision date: 2026-08-11
Scope: implementation contracts that were intentionally left open in
`PLAN.md` and `IMPLEMENTATION_PLAN.md`

This document does not broaden the product scope. It fixes parser boundaries,
time precision, event authority fields, message-create recovery, supported
deployment topology, resource limits, retries, retention, and crash recovery so
later phases can be implemented without making destructive-path decisions in
the middle of coding.

The Phase 0 exit gate is not closed until a live sanitized web/mobile callback
spike and the real target-host topology are recorded. Full web, Android, and
iOS acceptance is repeated in Phase 5, followed by the destructive canary in
Phase 8.

## 1. Command namespace

Command recognition is case-aware and runs on the original message text before
normal provider routing.

### 1.1 Boundary rule

1. Remove Unicode whitespace from both ends only to determine whether text
   looks like the history namespace.
2. Compare the first five characters case-insensitively with `/chat`.
3. If no `/chat` prefix exists, the text is not a history command.
4. If the character after `/chat` is a Unicode letter, decimal digit, or `_`,
   the text is an ordinary message. This preserves `/chatty`.
5. End-of-string or any other next character reserves the history namespace.
6. A reserved command is valid only when the literal command and keywords are
   lowercase and all outer/inter-token whitespace is ASCII space (`U+0020`) or
   tab (`U+0009`).
7. CR, LF, Unicode whitespace, control characters, punctuation in command
   tokens, or extra tokens make a reserved command invalid. Invalid reserved
   commands return usage and never reach OpenClaw.

The recognizer and parser must be separate: recognition decides whether the
message is reserved; parsing decides whether a reserved message is valid.

### 1.2 Truth table

| Input | Classification | Reason |
|---|---|---|
| `/chat` | stats | exact lowercase command |
| `  /chat\t` | stats | ASCII outer whitespace is allowed |
| `/chat clear 1w` | clear | valid form |
| `/chat\tclear\t1w` | clear | ASCII tab is allowed between tokens |
| `/chatty` | ordinary message | identifier continuation after `/chat` |
| `/chat1` | ordinary message | identifier continuation after `/chat` |
| `/chat_foo` | ordinary message | identifier continuation after `/chat` |
| `/Chat` | invalid history | reserved boundary but wrong case |
| `/chat Clear 1w` | invalid history | keyword must be lowercase |
| `/chat!` | invalid history | punctuation reserves namespace but is not grammar |
| `/chat-clear` | invalid history | punctuation reserves namespace but is not grammar |
| `/chat\u00a0clear 1w` | invalid history | non-ASCII whitespace |
| `\u00a0/chat\u00a0` | invalid history | recognized after trim; outer whitespace invalid |
| `/chat clear 1 w` | invalid history | argument must be one token |
| `/chat clear 1w\nextra` | invalid history | CR/LF and extra content |
| `please /chat` | ordinary message | namespace is not at the start after outer trim |

## 2. Time grammar and arithmetic

### 2.1 Resource limits

- `MAX_TIME_ARGUMENT_LENGTH = 64` Unicode code points
- `MAX_DURATION_COMPONENTS = 6`
- ASCII digits only
- at most one component for each of `y`, `mo`, `w`, `d`, `h`, `m`
- a component value must be greater than zero
- leading zeroes are accepted and removed during normalization (`01d` -> `1d`)
- Python integer parsing is bounded by the 64-character argument limit; date
  arithmetic overflow is reported as `time_overflow`

### 2.2 Relative durations

- Units must appear in descending order: `y`, `mo`, `w`, `d`, `h`, `m`.
- `mo` is tokenized before `m`.
- Years and months are converted to one total calendar-month delta and
  subtracted in one step. The day is clamped once to the final target month's
  last day.
- The reference instant is converted to `Asia/Bangkok` before calendar
  subtraction.
- Weeks, days, hours, and minutes are fixed durations applied after the
  calendar-month step.
- The result is normalized to UTC.

Bangkok has no daylight-saving transition, so calendar and fixed operations do
not create an ambiguous or nonexistent local time in this deployment contract.

### 2.3 Absolute time grammar

Accepted forms are exactly:

```text
YYYY-MM-DD
YYYY-MM-DDTHH:MM
YYYY-MM-DDTHH:MM:SS
YYYY-MM-DDTHH:MM:SS.f
YYYY-MM-DDTHH:MM:SS+HH:MM
YYYY-MM-DDTHH:MM:SS-HH:MM
YYYY-MM-DDTHH:MM:SS.f+HH:MM
YYYY-MM-DDTHH:MM:SS.f-HH:MM
YYYY-MM-DDTHH:MM:SSZ
YYYY-MM-DDTHH:MM:SS.fZ
```

Where `f` contains 1–6 decimal digits.

- Date-only and offset-free datetime values use `Asia/Bangkok`.
- An offset is allowed only when seconds are present.
- Uppercase `Z` is accepted; lowercase `z` is rejected.
- Offset magnitude is at most `14:00`; either sign at hour `14` requires minute
  `00`.
- `-00:00` is rejected because it denotes an unknown local offset rather than a
  known UTC offset.
- `24:00`, leap second `:60`, week/ordinal dates, a space instead of `T`, and
  timezone names are rejected.
- Gregorian dates supported by Python `datetime` (`0001`–`9999`) are allowed.

### 2.4 Reference timestamps and precision

- `chat.eventTime` is authoritative when present.
- A present but malformed `eventTime` fails closed. The server clock is used
  only when the field is absent.
- Google/protobuf timestamps can contain 0–9 fractional digits. Parse all nine;
  when converting to Python `datetime`, truncate toward the earlier instant at
  microsecond precision. This is deterministic and conservatively spares a
  sub-microsecond boundary message.
- User absolute input remains limited to six fractional digits.
- Canonical persisted/API cutoff format is UTC with six fractional digits and
  `Z`, for example `2026-08-11T02:30:00.123456Z`.
- `cutoff == reference_time` is allowed. `cutoff > reference_time` is rejected
  without clock-skew tolerance.
- Capture the clock at most once per command.

## 3. Add-on event authority

The event normalizer uses these fields:

| Value | Primary path | Cross-check/fallback |
|---|---|---|
| actor | `chat.user.name` | no `displayName` authorization fallback |
| event time | `chat.eventTime` | injected server clock only if absent |
| message space | `chat.messagePayload.space.name` | must equal `chat.space.name` when both exist |
| source message | `chat.messagePayload.message.name` | required for history command dedup |
| button space | `chat.buttonClickedPayload.space.name` | must equal `chat.space.name` when both exist |
| confirmation card | `chat.buttonClickedPayload.message.name` | required before action CAS |
| action handle | `commonEventObject.parameters.historyActionHandle` | no deprecated `parameters` authority fallback |
| client platform | `commonEventObject.platform` | diagnostic only; never authorization |

Required resource names must be canonical `users/...`, `spaces/...`, and
`spaces/.../messages/...` strings without control characters. Message names must
be children of the bound space.

The callback payload documents the message containing the clicked button, so
its `message.name` is the card-binding authority. A callback that lacks it is
invalid.

For HTTP Google Workspace add-ons, `onClick.action.function` is a complete
function endpoint URL. The application accepts only an explicitly configured
absolute HTTPS URL and never derives it from the HTTP `Host` header.

The Workspace Add-on Chat/DataActions documentation remains marked Developer
Preview as of the decision date. The production project/account must prove that
its existing HTTP add-on configuration can receive `buttonClickedPayload` and
return `updateMessageAction` on web, Android, and iOS before the destructive
flag can be enabled.

## 4. Confirmation and notification message identity

### 4.1 Decision

Use a client-assigned `messageId` as the single durable identity mechanism for
history-owned confirmation, stats, and final messages. Do not rely on
`requestId` for recovery.

Google documents that a custom ID:

- begins with `client-`;
- contains at most 63 lowercase letters, digits, and hyphens;
- is unique within a space; and
- can be used in `spaces/{space}/messages/{clientAssignedMessageId}` for later
  `get`, update, or delete calls.

Formats:

```text
client-jinx-hc-<32 lowercase hex>-<2 digit generation>
client-jinx-hs-<32 lowercase hex>
client-jinx-hf-<32 lowercase hex>
```

`hc`, `hs`, and `hf` mean history confirmation, stats, and history final.
Operation/source-derived hex must use a one-way deterministic digest, not a raw
Google resource ID.

### 4.2 Why `requestId` is not the recovery primitive

Google documents replay of a `requestId`, but safe replay normally requires the
same request body. A confirmation body contains random action handles, while
this design prohibits persisting plaintext handles. After a process crash, the
exact body cannot be reconstructed. A known client-assigned message name lets
the worker perform a read-only `get` without reconstructing the secret body.

### 4.3 Confirmation create/bind protocol

1. Persist operation, generation, client message ID, and SHA-256 handle hashes.
2. Keep plaintext handles in memory only.
3. Create the card using bot authentication and the persisted `messageId`.
4. On success, require a valid response `name`, then atomically bind that
   canonical name and transition to `PENDING_CONFIRMATION`.
5. If the create response is lost, wait the configured recovery grace and call
   `spaces.messages.get` using the client-assigned name.
6. If found, validate its space/sender/client ID, bind its returned canonical
   `name`, and transition to pending.
7. If it remains absent after bounded probes, abandon that generation, replace
   both handle hashes and the client ID in a transaction, and create a new
   generation.
8. A late card from an abandoned generation is orphaned: its handle no longer
   matches and its callback fails closed. The known alias is queued for
   best-effort cleanup; cleanup failure never enables deletion.
9. No callback is accepted while the job is `PREPARING` or while canonical card
   binding is absent.

Stats and final cards contain no action secret, but use the same custom-ID/get
recovery pattern for consistency.

## 5. Sender and space authority

- Before any list or delete, the configured space is fetched and must have
  `spaceType == DIRECT_MESSAGE` and `singleUserBotDm == true`.
- A single-user bot DM contains one human and one Chat app. Within that verified
  and explicitly allowlisted space, `sender.type == BOT` is the app partition.
- `sender.type == HUMAN` is deletable only when `sender.name` equals the bound
  requester.
- `TYPE_UNSPECIFIED`, missing sender fields, anonymous/deleted sender, or a
  different HUMAN is `SKIPPED`.
- `users/app` is only an alias for the calling app and is not persisted as a
  substitute for the canonical sender returned by the API.
- There is no credential fallback after any error.

## 6. Supported deployment topology

The history feature supports:

- one persistent Linux host;
- one or more WSGI processes on that host;
- all processes using the same absolute history state directory;
- a local persistent filesystem with POSIX advisory `flock`, atomic rename, and
  SQLite WAL support; and
- one singleton history worker elected by a lock file.

The feature does not support multi-host workers, NFS/CIFS/network filesystems,
ephemeral container filesystems, or a state directory under `/tmp`. Preflight
must refuse to enable delete execution in those configurations.

Local development evidence on 2026-08-11:

- host `thinkpad-p14s`, Linux, local Btrfs `/home`;
- Python 3.14.6 and Ruff 0.16.0 in `.venv`;
- no matching Python/Flask/Gunicorn service or port 8080 listener was running;
- no repository service/container manifest identifies the production runner;
- `deploy.sh` targets a different hostname, downloads mutable `development`,
  overlays files, and does not test, restart, or check readiness; and
- application code does not load `gchat-bot.env`; the eventual supervisor must
  inject environment variables explicitly.

Therefore `deploy.sh` is not an approved release path for this feature. Phase 7
must bind the supported topology to the real target host and exact tested SHA
before production rollout.

The target-host readiness record must include the exact service/supervisor and
start command, worker/process count and preload/fork behavior, OS user, working
directory, environment injection source, absolute state path and filesystem
type, persistence/backup owner, graceful stop/restart procedure, readiness
check, rollback command, and deployed commit SHA. Missing evidence is a failed
rollout preflight, not permission to infer a topology from `deploy.sh`.

## 7. Work and storage limits

These are initial constants. Configuration can lower them, but production
cannot raise them beyond the hard maximum without a reviewed contract change.

| Limit | Default | Hard maximum | Failure behavior |
|---|---:|---:|---|
| list page size | 1,000 | 1,000 | fixed by client |
| pages per list operation | 1,000 | 1,000 | abort whole stats/preview |
| clear candidates | 10,000 | 100,000 | abort preview, never truncate |
| snapshot metadata | 16 MiB | 64 MiB | abort preview, never truncate |
| active clear jobs per space | 1 | 1 | supersede pre-confirm; reject running |
| confirmation TTL | 10 min | 60 min | expire without delete |
| running job age | 24 h | 72 h | fail remaining items safely |
| terminal ledger retention | 30 d | 365 d | prune terminal jobs only |

At the 1.1-second write interval, 10,000 deletions require at least 3 hours 3
minutes before retries and notification writes. The confirmation card must show
an estimate derived from the actual snapshot size.

Pagination stores every seen non-empty page token. A repeated token aborts the
operation as `pagination_cycle`. Empty/malformed records abort a clear preview;
stats also abort rather than publish a misleading partial count.

Every fetched resource field has a bounded UTF-8 length before it enters the
snapshot size calculation. The implementation must reject a record rather than
truncate a resource name.

Maintenance runs only under the singleton worker lock. It prunes bounded batches
of terminal jobs and performs passive WAL checkpoints. A truncate checkpoint or
backup requires an operations window with no active job/write.

## 8. Timeouts and retries

| Operation | Timeout | Max attempts | Max elapsed | Backoff cap |
|---|---:|---:|---:|---:|
| `spaces.get`, message list page | 15 s | 5 | 5 min | 30 s |
| confirmation create/get recovery | 15 s | 5 | 10 min | 60 s |
| one message delete | 15 s | 8 | 15 min | 60 s |
| callback DB/update response | no network before response | 1 | 5 s internal target | none |
| final notification | 15 s | 10 | 24 h | 15 min |

- Exponential retry uses full jitter in `[0, min(cap, base * 2^attempt)]` with a
  one-second base.
- `Retry-After` accepts delta-seconds and HTTP-date. It is honored when within
  the remaining elapsed budget, with up to one second of positive jitter.
- `404` delete is terminal `ALREADY_ABSENT`.
- `429`, timeout, connection reset, and `5xx` are transient within the budget.
- `401` permits one credential refresh/rebuild per partition, then becomes a
  partition failure.
- `400`, `403`, and other non-transient `4xx` are terminal without credential
  fallback.
- A job deadline never causes already-terminal items to run again.
- Retry timestamps are persisted; worker restarts do not reset budgets.

## 9. State transitions and atomicity

### 9.1 Job transitions

| From | To | Required atomic condition |
|---|---|---|
| none | `PREVIEW_QUEUED` | source message unique, no running job |
| `PREVIEW_QUEUED` | `PREPARING` | singleton worker claim |
| `PREPARING` | `PREVIEW_QUEUED` | transient failure + persisted retry time |
| `PREPARING` | `COMPLETED` | full snapshot has no deletable item |
| `PREPARING` | `FAILED` | permanent preview/card failure |
| `PREPARING` | `PENDING_CONFIRMATION` | snapshot complete + card canonical name bound + TTL set |
| `PENDING_CONFIRMATION` | `DELETE_QUEUED` | confirm hash, actor, space, card, TTL all match |
| `PENDING_CONFIRMATION` | `CANCELLED` | cancel hash and same bindings match |
| `PENDING_CONFIRMATION` | `EXPIRED` | current time is at/after expiry |
| pre-delete state | `CANCELLED` | newer clear supersedes; compare-and-swap state |
| `DELETE_QUEUED` | `RUNNING` | delete flag enabled + singleton claim |
| `RUNNING` | terminal outcome | item counts reconciled in same transaction |

Only `PENDING_CONFIRMATION` can accept a first confirm/cancel action. Duplicate
callbacks read and return the already-claimed safe state without another
transition.

### 9.2 Item transitions

| From | To | Rule |
|---|---|---|
| snapshot insert | `SKIPPED` | partition is `NONE` |
| snapshot insert | `PENDING` | partition is `USER` or `BOT` |
| `PENDING` | `RUNNING` | oldest `(createTime, messageName)` due item |
| `RUNNING` | `PENDING` | transient error + attempt/retry persisted |
| `RUNNING` | `DELETED` | remote delete success |
| `RUNNING` | `ALREADY_ABSENT` | remote 404 |
| `RUNNING` | `FAILED` | permanent/exhausted/partition failure |

No database transaction spans a network request, sleep, or card render.

## 10. Crash-window table

| Crash/failure window | Durable state | Recovery |
|---|---|---|
| job insert committed before webhook ACK | `PREVIEW_QUEUED` | webhook retry deduplicates; worker resumes |
| pagination interrupted | `PREPARING`, incomplete snapshot | discard incomplete items and relist all pages |
| snapshot complete before card create | `PREPARING` | create persisted generation |
| card create succeeds before bind commit | `PREPARING` | get by client message ID, validate, bind canonical name |
| card create absent after recovery grace | `PREPARING` | abandon generation; new handles and message ID |
| orphan generation is clicked | non-pending/unbound | generic failure; no state change/delete |
| confirm CAS commits before HTTP response | `DELETE_QUEUED` | retry returns running card; no second claim |
| cancel CAS commits before HTTP response | `CANCELLED` | retry returns cancelled card; no enqueue |
| callback update before first delete | `DELETE_QUEUED` | persisted pacer delays next write by at least 1.1 s |
| item claim before API call | item `RUNNING` | new singleton owner resets stale claim to pending |
| delete succeeds before item commit | item `RUNNING` | repeat delete; 404 becomes already absent |
| item terminal before cached count update | terminal item | reconcile counts from items |
| job terminal before final create | notification pending | send only final notification |
| final create succeeds before notification commit | notification sending | get by client ID; mark sent; never rerun delete |
| process dies while delete flag is turned off | last item may be in flight | finish/recover that item; no new claim until re-enabled |

## 11. Phase 0 artifacts and verification

Phase 0 produces:

- this accepted contract;
- synthetic official-contract Add-on fixtures for WEB/ANDROID/IOS;
- one legacy fixture derived from the observed local field shape with all values
  replaced;
- fixture contract tests;
- a pinned Ruff development dependency;
- a Python 3.10/3.14 GitHub Actions quality matrix; and
- a green local quality gate.

These repository artifacts do not substitute for the two outstanding external
checks: live staging/API evidence and target-host deployment evidence.

Official references checked on 2026-08-11:

- [Workspace Add-on event objects](https://developers.google.com/workspace/add-ons/concepts/event-objects)
- [Create a Chat message](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create)
- [Get a Chat message](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/get)
- [Name and send Chat messages](https://developers.google.com/workspace/chat/create-messages#name_a_message)
- [Send and update messages from an Add-on](https://developers.google.com/workspace/add-ons/chat/send-messages)
- [Convert an HTTP Chat app to an Add-on](https://developers.google.com/workspace/add-ons/chat/convert)
- [Chat API usage limits](https://developers.google.com/workspace/chat/limits)
- [Space resource](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces)
- [User resource](https://developers.google.com/workspace/chat/api/reference/rest/v1/User)

Local verification command:

```bash
python -m pip check
python -m unittest discover -s tests -v
ruff check .
git diff --check
```
