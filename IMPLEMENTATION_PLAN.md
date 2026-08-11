# Detailed Implementation Plan: Google Chat History

เอกสารนี้แปลงข้อกำหนดใน `PLAN.md` ให้เป็นแผนลงมือทำสำหรับ branch
`google-chat-history` โดยอ้างอิงโครงสร้างโปรเจกต์ปัจจุบัน ณ วันที่ 2026-08-11

เป้าหมายของแผนนี้คือส่งมอบ `/chat` และ `/chat clear <เวลา>` แบบ incremental:
เริ่มจาก security/routing foundation, เปิด read-only stats ก่อน, ต่อด้วย durable preview และ
confirmation และเปิดการลบจริงเป็นลำดับสุดท้าย

เอกสารนี้ไม่เปลี่ยน product contract ใน `PLAN.md` แต่เพิ่มรายละเอียดที่จำเป็นต่อการ implement,
review, test, deploy และ rollback งานที่มี destructive side effect

รายละเอียด decision ที่ปิดแล้วใน Phase 0 อยู่ที่
[`docs/chat-history-phase0-contract.md`](docs/chat-history-phase0-contract.md) และให้ถือเป็น
contract หลักเมื่อข้อความเชิงข้อเสนอในเอกสารนี้ไม่เจาะจงเท่า ADR ดังกล่าว

## 1. สถานะตั้งต้นของ repository

- branch ปัจจุบัน: `google-chat-history`
- production code หลักอยู่ใน `app.py` และ module ใต้ `helpers/`
- webhook ปัจจุบันบันทึก raw body ก่อนตรวจ JWT
- route ปัจจุบันใช้ `displayName` แทน stable `user.name`
- message และ callback ยังไม่มี typed event normalization กลาง
- ทุก message ปกติถูกส่งต่อไป `MessageOrchestrator`
- Chat REST write ทั้งหมดผ่าน `ChatGateway._post_message()` แต่ยังไม่มี shared pacer
  และ method ยังไม่คืน response หรือ `message.name`
- มี SQLite/WAL, retry และ cross-process `flock` pattern ให้ใช้เป็นต้นแบบใน
  `helpers/outbound_attachment_watcher.py`
- OAuth scopes ซ้ำอยู่ใน `app.py` และ token helper สามไฟล์
- `helpers/token_tools/manual_token.py` มี authorization code อยู่ใน source และต้องถูกลบ
  ก่อนเปิดใช้ scope ใหม่
- baseline unit tests ผ่าน 194 tests ด้วย local virtual environment
- baseline ก่อน Phase 0 มี `ruff check .` ไม่ผ่าน 3 จุดเดิม (`SIM117`) ใน
  `tests/test_openclaw_cli.py`; H0.5 แก้ debt นี้แล้ว
- ก่อน Phase 0 repository ไม่มี CI workflow, production service manifest หรือ readiness endpoint;
  H0.5 เพิ่ม quality workflow แล้ว แต่ service manifest/readiness เป็นงาน Phase 7
- `deploy.sh` ดึง branch `development` แบบ mutable ZIP และไม่รัน test/lint/preflight/restart

ผลจาก baseline นี้ทำให้ต้องมี Phase 0 และ Phase 1 ก่อนแตะ Google Chat delete API

## Roadmap overview

| Phase | Outcome | Production capability |
|---|---|---|
| 0 — Contract/baseline | ตรึง grammar, API idempotency, topology และ release gate | ยังไม่เปลี่ยน runtime behavior |
| 1 — Security/routing | auth-before-log, redaction, typed events, command reservation | feature ปิดและไม่มี Chat history API call |
| 2 — Command/time domain | strict parser และ deterministic cutoff | ยังไม่มี network/state side effect |
| 3 — Read-only stats | canonical DM validation, pagination, `/chat`, shared pacer | เปิด stats-only canary ได้ |
| 4 — Durable state | SQLite state machine, CAS, scheduler, recovery | ยังไม่มี delete executor |
| 5 — Preview/confirmation | immutable snapshot, secure card, cancel/expiry | ทดสอบ flow ได้; production confirm ยังปิด |
| 6 — Delete worker | sender-routed delete, retry, recovery, final summary | destructive canary ได้ |
| 7 — Operations | lifecycle, readiness, CI/deploy, README/runbook | release พร้อม rollout |
| 8 — Rollout | config → stats → preview → small delete → full enable | เปิดเฉพาะ allowed 1:1 DM |

## 2. หลักการออกแบบที่ทุกเฟสต้องรักษา

1. **Fail closed** — identity, space, scope, state, token หรือ API record ที่ไม่ครบต้องหยุดงาน
   และห้ามลอง credential อีกชนิด
2. **Durable before side effect** — ต้อง persist intent/snapshot/claim ก่อน remote mutation
3. **No delete before confirmation** — preview และ card delivery ต้องไม่มี delete call แฝง
4. **Immutable target set** — หลัง `PENDING_CONFIRMATION` เปลี่ยน identity fields ของ item ไม่ได้
5. **Stable identity only** — authorization ใช้ `users/...`, `spaces/...` และ `message.name`
   เท่านั้น; `displayName` ใช้เพื่อแสดงผลได้แต่ห้ามใช้ตัดสินสิทธิ์
6. **One credential partition per item** — HUMAN requester ใช้ user OAuth, BOT ใช้ bot
   credential, unknown/mismatch ถูกข้าม และไม่มี fallback
7. **One durable write schedule per space** — gateway write, confirmation/final card และ delete
   ต้องใช้ per-space pacer เดียวกัน
8. **No secrets or content at rest** — ledger/log ต้องไม่มี message text, OAuth token, raw API
   error body หรือ plaintext confirmation handle
9. **No long-held global gate** — history worker ห้ามถือ `ProcessingGate` ระหว่าง preview/delete
10. **Testable time and I/O** — clock, sleeper, random jitter, Google API transport และ worker wakeup
    ต้อง inject/fake ได้

## 3. ขอบเขต module และ dependency

| Module | หน้าที่หลัก | ห้ามรับผิดชอบ |
|---|---|---|
| `helpers/google_scopes.py` | source of truth ของ Drive/user Chat/bot scopes | สร้าง credential หรือเรียก API |
| `helpers/chat_events.py` | normalize Add-on/legacy payload เป็น typed event | authorization, command parsing, network |
| `helpers/chat_history_time.py` | recognize command, parse argument, คำนวณ cutoff | Flask, Google API, SQLite |
| `helpers/chat_history_client.py` | validate DM, list/paginate, delete ตาม credential | state transition, card rendering |
| `helpers/chat_clear_store.py` | schema, transaction, dedup, snapshot, CAS, recovery, pacing state | network call หรือ sleep ใน transaction |
| `helpers/chat_history_service.py` | stats/preview/callback/worker orchestration | parse raw Flask payload โดยตรง |
| `helpers/services/chat_services.py` | render stats/usage/confirmation/status/final cards | authorization หรือ mutate job |
| `helpers/chat_gateway.py` | Chat REST create, structured card, response name, shared pacing, redacted output log | เลือก candidate หรือ mutate history state |
| `app.py` | dependency wiring, auth, route ordering, ACK, lifecycle | business logic ของ parser/store/worker |
| `helpers/message_orchestrator.py` | defense-in-depth ว่า reserved history command ไม่ถึง provider | รัน history job |

Dependency หลัก:

```text
scopes/config ─┬─> credential service ─> Chat API client
               └─> token helpers

event normalizer ─> command/time parser ─> history service
                                              │
store + pacer <────────────────────────────────┤
gateway + presenters <────────────────────────┤
                                              └─> app route/lifecycle
```

หลัง interface ของ event/parser/store ถูกตรึง งาน client, presenter และ store สามารถพัฒนา
ขนานกันได้ แต่การ wire production route ต้องทำหลังแต่ละ component มี targeted tests แล้ว

## 4. Configuration ที่จะเพิ่ม

| Variable | Required when | ความหมาย/validation |
|---|---|---|
| `GCHAT_HISTORY_ENABLED` | ทุก history mode | `true/false`; default `false` ระหว่าง rollout |
| `GCHAT_HISTORY_DELETE_ENABLED` | จะ confirm/delete จริง | kill switch สำหรับ destructive path; default `false` |
| `GCHAT_HISTORY_ALLOWED_USER` | history enabled | exact `users/...` เพียงหนึ่งค่า |
| `GCHAT_HISTORY_ALLOWED_SPACE` | history enabled | exact `spaces/...`; fallback ได้เฉพาะ explicit `GCHAT_OUTBOUND_SPACE` จาก env |
| `GCHAT_CARD_ACTION_URL` | delete enabled | absolute HTTPS URL, ไม่มี userinfo/query-derived authority/fragment |
| `JINX_CHAT_HISTORY_STATE_DIR` | optional | default `~/.openclaw/state/jinx-gchat/chat-history` |
| `GCHAT_HISTORY_CONFIRM_TTL_SECONDS` | optional | default `600`; มี min/max ที่ validate ตอน startup |

กฎของ feature flags:

- route-level recognizer ยัง reserve เฉพาะ history command boundary แม้ feature ปิด
  เพื่อไม่ให้คำสั่งดูแลระบบไหลเข้า
  OpenClaw; ผู้ใช้จะได้รับข้อความว่า feature ยังไม่พร้อม
- `GCHAT_HISTORY_ENABLED=true` และ delete flag ปิด: `/chat` ใช้งานได้ แต่ `/chat clear...`
  ตอบว่า clear ยังปิดและไม่สร้าง operation/card
- worker ตรวจ delete flag ก่อน claim job และก่อน item ถัดไป เมื่อ kill switch ถูกปิดให้หยุดหลัง
  remote request ปัจจุบันจบ โดยเก็บ state เพื่อ resume ภายหลัง
- configuration ที่ผิดต้อง fail closed เฉพาะ history feature พร้อม startup diagnostic ที่ sanitize แล้ว;
  ห้าม auto-learn allowlist จาก webhook หรือ persisted target store

## 5. State model ที่จะ implement

### 5.1 Job states

```text
PREVIEW_QUEUED -> PREPARING
PREPARING -> PREVIEW_QUEUED                (transient retry)
PREPARING -> PENDING_CONFIRMATION | COMPLETED | FAILED
PENDING_CONFIRMATION -> DELETE_QUEUED -> RUNNING
RUNNING -> COMPLETED | PARTIAL_FAILED | FAILED
PENDING_CONFIRMATION -> CANCELLED | EXPIRED
PREVIEW_QUEUED | PREPARING | PENDING_CONFIRMATION -> CANCELLED (superseded)
```

Failure path ที่ไม่ต้องเพิ่ม state ใหม่:

- preview/list/card delivery ล้มเหลวถาวร: `FAILED` + safe error category
- ไม่มี deletable candidate: `COMPLETED` พร้อม zero/no-op summary และไม่สร้างปุ่ม
- transient preview/card failure: คง `PREVIEW_QUEUED` หรือ `PREPARING` พร้อม `next_attempt_at`
- confirmation card ที่ส่งสำเร็จแต่ยัง bind ไม่ได้: คง `PREPARING`; callback fail closed
- notification ล้มเหลว: job terminal เหมือนเดิม แต่ notification state retry แยกต่างหาก
- clear ใหม่ supersede งานก่อน delete: `CANCELLED` + category `superseded`

TTL เริ่มเมื่อ confirmation card สร้างและ bind `message.name` สำเร็จ ไม่เริ่มตอนรับ command
เพื่อไม่ให้ history ขนาดใหญ่หมดอายุก่อนผู้ใช้เห็น card

### 5.2 Item states

```text
PENDING -> RUNNING -> DELETED | ALREADY_ABSENT | SKIPPED | FAILED
RUNNING -> PENDING                  (transient retry พร้อม next_attempt_at)
```

- `SKIPPED` ถูกสร้างเป็น terminal ได้ตั้งแต่ snapshot เมื่อ sender unknown/HUMAN mismatch
- startup recovery เปลี่ยน stale `RUNNING` กลับ `PENDING`
- ลำดับ claim คือ `(create_time_utc ASC, message_name ASC)`
- terminal item ห้าม claim ซ้ำ

### 5.3 Notification states

confirmation และ final notification มี state แยก:

```text
PENDING -> SENDING -> SENT
SENDING -> PENDING                 (recover/retry)
PENDING | SENDING -> FAILED        (bounded permanent failure)
```

final notification retry ห้ามเปลี่ยน item/job outcome และห้ามเรียก delete ซ้ำ

## 6. SQLite schema เป้าหมาย

ใช้ `PRAGMA user_version`, `journal_mode=WAL`, `synchronous=FULL`,
`foreign_keys=ON`, `busy_timeout=5000` และ migration lock ข้าม process

### 6.1 `clear_jobs`

เก็บอย่างน้อย:

- `operation_id TEXT PRIMARY KEY`
- `source_message_name TEXT NOT NULL UNIQUE`
- `requester_name TEXT NOT NULL`
- `space_name TEXT NOT NULL`
- `source_event_time_utc TEXT NOT NULL`
- `reference_time_utc TEXT NOT NULL`
- `cutoff_utc TEXT NOT NULL`
- `display_timezone TEXT NOT NULL`
- `normalized_argument TEXT NOT NULL`
- `status TEXT NOT NULL`
- `snapshot_complete INTEGER NOT NULL DEFAULT 0`
- `confirmation_message_name TEXT`
- `confirmation_client_message_id TEXT UNIQUE`
- `confirmation_delivery_generation INTEGER NOT NULL DEFAULT 0`
- `final_message_name TEXT`
- `final_client_message_id TEXT NOT NULL UNIQUE`
- `final_delivery_generation INTEGER NOT NULL DEFAULT 0`
- `confirm_token_hash BLOB`
- `cancel_token_hash BLOB`
- `created_at`, `updated_at`, `expires_at`, `next_attempt_at`
- `candidate_human`, `candidate_bot`, `candidate_skipped`
- `deleted_count`, `already_absent_count`, `skipped_count`, `failed_count`
- `user_partition_error`, `bot_partition_error`
- `final_notification_state`, `final_notification_attempts`,
  `final_notification_next_attempt_at`
- `safe_error_category`

ค่าจำนวนใน job เป็น cache สำหรับ render เท่านั้น ก่อนส่ง final summary ต้อง reconcile จาก
`clear_items` ใน transaction เดียวเพื่อป้องกัน count drift หลัง crash

### 6.2 `clear_items`

เก็บอย่างน้อย:

- `operation_id TEXT NOT NULL REFERENCES clear_jobs(operation_id) ON DELETE CASCADE`
- `message_name TEXT NOT NULL`
- `create_time_utc TEXT NOT NULL`
- `sender_name TEXT NOT NULL`
- `sender_type TEXT NOT NULL`
- `credential_partition TEXT NOT NULL` (`USER`, `BOT`, `NONE`)
- `status TEXT NOT NULL`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `claimed_at`, `next_attempt_at`
- `safe_error_category TEXT NOT NULL DEFAULT ''`
- `updated_at TEXT NOT NULL`
- `PRIMARY KEY (operation_id, message_name)`

หลัง `snapshot_complete=1` เปลี่ยนได้เฉพาะ status/attempt/retry/error timestamps;
identity columns ต้อง immutable ผ่าน store API และมี invariant tests

### 6.3 `space_write_pacer`

- `space_name TEXT PRIMARY KEY`
- `next_write_at_utc TEXT NOT NULL`
- `updated_at_utc TEXT NOT NULL`

reservation algorithm:

1. `BEGIN IMMEDIATE`
2. อ่าน `next_write_at`
3. `slot = max(now, next_write_at)`
4. persist `slot + 1.1 seconds`
5. commit
6. sleep นอก transaction จนถึง `slot`
7. ทำ remote write

callback `updateMessageAction` เป็น external synchronous write ที่ไม่ผ่าน REST gateway จึงต้อง
บันทึก conservative `next_write_at >= callback_time + 1.1s` ก่อนปลุก delete worker

### 6.4 Indexes และ invariants

- index งานที่ due: `(status, next_attempt_at, created_at)`
- index item ที่ due: `(operation_id, status, next_attempt_at, create_time_utc, message_name)`
- unique source message สำหรับ webhook deduplication
- partial unique index หรือ transaction invariant ให้มี active delete job ต่อ space ได้หนึ่งงาน
- ห้าม hold transaction ระหว่าง Google API call, sleep หรือ card rendering
- directory mode `0700`; DB/WAL/SHM/lock mode `0600`
- refuse database ที่ `user_version` ใหม่กว่าที่ binary รองรับ

## 7. Detailed phased implementation

## Phase 0 — Contract lock, baseline และ release safety

**เป้าหมาย:** ปิด ambiguity ที่มีผลต่อ parser/state/API recovery และสร้าง gate ที่เชื่อถือได้
ก่อนเริ่ม destructive path

### งาน

- [x] **H0.1 — ตรึง command lexical boundary**
  - ระบุว่า reserve `/chat` เมื่อใด และให้ test table ครอบ `/chatty`, `/chat!`,
    `/chat-clear`, `/chat\u00a0clear`
  - กำหนด whitespace ที่รับระหว่าง token; ข้อเสนอคือ ASCII space/tab เท่านั้น
  - CR/LF, Unicode control/whitespace และ trailing token ต้อง reject
- [x] **H0.2 — ตรึง time grammar/limits**
  - fractional seconds 1–6 หลัก
  - uppercase/lowercase `Z`, leap second, `24:00`, `-00:00`, leading zero และ offset
    รูปแบบต่าง ๆ ต้องมี accept/reject decision ชัดเจน
  - กำหนด `MAX_TIME_ARGUMENT_LENGTH` (ข้อเสนอ 64) และ
    `MAX_DURATION_COMPONENTS` (สูงสุด 6 หน่วย)
  - ถ้า `eventTime` มีแต่ parse ไม่ได้ให้ fail closed; fallback clock เฉพาะ field หาย
  - ยืนยันว่า cutoff เท่ากับ reference timeรับได้ แต่ cutoff ที่มากกว่า reject แบบ strict
- [ ] **H0.3 — ยืนยัน Add-on/API contract ด้วย sanitized fixtures**
  - เก็บ synthetic fixture ของ `chat.messagePayload` และ `chat.buttonClickedPayload` ตาม
    official contract โดยแยก WEB/ANDROID/IOS เป็นฐานทดสอบแล้ว
  - ระบุตำแหน่ง stable user/space/source message/confirmation message names และ fail-closed rule
  - ใช้ custom client-assigned `messageId` ต่อ delivery generation; ไม่ใช้ `requestId` ใน flow
    ที่ body มี plaintext action handles ซึ่งสร้างซ้ำหลัง crash ไม่ได้
  - กำหนด recovery ของ create timeout เป็น bounded `spaces.messages.get` ด้วย client ID ก่อน
    abandon generation และออก ID/handles ชุดใหม่
  - **ยังไม่ผ่าน:** ต้องมี live sanitized callback จาก WEB และ mobile อย่างน้อยหนึ่ง client,
    canonical REST/card binding, create-timeout recovery, bot sender identity และยืนยันสิทธิ์
    Developer Preview; synthetic fixture ไม่ถูกนับเป็นหลักฐานนี้
- [ ] **H0.4 — ยืนยัน deployment topology**
  - รองรับหนึ่ง persistent Linux host; หลาย process ได้เมื่อใช้ state directoryเดียวกันและ
    singleton worker ผ่าน `flock`
  - local development filesystem และ `flock` support ถูกตรวจแล้ว
  - **ยังไม่ผ่าน:** production runner/process count/preload, OS user, working directory,
    env injection, filesystem และ restart mechanism ต้องบันทึกจาก target host
  - SQLite+`flock` design นี้รองรับ single persistent host เท่านั้น; multi-host/NFS/ephemeral
    deployment ต้องเปลี่ยน store/lock ก่อน implement delete
- [x] **H0.5 — ทำ baseline gate ให้เป็นสีเขียว**
  - แก้ Ruff debt 3 จุดเดิมแบบ mechanical โดยไม่เปลี่ยน runtime behavior
  - ระบุ dev tooling/เวอร์ชัน Ruff ที่ใช้
  - รัน unit tests, Ruff, `pip check`, `git diff --check`
  - พิจารณาเพิ่ม CI บน Python 3.10 และ production version; หากเพิ่ม `.github/workflows`
    ต้องปรับ `.gitignore` ที่ปัจจุบัน ignore hidden directories
- [x] **H0.6 — กำหนด load/retry/retention policy**
  - maximum pages/items/job age และ disk budget
  - เมื่อชน cap ต้อง abort preview ทั้งงาน ห้าม truncate snapshot แล้วลบบางส่วน
  - list/get/delete timeout, max attempts, max elapsed, backoff cap, jitter
  - terminal ledger retention, pruning และ WAL checkpoint policy
- [x] **H0.7 — เขียน state-transition และ crash-window table**
  - ครอบก่อน/หลัง remote create, callback CAS, delete call, item commit และ final notification
  - ระบุ recovery action และ expected idempotent result ของทุก window

### Deliverables

- contract decisions เพิ่มใน `PLAN.md` หรือเอกสาร ADR ที่ link จาก `PLAN.md`
- synthetic sanitized official-contract fixtures ใน `tests/fixtures/` พร้อมแล้ว; live sanitized
  staging fixtures ตาม H0.3 ยังรอ external test
- baseline checks ผ่านทั้งหมด
- supported deployment topology ถูกตกลงแล้ว แต่ real target evidence ตาม H0.4 ยังรอยืนยัน

### Exit gate

```bash
python -m pip check
python -m unittest discover -s tests -v
ruff check .
git diff --check
git status --short
```

ตามคำสั่งให้จบทีละเฟส ห้ามเริ่ม Phase 1 จน H0.3 และ H0.4 มีหลักฐานจริงครบ แม้ local
contract/tests จะพร้อมแล้ว

## Phase 1 — Security, scopes, typed events และ route reservation

**เป้าหมาย:** ทำให้ history command/callback แยกจาก OpenClaw อย่างปลอดภัย โดย feature ยังปิด
และยังไม่มี list/delete API call

### งาน

- [x] **H1.1 — สร้าง `helpers/google_scopes.py`**
  - export Drive scopes, `CHAT_MESSAGES_SCOPE`, `USER_SCOPES`, `BOT_SCOPES`
  - ให้ `app.py`, `CredentialService` และ token helpers import จากที่เดียวกัน
- [x] **H1.2 — แก้ token helpers**
  - `get_token.py` และ `get_token_manual.py` ใช้ scopes กลาง
  - ลบ hard-coded authorization code จาก `manual_token.py`; เปลี่ยนเป็น secure input หรือ retire ไฟล์
  - เขียน `token.json` แบบ atomic mode `0600`
  - แจ้งชัดว่าต้องสร้าง token ใหม่เมื่อเพิ่ม `chat.messages`
- [x] **H1.3 — เพิ่ม credential readiness**
  - แยก error: missing file, invalid token, missing granted scope, refresh failure, reauthorize required
  - credential refresh/token file write ต้อง serialize ข้าม thread/process
  - ยังไม่ reauthorize production token ในเฟสนี้
- [x] **H1.4 — สร้าง `helpers/chat_events.py`**
  - dataclass/enum แยก `MESSAGE`, `BUTTON_CLICK`, `UNKNOWN`
  - normalize Add-on payload ก่อน และคง legacy message fallback
  - ดึง raw text โดยยังไม่ `.strip()`, attachments, stable resource names, event time และ
    safe parameters
  - malformed required field ต้องคืน typed validation error; ห้าม fallback ไป `displayName`
- [x] **H1.5 — สร้าง history command recognizer skeleton**
  - แยก `NOT_HISTORY`, `STATS`, `CLEAR`, `INVALID_HISTORY`
  - `/chatty` ต้องเป็น `NOT_HISTORY`
  - valid/invalid reserved command ต้องไม่ถึง provider แม้ full parser ยังไม่พร้อม
- [x] **H1.6 — แก้ logging order และ redaction**
  - `auth_verifier.verify()` ต้องเกิดก่อนบันทึก payload ที่ใช้งานได้
  - unauthorized/malformed request log เฉพาะ safe request metadata ไม่บันทึก raw body
  - recursive redaction ของ action parameters ทั้ง incoming/outgoing
  - `ChatGateway.record_incoming()` รับ parsed/redacted object แทน raw stringเมื่อทำได้
  - ห้าม exception log raw API body/handle/token
- [x] **H1.7 — เปลี่ยน route order ใน `app.py`**
  1. verify JWT
  2. parse JSON
  3. normalize event
  4. record redacted event
  5. route button callback ก่อน message/text path
  6. route history command ก่อน target learning, watcher และ orchestrator
  7. เฉพาะ normal message จึงคง legacy flow เดิม
- [x] **H1.8 — เพิ่ม defense-in-depth ใน `MessageOrchestrator`**
  - direct dispatch ของ reserved history command ต้องหยุดก่อนจับ `ProcessingGate`/download/provider
  - ใช้ recognizer เดียวกับ route เพื่อไม่ให้ grammar drift
- [x] **H1.9 — เพิ่ม settings validation/feature flags**
  - exact allowed user/space syntax
  - action URL validation จะ required เมื่อ delete enabled เท่านั้น
  - explicit outbound space fallback ต้องมาจาก env ไม่อ่าน learned target
- [x] **H1.10 — ป้องกัน state directory จาก outbound `MEDIA:`**
  - history DB/WAL/SHM/lock อยู่ใต้ blocked root
  - ขยาย file policy หากปัจจุบัน block ได้เฉพาะ exact file/subtree อื่น

### Tests

- `tests/test_chat_events.py`
- route cases เพิ่มใน `tests/test_chat_route_events.py`
- command reservation cases ใน orchestrator tests
- sentinel secret tests ของ `chat-in.jsonl`/`chat-out.jsonl`
- scope equality/static scan ว่าไม่มี authorization code/secret literal ใน tracked Python source
- regression ของ legacy payload, attachments, `/new`, `/model`, `/models`, `/abort`

### Exit gate

- auth เกิดก่อน logging
- callback และ reserved history command ไม่เรียก target learning, attachment download/watcher,
  `ProcessingGate` หรือ OpenClaw
- `/chatty` และ normal messages ทำงานเดิม
- ไม่มี code path เรียก `spaces.messages.list/delete`
- full existing suite + Ruff ผ่าน

## Phase 2 — Pure command and cutoff domain

**เป้าหมาย:** สร้าง parser ที่ deterministic และทดสอบได้โดยไม่มี Flask/Google/SQLite dependency

### งาน

- [x] **H2.1 — Implement command parser ใน `helpers/chat_history_time.py`**
  - parse outer whitespace ตาม contract แต่เก็บ raw text เพื่อ reject CR/LF/control characters
  - clear ต้องมี argument token เดียวและไม่มี extra token
  - invalid reserved form คืน usage category ไม่โยน raw parser exceptionขึ้น route
- [x] **H2.2 — Implement relative duration parser**
  - enforce unit order `y`, `mo`, `w`, `d`, `h`, `m`
  - integer positive, unit ไม่ซ้ำ, component count/argument length จำกัด
  - parse `mo` ก่อน `m` เพื่อไม่เกิด prefix ambiguity
- [x] **H2.3 — Implement absolute parser**
  - strict regex ก่อนเรียก stdlib parser เพื่อไม่รับ syntax นอก contract
  - date-only/naive datetime bind `Asia/Bangkok`
  - `Z`/offset normalize UTC
  - reject invalid date/time, overflow และ syntax ที่ไม่ได้ตกลงใน H0
- [x] **H2.4 — Implement calendar/fixed subtraction**
  - capture reference time ครั้งเดียว
  - ปี/เดือนแบบ calendar + end-of-month clamp
  - สัปดาห์/วัน/ชั่วโมง/นาทีแบบ fixed durationหลัง calendar subtraction
  - serialize UTC canonical RFC3339 และ Bangkok display text
  - cutoff compare แบบ strict ตาม H0
- [x] **H2.5 — Define typed results/errors**
  - parsed command มี normalized argument, reference UTC, cutoff UTC, display timezone
  - error categories ปลอดภัยต่อ user/log เช่น `invalid_syntax`, `invalid_datetime`,
    `future_cutoff`, `argument_too_long`, `too_many_components`

### Tests

- `tests/test_chat_history_time.py`
- ทุก accept/reject example ใน `PLAN.md`
- month-end, leap year, multi-month/year ordering
- offset equivalence, Bangkok midnight, `Z`, fractional seconds
- duplicate/reversed units, zero, negative, decimal, uppercase, garbage suffix
- missing/extra args, Unicode whitespace/control, CR/LF, overflow/length limits
- eventTime retry determinism และ fallback clock เรียกเพียงครั้งเดียว

### Exit gate

- parser เป็น pure logic และ coverage ครบ contract table
- ไม่มี dependency ใหม่สำหรับเวลา
- same event + same source time ให้ normalized argument/reference/cutoff เท่ากันทุก retry

## Phase 3 — Read-only Google Chat client, shared pacer และ `/chat`

**เป้าหมาย:** เปิด `/chat` แบบ read-only canary โดย destructive flag ยังปิด

### งาน

- [x] **H3.1 — สร้าง `helpers/chat_history_client.py` read side**
  - user-auth Chat client พร้อม explicit timeout
  - `spaces.get` และ canonical `name` validation
  - assert `spaceType == DIRECT_MESSAGE` และ `singleUserBotDm == true`
  - allowlist check ต้องเกิดก่อน API call
- [x] **H3.2 — Implement message pagination**
  - base params: `parent`, `pageSize=1000`, `showDeleted=false`, partial fields
  - เปลี่ยนเฉพาะ `pageToken` ต่อหน้า
  - รองรับ `{}` และไม่มี `messages`
  - ป้องกัน repeated/cyclic page token และ enforce Phase 0 limits
  - bounded retry/deadline สำหรับ get/list โดยไม่ log raw response body
- [x] **H3.3 — Implement stats reducer**
  - stream metadata โดยไม่เก็บ text/cards/attachment
  - exclude source command ด้วย exact `message.name`
  - count HUMAN/BOT/unknown, min/max createTime และ duration
  - ห้ามสมมติ API ordering
  - malformed record policy ต้องชัด; safest default คือ abort statsพร้อม safe errorแทน partial number
- [x] **H3.4 — เพิ่ม durable per-space pacer**
  - implement reservation ใน `helpers/chat_clear_store.py` หรือ class ย่อยที่ใช้ DB เดียวกัน
  - inject pacer เข้า `ChatGateway` โดยรักษา public boolean behavior เดิม
  - `_post_message()` คืน parsed response และ validate `message.name`
  - เพิ่ม structured-card send method ที่คืน message resource
  - every Chat REST write ผ่าน gateway ใช้ pacerเดียวกัน ไม่เฉพาะ history write
- [x] **H3.5 — เพิ่ม stats presenter**
  - header `🛠️ ผู้ดูแลระบบ`
  - count split, first/latest/range ใน Bangkok
  - zero state ไม่มี range
  - sanitize API-derived valuesก่อน render
- [x] **H3.6 — Implement async stats service**
  - route validate event/allowlist แล้ว ACK ทันที
  - background task validate canonical DM, list/reduce และส่ง card
  - duplicate source webhook ใช้ deterministic custom client message ID เพื่อไม่ตั้งใจส่ง stats ซ้ำ
  - failure ส่ง safe admin card; missing scopeบอก reauthorize ชัดเจน
- [x] **H3.7 — Wire stats-only mode**
  - `GCHAT_HISTORY_ENABLED=true`
  - `GCHAT_HISTORY_DELETE_ENABLED=false`
  - clear command ยังถูก reserve แต่ไม่สร้าง operation

### Tests

- `tests/test_chat_history_client.py`
- stats/service cases ใน `tests/test_chat_history_service.py`
- gateway regression ใน `tests/test_chat_gateway_file_delivery.py`
- fake API block test ยืนยัน webhook ACK ไม่รอ network
- `{}`, one page, >1,000 messages, token cycle, timeout/retry, invalid record
- params invariant, partial fieldsไม่มี content, source exclusion, timezone/count/range
- wrong user/space/non-DM ไม่มี list call
- all Phase 3 tests ต้องมี delete call count เท่ากับ 0
- fake-clock pacer รวม normal gateway writes ข้าม thread/process

### Exit gate

- automated tests/full regression/lint ผ่าน
- deploy test environment แบบ stats-only
- `/chat` ตรงกับ independent count และ Bangkok display
- missing/revoked scope/non-DM ได้ safe failure และไม่มี bot/user credential fallback
- read-only canary เสถียรก่อนเริ่ม confirmation code

## Phase 4 — Durable clear store, scheduler และ recovery โดยยังไม่ delete

**เป้าหมาย:** ทำ durable state machine ให้ครบก่อนมีปุ่มหรือ remote delete side effect

### งาน

- [x] **H4.1 — สร้าง state directory อย่างปลอดภัย**
  - reject symlink/invalid path ตาม deployment policy
  - mode `0700`; DB/WAL/SHM/locks `0600`
  - validate `ZoneInfo("Asia/Bangkok")`, disk writability และ schema compatibilityใน preflight
- [x] **H4.2 — Implement schema/migration**
  - transaction + migration process lock
  - `user_version`, tables/indexes/invariants ตาม section 6
  - refuse newer/corrupt DB แบบ fail closed
- [x] **H4.3 — Implement job creation/dedup transaction**
  - unique source message คืน operation เดิมเมื่อ webhook retry
  - one active job ต่อ allowed space
  - reject new clear ถ้ามี `DELETE_QUEUED`/`RUNNING`
  - cancel/supersede old `PREVIEW_QUEUED`/`PREPARING`/`PENDING_CONFIRMATION`
    และ insertใหม่ใน transaction เดียว
- [x] **H4.4 — Implement snapshot transaction API**
  - claim `PREVIEW_QUEUED -> PREPARING`
  - incomplete snapshot insert ได้แต่ `snapshot_complete=0`
  - recovery ลบ/rebuild incomplete itemsก่อน list ใหม่
  - finalize counts + `snapshot_complete=1` atomically
  - ห้ามเปลี่ยน immutable columnsหลัง pending
- [x] **H4.5 — Implement action CAS API**
  - token hash, requester, space, confirmation card, expiry และ state อยู่ใน
    `BEGIN IMMEDIATE` transaction เดียว
  - confirm/cancel race มีผู้ชนะหนึ่ง transition
  - repeated callback หลังผู้ชนะต้องคืน current safe status แบบ idempotent
- [x] **H4.6 — Implement item claim/result API**
  - oldest-first + message name tie-breaker
  - claim/commit แยกจาก network call
  - transient reset, terminal result, partition failure และ count reconcile
- [x] **H4.7 — Implement worker supervisor skeleton**
  - singleton `flock`, startup poll และ periodic pollแม้ไม่มี webhook wakeup
  - wake event เป็น optimization ไม่ใช่ durability mechanism
  - expire pending, recover incomplete preview/stale running item
  - lifecycle start/stop/idempotent startup แยกจาก existing outbound watcher
  - ยังไม่มี delete executorในเฟสนี้
- [x] **H4.8 — Implement retention/maintenance**
  - prune terminal jobsตาม policyโดยไม่แตะ active jobs
  - WAL checkpoint/backup-safe operation
  - structured diagnostic: state counts, oldest active age, worker owner/heartbeat

### Tests

- `tests/test_chat_clear_store.py`
- fresh/old/newer/corrupt schema, permissions, WAL/FK/busy timeout
- same source dedup, one-active rule, supersession, no-op, expiry
- snapshot incomplete/frozen invariants
- separate SQLite connections + multiprocessing confirm/cancel races
- crash recoveryทุก nonterminal state, DB busy/disk error, singleton owner
- DB byte/iterdump scan ไม่มี message text/raw handle/token
- pacer persistence/restart/clock cases

### Exit gate

- restart ได้ทุก state โดยยังไม่มี external delete
- no network call inside transaction
- invariants และ multiprocessing tests ผ่าน
- store unavailable/corrupt ทำให้ history fail closed แต่ไม่ส่ง corrupt operationไป worker

## Phase 5 — Clear preview, confirmation card และ cancel callback

**เป้าหมาย:** สร้าง exact snapshot และ secure confirmation flow; delete executor ยังถูกปิด
จนทุก race/crash test ผ่าน

### งาน

- [x] **H5.1 — รับ `/chat clear <เวลา>` แบบ durable ก่อน ACK**
  - normalize/authorize/parse cutoff
  - persist `PREVIEW_QUEUED` ด้วย source message dedup
  - attachment ไม่ถูก download/forward และส่งข้อความว่า skipped
  - ACK หลัง durable insertสำเร็จ; DB unavailableได้ safe synchronous error
- [x] **H5.2 — Build preview worker**
  - revalidate allowlist + canonical DMก่อน list
  - list filter exact `createTime < "<cutoff UTC>"`
  - paginate ให้จบ; malformed record/cap/page failure abortทั้ง snapshot ห้าม truncate
  - validate ทุก message resource อยู่ใต้ allowed space
  - classify HUMAN requester=`USER`, BOT=`BOT`, unknown/HUMAN mismatch=`NONE/SKIPPED`
  - ไม่เก็บ text/cards/attachment metadata
- [x] **H5.3 — Handle zero/no-deletable candidate**
  - terminal no-op summary
  - ไม่ generate handles, ไม่สร้าง buttons และไม่ enqueue delete
- [x] **H5.4 — Generate action handles**
  - `secrets` entropy 256-bit แยก confirm/cancel
  - store SHA-256 digest เท่านั้นและ compareแบบ constant-time
  - card parameter มีเฉพาะ opaque handle; ไม่มี operation/cutoff/count/message/partition
- [x] **H5.5 — Build confirmation presenter**
  - header, Bangkok cutoff, strict-before wording, HUMAN/BOT/skipped counts
  - minimum estimate จาก 1.1 seconds/write + card/final overhead
  - TTL 10 นาทีและปุ่ม confirm/cancel
  - `onClick.action.function` ใช้ validated full URLจาก configเท่านั้น
- [x] **H5.6 — Idempotent card delivery/binding**
  - persist unique custom client message ID และ delivery generation ก่อน remote create
  - ก่อน post ตรวจ job ยัง `PREPARING` และไม่ถูก supersede
  - capture actual canonical `message.name`
  - bind message, hashes, expiry และ transition `PENDING_CONFIRMATION` atomically
  - create timeout/crash ใช้ bounded GET ด้วย client ID; ห้าม re-create body ใหม่ด้วย ID เดิม
  - ถ้ายัง absent หลัง reconciliation window ให้ CAS abandon generation แล้วออก ID/handles ชุดใหม่
  - late response และ orphan/unbound callback จาก generation เก่า fail closed
- [x] **H5.7 — Implement button callback**
  - normalize callbackก่อน text path
  - hash handle แล้ว validate user/space/card/TTL/current stateใน transactionเดียว
  - cancel: transition `CANCELLED`, return synchronous update card ไม่มีปุ่ม
  - confirm: transition `DELETE_QUEUED`, return running update card ไม่มีปุ่ม
  - record callback updateใน pacer แล้วค่อย wake worker
  - duplicate callback คืน running/cancelled/expired state idempotently
  - unauthorized/forged callbackไม่เปลี่ยน stateและไม่เผยว่าข้อใด mismatch
- [x] **H5.8 — Expiry/superseded card cleanup**
  - worker expire state atomically
  - best-effort status update แยก notification state; update failห้าม enqueue delete
  - new clear supersede old preview/pendingก่อน old worker post cardได้
- [x] **H5.9 — Keep destructive execution disabled**
  - test/staging ยืนยัน delete spy มี call count 0
  - productionไม่แสดง confirm buttonจน Phase 6 binaryพร้อมและ delete flag rolloutได้รับอนุมัติ

### Tests

- `tests/test_chat_clear_confirmation.py`
- presenter casesใน history service/card tests
- route regressionใน attachment notification tests
- exact snapshot, new/backdated messageหลัง previewไม่อยู่ใน snapshot
- wrong same-displayName user, wrong space/card, forged handle, expired handle
- callback-before-bind, click retry, double click, confirm/cancel race, supersede race
- stable card recoveryหลัง timeout/crash
- invalid/missing/action URL and hostile Host header
- handle sentinel scanใน app logs, outgoing logs, DB/WAL
- synchronous callback testด้วย mocked dependencies <1 วินาที

### Exit gate

- web/mobile test card render/clickได้
- cancel/expiry/unauthorized/supersede ทุกกรณีมี delete calls = 0
- plaintext handleไม่อยู่ใน log/SQLite
- snapshot freeze และ card binding invariantsผ่าน
- kill switch/feature flagsทำงานก่อน merge Phase 6

## Phase 6 — Sender-routed delete worker, retry และ final summary

**เป้าหมาย:** เปิด remote deletion อย่าง recoverable, quota-safe และไม่มี credential fallback

### งาน

- [x] **H6.1 — เพิ่ม delete clients**
  - user and bot Chat clients แยกชัดเจน
  - `delete(name=..., force=False)` เท่านั้น
  - validate message name อยู่ใต้ bound allowed spaceทุกครั้งก่อน call
  - credential partitionมาจาก immutable snapshot ไม่ตัดสินใหม่จาก error
- [x] **H6.2 — Implement execution loop**
  - claim item + commit
  - reserve per-space write slot + sleepนอก transaction
  - call API
  - persist resultทันที
  - check kill switchก่อน claim itemถัดไป
  - ไม่ถือ `ProcessingGate` หรือ global app lockตลอด job
- [x] **H6.3 — Implement error classifier/retry**
  - `404 -> ALREADY_ABSENT`
  - `429 -> Retry-After` (seconds/HTTP-date) + truncated exponential backoff/jitter
  - timeout/reset/5xx -> bounded transient retry
  - `401` refresh/rebuild credentialหนึ่งครั้งต่อ partition แล้ว terminal/systemic
  - `400/403/other 4xx` -> safe permanent category; ไม่วน retry
  - raw response/error bodyไม่เข้า DB/log/user card
- [x] **H6.4 — Partition circuit breaker**
  - distinguish credential-wide failureจาก item-specific permission failure
  - systemic user failure mark remaining USER items failedโดยไม่ยิงซ้ำ
  - systemic bot failureทำเฉพาะ BOT partition
  - อีก partitionดำเนินต่อได้
  - ห้าม fallback USER↔BOTทุกกรณี
- [x] **H6.5 — Crash recovery**
  - startup reset stale RUNNING itemตาม lease/recovery rule
  - remote delete successก่อน commit: retryเดิมและรับ 404เป็น already absent
  - terminal itemsไม่ถูก callซ้ำ
  - singleton process ownerหนึ่งราย; standby takeoverเมื่อ ownerออก
- [x] **H6.6 — Finalize job deterministically**
  - reconcile item outcomesใน transaction
  - `COMPLETED`: ไม่มี failed item
  - `PARTIAL_FAILED`: มีทั้ง success/absentและ failed หรือมีบาง partition fail
  - `FAILED`: ไม่มี deletable itemสำเร็จและมี failureถาวร
  - invariant candidate = deleted + already absent + skipped + failed
- [x] **H6.7 — Final summary delivery**
  - persisted custom final client message ID และ canonical response binding
  - deleted/already absent/skipped/failed และ HUMAN/BOT splitตามที่มีประโยชน์
  - retry notificationแยกจาก delete states
  - final notification failureห้าม resetหรือ execute terminal item
- [x] **H6.8 — Shared quota integration**
  - gateway normal text/card/file messages, confirmation/final card และ deleteใช้ pacerเดียวกัน
  - callback updateเลื่อน first deleteอย่างน้อย 1.1 วินาที
  - fake-clock testsยืนยันทุก adjacent writeใน spaceเดียวกันห่างตามขั้นต่ำ
- [x] **H6.9 — Operational safety**
  - structured logsมี operation ID/state/count/duration/safe category ไม่มี content/handle
  - diagnostic state counts, oldest job age, worker heartbeat, partition failures
  - alert/runbook triggerสำหรับ stuck running, repeated auth failure, DB full/corrupt,
    final notificationค้างและ WALโตผิดปกติ

### Tests

- `tests/test_chat_history_worker.py`
- exact-set assertion เทียบ immutable snapshot
- mixed HUMAN/BOT/unknown, oldest-first + tie-breaker, `force=False`
- 404/429/Retry-After/5xx/timeout/reset/401 refresh/nonretry 4xx
- systemic failureหยุดเฉพาะ partitionและไม่มี fallback
- fake clock/sleeper/randomเพื่อไม่ให้ suite sleepจริง
- fault injection:
  - หลัง claimก่อน API call
  - หลัง API successก่อน item commit
  - หลัง item terminalก่อน job reconcile
  - หลัง job terminalก่อน final notification
- restartโดยไม่มี webhookใหม่, two-process singleton, standby takeover
- final notification retryไม่เพิ่ม delete call count
- normal OpenClaw/attachment trafficระหว่าง deleteไม่ถูก `ProcessingGate` blockและยังถูก pace

### Exit gate

- automated crash/race/retry suiteผ่านทั้งหมด
- full regression + Ruff + diff checkผ่าน
- manual destructive pilotใน test DMจำนวนน้อยพิสูจน์ exact message names/timestamps/outcome
- ไม่มี credential fallback, no out-of-snapshot delete และ no duplicate terminal execution

## Phase 7 — Integration hardening, documentation และ deployment tooling

**เป้าหมาย:** ทำให้ binary ที่ทดสอบเป็น binary เดียวกับที่ deploy และมี runbook ก่อน rolloutจริง

### งาน

- [ ] **H7.1 — App lifecycle integration**
  - initialize settings/store/pacer/client/service/workerในลำดับที่ fail closed
  - worker resumeตอน process startup ไม่รอ webhookแรก
  - idempotent start/stopและ `atexit` timeoutที่เหมาะสม
  - multi-process preload/fork behaviorตรงกับ topologyใน H0
- [ ] **H7.2 — Readiness/preflight**
  - แยก liveness `/` จาก history readinessหรือเพิ่ม diagnostic command/script
  - ตรวจ config, timezone DB, state dir/schema, worker lease, granted scopes และ allowed DM access
  - outputไม่เปิดเผย token/resourceเกินจำเป็น
- [ ] **H7.3 — README**
  - command grammar/examples, DM-only, strict cutoff, TTL, quota และ partial results
  - scopes/reauthorize/restricted scope/admin policy
  - env matrix/feature flags/action URL/state directory
  - local logs/OpenClaw/Drive/Vaultที่ไม่ถูกลบ
  - recovery, missing/revoked token, permission failures, stuck job
  - แก้ path token helpersและ project file referencesที่ปัจจุบันล้าสมัย
- [ ] **H7.4 — Operations runbook**
  - start/stop/restart, worker ownership, queue inspection
  - SQLite checkpoint-aware backup/restore; ห้าม copy main DBตอน WAL active
  - kill switch, pending expiry, stuck RUNNING, auth reauthorize, DB migration
  - ระบุว่าข้อความที่ลบสำเร็จแล้ว undoไม่ได้ และ confirmแล้วไม่มี user cancelระหว่าง RUNNING
- [ ] **H7.5 — Deployment safety**
  - ห้ามใช้ `deploy.sh` ปัจจุบันกับ branchนี้โดยตรงเพราะดึง `development`
  - deploy exact tested commit SHA/tagแทน mutable branch ZIP
  - หลีกเลี่ยง overlayที่ทิ้ง stale source; ใช้ staged release + controlled switchตาม topologyจริง
  - clean environment/constraints, test/lint/preflightก่อน restart
  - post-restart verify SHA, liveness, history readiness และ singleton owner
- [ ] **H7.6 — CI/reproducibility**
  - เพิ่ม Linux CIอย่างน้อย Python 3.10และ production version หรือระบุ equivalent release gate
  - pin dev tools; ตัดสิน runtime constraints/lock strategy
  - `ZoneInfo("Asia/Bangkok")` ต้องโหลดได้ใน clean environment; เพิ่ม `tzdata` เฉพาะถ้า
    production imageไม่มี system tz database

### Exit gate

- clean install/full test/lint/preflightผ่านจาก exact release SHA
- restart recoveryไม่ต้องพึ่ง webhookใหม่
- backup/restoreและ kill-switch drillผ่าน
- README/runbook/manual acceptanceพร้อมก่อนเปิด delete flag

## Phase 8 — Staged production rollout

**เป้าหมาย:** เปิด featureทีละ capability พร้อมสังเกตผลและ rollbackได้โดยไม่แตะ ledgerผิดวิธี

### Stage A — Configuration only

- [ ] checkpoint-aware backup stateเดิม
- [ ] ตั้ง explicit allowed user/space, state dirและ flagsปิด
- [ ] deploy security/routing baselineจาก exact SHA
- [ ] ตรวจว่า history commandsถูก reserveและ normal trafficไม่ regress

### Stage B — Read-only stats

- [ ] reauthorize `token.json` ด้วย `chat.messages`
- [ ] ตรวจ granted scopeจริงและ allowed DM preflight
- [ ] เปิด `GCHAT_HISTORY_ENABLED=true`, delete flagยัง false
- [ ] run `/chat`, compare count/timezoneกับ independent inspection
- [ ] observeหนึ่งช่วงใช้งานจริงและตรวจ auth/page/pacer errors

### Stage C — Preview/cancel acceptance

- [ ] deploy full binaryแต่คง delete flagปิดจน smoke testsผ่าน
- [ ] ใน controlled test window เปิด clear, สร้าง small preview แล้วกด cancel
- [ ] ทดสอบ expiry, forged/wrong actor, double click และ web/mobile
- [ ] scan application/proxy logsและ SQLiteด้วย sentinel handle

### Stage D — Destructive canary

- [ ] เตรียม test DMที่รู้ exact old/new boundary
- [ ] เริ่มด้วย candidateจำนวนน้อย เช่น `/chat clear 30m`
- [ ] confirmและตรวจ API timestamps, oldest-first, exact snapshotและ strict cutoff
- [ ] restartระหว่าง controlled jobแล้วตรวจ resume
- [ ] ทดสอบ user token revoke/partial failureโดยไม่ fallback
- [ ] ทดสอบ final notification retryแยกจาก delete

### Stage E — Full enablement

- [ ] manual acceptance checklistใน `PLAN.md` ผ่านครบ
- [ ] backlog, oldest job age, auth errors, 429s, pacer delays, WAL sizeปกติ
- [ ] rollback drillจาก pendingและ terminal jobผ่าน
- [ ] เปิด delete flagถาวรเฉพาะ allowed DM

### Rollback

1. ปิด `GCHAT_HISTORY_DELETE_ENABLED` ก่อน เพื่อหยุด claim itemใหม่หลัง requestปัจจุบัน
2. ตรวจ job stateและเก็บ ledgerเดิม ห้ามลบ DB/WAL/lockเพื่อ “reset”
3. ปล่อย pending confirmationให้ expireหรือ updateเป็น unavailableตาม runbook
4. rollbackเฉพาะ binaryที่ยังรองรับ security routing/redactionและ schemaปัจจุบัน
5. ห้าม rollbackกลับ codeที่ log raw callbackหรือส่ง callbackเข้า OpenClaw
6. เมื่อ re-enable ให้ singleton worker resumeจาก nonterminal items; terminal itemsห้าม executeซ้ำ
7. การลบที่ remoteสำเร็จแล้วไม่สามารถ rollbackข้อมูลได้

## 8. Test suite mapping

| Test file | ขอบเขต | เริ่มใน phase |
|---|---|---|
| `tests/test_chat_history_time.py` | command/time grammar | 2 |
| `tests/test_chat_events.py` | Add-on/legacy normalization | 1 |
| `tests/test_chat_route_events.py` | auth/log/route precedence | 1 |
| `tests/test_chat_history_client.py` | DM/list/pagination/delete classifier | 3/6 |
| `tests/test_chat_clear_store.py` | schema/state/CAS/recovery/pacer | 4 |
| `tests/test_chat_clear_confirmation.py` | card/handle/callback/races | 5 |
| `tests/test_chat_history_service.py` | stats/preview orchestration | 3/5 |
| `tests/test_chat_history_worker.py` | delete/retry/recovery/final | 6 |
| existing gateway/attachment/orchestrator tests | regression และ shared pacing | ทุก phase |

ทุก phase ต้องรัน targeted tests ก่อน แล้วจึงรัน full gate:

```bash
python -m pip check
python -m unittest discover -s tests -v
ruff check .
git diff --check
```

Tests ที่มีเวลา/backoff/pacing ต้องใช้ injected fake clock/sleeper/random ห้าม sleep 1.1 วินาทีจริง
ใน unit suite ส่วน race ข้าม processต้องมีอย่างน้อยหนึ่ง multiprocessing test ไม่พึ่งเฉพาะ threads

## 9. Crash-window acceptance matrix

| Crash/failure point | State หลัง restart | Recovery ที่ต้องได้ |
|---|---|---|
| หลัง insert jobก่อน ACK | `PREVIEW_QUEUED` | webhook retry dedup; workerทำ previewครั้งเดียว |
| ระหว่าง pagination | `PREPARING`, snapshot incomplete | ทิ้ง/rebuild incomplete snapshot |
| หลัง snapshot completeก่อน card create | `PREPARING` | ส่ง confirmationด้วย persisted client message ID/generation |
| remote card createสำเร็จก่อน bind | `PREPARING` | GET ด้วย client ID แล้ว bind; generation เก่าที่ orphan ต้องกดใช้งานไม่ได้ |
| click CASสำเร็จก่อน HTTP response | `DELETE_QUEUED`/`CANCELLED` | callback retryคืน current state ไม่ claimซ้ำ |
| หลัง callback updateก่อน first delete | `DELETE_QUEUED` | pacerบังคับระยะอย่างน้อย 1.1s |
| หลัง item claimก่อน API call | item `RUNNING` | stale claimกลับ `PENDING` |
| remote deleteสำเร็จก่อน item commit | item `RUNNING` | retryแล้ว 404 => `ALREADY_ABSENT` |
| หลัง item terminalก่อน count update | terminal item | reconcile countsจาก items |
| หลัง job terminalก่อน final card | terminal job + notification pending | retryเฉพาะ notification |
| final cardสำเร็จก่อน notification commit | notification `SENDING` | GET ด้วย final client ID แล้ว mark sent; ไม่ deleteซ้ำ |

## 10. Suggested review/commit slices

1. baseline lint + scope/token security cleanup
2. typed event normalizer + auth-before-log + redaction
3. command/time parser + tests
4. read-only Chat client + stats reducer
5. durable pacer + gateway response/structured-card support
6. `/chat` service/routing + stats-only rollout docs
7. SQLite schema/store/recovery
8. preview snapshot + presenters + idempotent card binding
9. confirm/cancel callback + concurrency tests
10. delete client/worker + retry/circuit breaker
11. final notification + recovery/observability
12. lifecycle/readiness/deploy/runbook/README

แต่ละ slice ต้อง reviewได้โดยไม่ต้องเปิด destructive flag และห้ามรวม secure callback bindingกับ
delete workerเป็น commit/PRแรกเดียวกัน

## 11. Definition of Ready สำหรับเริ่ม implement

- Phase 0 command/time/API decisions ถูกบันทึกใน ADR และมี contract fixtures
- production target ยืนยันว่าเป็น single persistent host + local filesystem + `flock`
- custom client message ID/generation recovery และ callback binding rule ถูกตรึง
- live sanitized web/mobile fixture พิสูจน์ callback message binding, bot sender identity,
  Developer Preview entitlement และ create-timeout reconciliation
- baseline tests/lintเป็นสีเขียว
- feature flags defaultปิดและ rollback owner/runbook skeletonพร้อม

## 12. Definition of Done

ใช้ Definition of Done ใน `PLAN.md` เป็นหลัก และเพิ่ม operational conditions ต่อไปนี้:

- exact tested SHA คือ artifactเดียวกับที่ deploy
- worker resumeจาก startupโดยไม่ต้องมี webhookใหม่
- kill switchหยุด claim delete itemใหม่ได้
- backup/restore/schema migration/rollback drillผ่าน
- DB/log/proxy-log reviewไม่พบ plaintext handleหรือ message contentที่ history client fetch
- one-space pacingครอบ normal gateway writes, callback update, deleteและ final notification
- full automated gate, manual test DM, web/mobile card และ destructive canaryผ่านครบ
