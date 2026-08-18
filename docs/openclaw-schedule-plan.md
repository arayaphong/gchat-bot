# แผน: รองรับการสั่ง schedule จาก Google Chat ผ่าน OpenClaw cron

> สถานะ 2026-08-13: **implement แล้วและทดสอบ end-to-end ผ่าน** — เอกสารนี้เก็บ
> บริบทการออกแบบ ทางเลือกที่ลองแล้วไม่เวิร์ก และโครงสร้างโค้ดจริงไว้เป็นคู่มือดูแลต่อ

## เป้าหมาย

ให้ผู้ใช้สั่งงานล่วงหน้าได้จาก Google Chat เช่น "เตือนฉันอีก 10 นาที" แล้วผลลัพธ์ถูกส่งกลับมาที่ **thread เดิม** โดยไม่ต้องพึ่งนาฬิกาภายนอก

## หลักการ (ผ่านการทดสอบจริง)

cron job ของ OpenClaw ถูก pin เข้า session ของ thread ด้วย
`--session "session:<session_key>"` เมื่อครบกำหนด OpenClaw inject `--message`
เข้า session เดิม agent ตอบใน trajectory ของ session นั้น และ
`SessionTrajectoryWatcher` ของบอทส่งกลับ Google Chat thread อัตโนมัติ —
ไม่ต้องสร้าง channel delivery ใหม่

session key derive จาก event เสมอ (`ChatSessionContext` / `derive_session_key`)
รูปแบบ `agent:main:gchat:<space>:<thread>` — ห้ามรับ key จากข้อความผู้ใช้

### ทางเลือกที่ลองแล้วไม่เวิร์ก (บันทึกไว้กันทำซ้ำ)

- `--session main --session-key <key> --system-event ...` — run ไปออกใน
  isolated cron session (`agent:main:cron:<jobId>:run:<ts>`) ไม่เข้า gchat
  session และ openclaw ไม่มีผู้รับ system event ใน session ปลายทาง
- announce delivery ของ OpenClaw (`--announce --channel last`) — resolve เป็น
  channel `googlechat` แล้วล้มเหลว "Unsupported channel" เพราะ integration
  นี้เป็น custom webhook ของบอท ไม่ใช่ channel plugin ของ OpenClaw —
  การส่งกลับจึงต้องพึ่ง watcher ของบอทเท่านั้น

### เงื่อนไขสำคัญที่พบระหว่างทดสอบ

- watcher ต้อง **start ตั้งแต่ boot** (`app.py` `__main__`) ไม่ใช่รอ message
  เข้า — ไม่อย่างนั้น output ของ cron ที่ยิงระหว่างที่ไม่มี traffic จะค้างใน
  trajectory จนกว่าจะมีคนทัก (cursor persist อยู่แล้วใน
  `~/.openclaw/state/jinx-gchat/attachments/trajectory-cursors.json` แต่
  thread poll ไม่ได้ตื่น)
- message ที่ inject ต้องห่อด้วยบริบท ("การแจ้งเตือนที่ตั้งเวลาไว้ครบกำหนดแล้ว…")
  ไม่อย่างนั้น agent อาจเข้าใจผิดว่าเป็นคำสั่งตั้งเตือนใหม่

## โครงสร้างโค้ดที่ implement แล้ว

### 1. `helpers/schedule_commands.py` (ไฟล์ใหม่)

- `parse_schedule_args(args)` — แปลง argument หลัง `/schedule` เป็น
  usage / list / cancel / add รองรับ `+10m`, `in 1h`, `at 18:30`,
  `at 2026-08-14 09:00` (เวลา `HH:MM` ที่เลยไปแล้วเลื่อนเป็นพรุ่งนี้,
  ล็อก lead time ไม่เกิน 366 วัน)
- `build_cron_add_argv(spec, session_key)` — สร้าง argv ของ
  `openclaw cron add` (argv list ล้วน ไม่ผ่าน shell — message ที่มี
  `$(...)`/backtick ปลอดภัย) พร้อม `--delete-after-run`, `--tz Asia/Bangkok`,
  `--agent main`, `--display-name` เก็บข้อความผู้ใช้ไว้แสดงใน list
- `list_session_jobs` / `resolve_session_job` — กรองเฉพาะ job ของ session
  นี้ (match ทั้ง `sessionTarget: session:<key>` และ `sessionKey`) และ
  cancel ได้เฉพาะ job ของ thread ตัวเอง (รองรับ unique prefix ของ id)
- `add_session_job` / `remove_session_job` — wrapper ที่แปลง nonzero exit /
  JSON เพี้ยนเป็น RuntimeError

### 2. `helpers/providers/openclaw_cli.py`

เพิ่ม `cron_add(argv)`, `cron_list()` (`--all --json`), `cron_remove(job_id)`
ตาม pattern ฟังก์ชัน CLI เดิม (timeout 15s, resolve binary จาก nvm)

### 3. `helpers/message_orchestrator.py`

- เพิ่ม `_SCHEDULE_COMMAND_RE` และ dispatch `/schedule` แบบ bypass (ไม่ยึด
  processing gate เพราะเรียกแค่ cron CLI ไม่แตะ agent session)
- `_handle_schedule` ตอบผลใน thread เดิมทุกกรณี รวมถึง error (FileNotFoundError,
  timeout, CLI ล้มเหลว) — คำสั่ง schedule พังต้องไม่ทำ flow หลักพัง

### 4. `helpers/orchestrator_messages.py`

formatter ภาษาไทย: `format_schedule_add_success` (แสดงเวลา + id สำหรับ
ยกเลิก), `format_schedule_list`, `format_schedule_cancel_success`,
`format_schedule_failure`, `USAGE_TEXT` อยู่ใน schedule_commands

### 5. `helpers/providers/openclaw_provider.py` + `openclaw_prompts.py`

`build_openclaw_prompt` แนบ block `[SESSION_CONTEXT] sessionKey: ...` และ
`[THREAD_UPLOAD_DIRECTORY] <path>` ทุกข้อความเหมือนเดิม (เคยลอง gate ด้วย
regex ตรวจ intent ข้อความ เช่น remind/schedule/เตือน แล้วเปลี่ยนใจเมื่อ
2026-08-18 — regex คลุม intent ได้ไม่ครบทุกคำ/ทุกภาษา และ false negative
แปลว่า reminder/upload พังเงียบๆ ในเทิร์นที่ regex miss) แต่ตั้งแต่
2026-08-18 เอา instruction ร้อยแก้ว (`SESSION_CONTEXT_INSTRUCTION` /
`THREAD_UPLOAD_INSTRUCTION` เดิม) ออกจาก block รายข้อความ เหลือแค่
tag + ค่าจริง เพราะคำอธิบายวิธีใช้ซ้ำกับสิ่งที่ `AGENTS-EXTRA.md` (deploy
ครั้งเดียวเข้า `AGENTS.md`) สอน agent อยู่แล้ว — ลด token ต่อข้อความลง
~70-80% โดยไม่มี intent-miss risk เลย เพราะ agent (ไม่ใช่ regex) เป็นคน
ตัดสินใจว่าจะใช้ sessionKey/upload dir หรือไม่ ข้อมูลแค่ "พร้อมใช้เสมอ"

### 6. `harness/AGENTS-EXTRA.md`

เพิ่มกฎ `[SYSTEM CAPABILITY: SCHEDULED REMINDERS]` สั่ง agent: ใช้
`openclaw cron add` เท่านั้น (ห้าม `sleep`), pin `--session session:<key>`
จาก SESSION_CONTEXT เสมอ, `--tz Asia/Bangkok`, one-shot ต้อง
`--delete-after-run`, `--agent main` (ไฟล์นี้ถูกแทรกเข้า
`~/.openclaw/workspace/AGENTS.md` ตอน deploy ผ่าน `deploy.sh`)

### 7. `app.py`

`session_message_watcher.start()` ตอน `__main__` (ดูเงื่อนไขสำคัญด้านบน)

### 8. Tests — `tests/test_schedule_commands.py` (38 tests)

ครอบคลุม parse duration/เวลา, รูปแบบคำสั่งทุกแบบ, argv (metacharacter เป็น
argv element เดียว), ownership guard (list/cancel ข้าม session ไม่ได้),
error path ของ CLI, formatter และ handler ระดับ orchestrator
พร้อมปรับ `tests/test_openclaw_provider.py` 2 เคสที่ assert message ตรงๆ
ให้คาดหวัง SESSION_CONTEXT block

## ผลทดสอบ end-to-end (2026-08-13)

สร้าง job ผ่าน code path จริง (`build_cron_add_argv`) pin เข้า thread จริง
`--at +120s` → OpenClaw inject message เข้า session เดิมตอนครบกำหนด →
watcher ที่ start ตั้งแต่ boot ส่งกลับ Google Chat สำเร็จใน 5 วินาทีหลังยิง
(`chat-out` ตอบ `thread_reply=true`) และ job ลบตัวเองอัตโนมัติ
test suite รวม 397 tests ผ่านทั้งหมด

## ข้อจำกัด / ระวัง

- **ต้องรันบนเครื่องเดียวกับ OpenClaw gateway** (ปัจจุบัน t495) — cron store
  เป็น local sqlite ถ้าย้ายเครื่องต้อง migrate state เอง
- **job รอดจากการ restart บอท** เพราะ scheduler อยู่ฝั่ง gateway — แต่ถ้า
  gateway ดับตอนถึงเวลา job จะพลาดรอบนั้น (ดู policy เรื่อง skipped run ใน
  [OpenClaw cron docs](https://docs.openclaw.ai/cli/cron))
- **session key ผูกกับ thread ไม่ใช่ผู้ใช้** — reminder กลับมาที่ thread
  เดิมเสมอ ถ้าต้องการระบุตัวผู้สั่งให้ฝัง display name ใน message
- `cron list` default limit 50 job — ถ้า job สะสมเกินนี้ (เช่นมี recurring
  จากหลาย thread) list/cancel อาจมองไม่เห็น job เก่า ต้องเพิ่ม paging

## แนวทางต่อยอด (ยังไม่ทำ)

- คำสั่ง recurring ผ่าน `/schedule every ...` (ตอนนี้ recurring ทำได้ผ่าน
  ภาษาคนให้ agent สร้างเอง)
- เตือนใน thread เมื่อ job รันล้มเหลว (ดู failure delivery ใน cron docs)
- ยกเลิกด้วยภาษาคน ("เลิกเตือนตอนเช้า") โดย map กลับหา job จาก
  `--declaration-key`
