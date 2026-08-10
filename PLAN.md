# แผนพัฒนา `/chat` และ `/chat clear <เวลา>` สำหรับ Google Chat

สถานะ: แผนพร้อมนำไป implement บน branch `google-chat-history`  
ขอบเขต: Google Chat แบบ 1:1 ระหว่างผู้ใช้หนึ่งคนกับ Jinx เท่านั้น

## 1. เป้าหมายและขอบเขตที่ตกลงแล้ว

เพิ่มคำสั่งจัดการประวัติข้อความจริงใน Google Chat ดังนี้

- `/chat` แสดงจำนวนข้อความและช่วงเวลาของประวัติที่ยังมองเห็นได้
- `/chat clear <เวลา>` เตรียมลบข้อความที่เก่ากว่าเวลาที่ระบุ
- ต้องถามยืนยันในแชทด้วยการ์ดหัวข้อ `🛠️ ผู้ดูแลระบบ` ก่อนลบทุกครั้ง
- การ์ดมีปุ่ม `ยืนยันการลบ` และ `ยกเลิก`
- หลังยืนยัน งานลบทำเบื้องหลังและรายงานผลสำเร็จ/ข้าม/ล้มเหลวเมื่อจบ
- คำสั่ง `/chat...` และ callback จากปุ่มต้องไม่ถูกส่งต่อไป OpenClaw

ฟีเจอร์นี้ไม่ใช้ Space Manager และไม่รองรับ group chat หรือ named space ถึงแม้ Google Chat API จะเรียก DM ว่า resource ชนิด `spaces/{id}` ก็ตาม ก่อนทำงานทุกครั้งต้องตรวจจาก API ว่า resource นั้นมี `spaceType == DIRECT_MESSAGE` และ `singleUserBotDm == true`

สิ่งที่อยู่นอกขอบเขต:

- ไม่ลบ `chat-in.jsonl`, `chat-out.jsonl`, OpenClaw session/trajectory หรือไฟล์ในเครื่อง
- ไม่ลบไฟล์ Drive ที่เคยแนบหรืออ้างถึงในข้อความ
- ไม่แก้ Google Vault retention rule หรือ legal hold
- ไม่รับประกันการ purge ถาวรจาก Vault; คำว่า “ลบ” ในแผนนี้หมายถึงลบ Google Chat Message resource ออกจาก Chat
- ไม่รองรับคำสั่งภาษาไทยหรือหน่วยเวลาไทย

## 2. สัญญาของคำสั่ง

### 2.1 `/chat`

รับเฉพาะคำสั่ง `/chat` แบบตรงตัว หลังตัด whitespace รอบนอกแล้ว `/chatty` หรือข้อความที่ขึ้นต้นคล้ายกันต้องยังเป็นข้อความปกติ

ผลลัพธ์ประกอบด้วย:

- จำนวนข้อความทั้งหมด
- จำนวนข้อความจากคน, จากบอต และ sender ที่จำแนกไม่ได้
- เวลาของข้อความแรกและข้อความล่าสุด
- ระยะเวลาระหว่างข้อความแรกกับข้อความล่าสุด
- เวลาแสดงผลใน `Asia/Bangkok`

นับจาก `spaces.messages.list` ทุกหน้าโดยใช้ `showDeleted=false` เนื่องจาก API ไม่มี `totalSize` ข้อความ system ที่ API ไม่ส่งคืนและ tombstone ที่ถูกลบแล้วจะไม่ถูกนับ คำสั่ง `/chat` ที่กำลังเรียกสถิติจะถูกตัดออกด้วย `message.name` เพื่อให้ผลหมายถึงประวัติก่อนเรียกคำสั่ง

ถ้าไม่มีข้อความ ให้แสดงจำนวน `0` โดยไม่แสดงช่วงเวลา

### 2.2 `/chat clear <เวลา>`

กฎระดับ command:

- อนุญาต whitespace ระหว่าง `/chat`, `clear` และ argument
- `<เวลา>` ต้องเป็น token เดียวและห้ามมี whitespace ภายใน
- ห้ามมี argument เพิ่มเติม, CR/LF, ตัวพิมพ์ใหญ่, เครื่องหมาย, decimal หรือข้อความไทย
- command ที่ขึ้นต้น `/chat` แต่รูปแบบผิดต้องตอบ usage error และห้ามส่งเข้า OpenClaw
- attachment ที่ส่งมากับคำสั่งนี้จะไม่ถูกดาวน์โหลดหรือส่งเข้า OpenClaw และต้องแจ้งผู้ใช้ว่า attachment ถูกข้าม

รูปแบบ relative duration ที่รองรับ:

- `30m`, `12h`, `7d`, `2w`, `3mo`, `1y`
- duration ผสม เช่น `1w2d`, `2h30m`, `1y2mo3d`
- หน่วยต้องเรียงจากใหญ่ไปเล็กตาม `y`, `mo`, `w`, `d`, `h`, `m`
- แต่ละหน่วยใช้ได้ครั้งเดียวและค่าต้องเป็นจำนวนเต็มบวก
- `m` หมายถึง minute และ `mo` หมายถึง calendar month

รูปแบบ absolute time ที่รองรับ:

- วันที่: `2026-08-01`
- วันที่และเวลา: `2026-08-01T18:30`
- วินาทีและเศษวินาที: `2026-08-01T18:30:45.123456`
- RFC 3339 พร้อม offset: `2026-08-01T18:30:00+07:00`
- UTC: `2026-08-01T11:30:00Z`

ตัวอย่างที่ต้องปฏิเสธ:

- `/chat clear`
- `/chat clear 1 w`
- `/chat clear 7 วัน`
- `/chat clear 2026-08-01 18:30`
- `/chat clear 1w extra`
- `/chat clear 1W`
- `/chat clear 0m`
- `/chat clear -1d`

### 2.3 ความหมายของ cutoff

- จับเวลาอ้างอิงเพียงครั้งเดียวจาก `chat.eventTime`; ถ้าไม่มีจึงใช้ UTC clock ของ server
- เก็บเวลาอ้างอิงและ cutoff ลงฐานข้อมูลเพื่อให้ webhook retry ได้ผลเดิม
- วันที่ที่ไม่มีเวลาแปลเป็น `00:00:00 Asia/Bangkok`
- datetime ที่ไม่มี offset ใช้ `Asia/Bangkok`
- datetime ที่มี `Z` หรือ offset ใช้ offset ที่ให้มา แล้ว normalize เป็น UTC
- `y` และ `mo` ลบแบบปฏิทินและ clamp วันที่ไปวันสุดท้ายของเดือนเป้าหมาย
- `w`, `d`, `h`, `m` ลบด้วย fixed duration หลังคำนวณปี/เดือนแล้ว
- ปฏิเสธวันเวลาที่ไม่ถูกต้อง, overflow และ cutoff ในอนาคต
- จำกัดความยาว argument และจำนวน component เพื่อไม่ให้ parser ใช้ทรัพยากรเกินจำเป็น

ขอบเขตการลบกำหนดเป็น `createTime < cutoff` หรือ “เก่ากว่าเวลาที่ระบุ” แบบ strict ข้อความที่มีเวลาเท่ากับ cutoff พอดีจะไม่ถูกลบ กฎนี้ตรงกับ filter ที่ `spaces.messages.list` รองรับโดยตรง

## 3. Authentication และ authorization

ใช้ credential สองชนิดตามเจ้าของข้อความ:

- `spaces.messages.list` ใช้ user OAuth scope `https://www.googleapis.com/auth/chat.messages`
- ข้อความ `sender.type == HUMAN` และ `sender.name` ตรงกับผู้ใช้ใน DM ลบด้วย user OAuth
- ข้อความ `sender.type == BOT` ลบด้วย service account scope `https://www.googleapis.com/auth/chat.bot`
- sender ที่ไม่รู้จักหรือ HUMAN ที่ไม่ตรง requester ห้ามลองสลับ credential ให้ข้ามแบบ fail closed
- ไม่ส่ง `force=true`; bot DM เป็น flat conversation และไม่ควรเสี่ยงลบ reply ที่อยู่นอก cutoff

เหตุผลที่ต้องแยก credential คือ bot auth ลบได้เฉพาะข้อความที่ calling app สร้าง ส่วน user auth ใช้ลบข้อความของผู้ใช้ที่ authorize token ใน DM นี้ ดังนั้นไม่ต้องมี Space Manager สำหรับกรณี 1:1 นี้

เพิ่มการอนุญาตระดับแอป:

- `GCHAT_HISTORY_ALLOWED_USER=users/...` ต้องตรงกับ `chat.user.name`
- `GCHAT_HISTORY_ALLOWED_SPACE=spaces/...` หรือใช้ค่า trusted เดียวกับ `GCHAT_OUTBOUND_SPACE`
- ห้าม auto-enroll ผู้ใช้หรือ DM ใหม่จาก webhook แรกที่เข้ามา
- ตรวจทั้ง allowlist และ canonical Space resource ก่อน list หรือ delete

JWT ของ webhook ยืนยันเพียงว่า request มาจาก Google แต่ไม่ใช้แทน authorization ของคนที่กดปุ่ม ต้อง bind งานกับ stable `user.name`; ห้ามใช้ `displayName`

งานติดตั้ง OAuth:

- รวมรายการ scope ไว้ใน module กลางเพื่อไม่ให้ `app.py` และ token helper ต่างกัน
- เพิ่ม `chat.messages` ใน OAuth consent configuration
- สร้าง `token.json` ใหม่ด้วยบัญชีผู้ใช้คนเดียวกับที่คุยใน DM; การเพิ่ม scope ใน source code ไม่ทำให้ refresh token เก่าได้สิทธิ์เพิ่ม
- ตรวจ granted scopes ตอน startup/ก่อน preview และแสดงข้อความ reauthorize ที่ชัดเจนเมื่อขาด scope
- ระบุใน README ว่า `chat.messages` เป็น restricted scope และอาจต้องตั้งค่า Workspace admin policy/OAuth verification ตามรูปแบบ deployment
- เอา authorization code หรือ secret ที่ hard-code ออกจาก token helper และรับค่าผ่าน input ที่ไม่ถูก commit

ไม่ต้องเพิ่ม `chat.app.messages.readonly`: แผนนี้ list ด้วย user auth และใช้ `chat.bot` เฉพาะการลบข้อความของบอต

## 4. การไหลของข้อมูล

### 4.1 `/chat`

1. Flask route ตรวจ JWT และ parse JSON
2. normalize Google Workspace Add-on event
3. ตรวจ stable user, allowlist และ single-user bot DM
4. route คำสั่งออกจาก MessageOrchestrator และตอบ webhook ทันที
5. background task ไล่ `spaces.messages.list` ทุกหน้า
6. คำนวณ count/range โดยไม่เก็บหรือ log เนื้อความ
7. ส่งการ์ดสถิติกลับเข้า DM

### 4.2 `/chat clear 1w`

1. ตรวจ event และ parse cutoff จาก event time
2. สร้าง durable operation โดย deduplicate ด้วย source `message.name`
3. ACK webhook ทันที
4. worker list ข้อความด้วย filter `createTime < "<cutoff UTC>"`
5. paginate ให้จบและสร้าง immutable snapshot ของ candidate ก่อนลบหรือส่งการ์ดยืนยัน
6. แยก HUMAN/BOT/unknown และบันทึกเฉพาะ metadata ที่จำเป็น
7. ถ้าไม่มี candidate ที่ลบได้ ให้แจ้งผลและไม่สร้างปุ่ม
8. ส่ง confirmation card และ bind resource name ของการ์ดเข้ากับ operation
9. ณ จุดนี้ยังไม่มีการเรียก delete API

### 4.3 เมื่อกด “ยืนยันการลบ”

1. ตรวจ callback JWT และ normalize `chat.buttonClickedPayload`
2. อ่าน opaque action handle จาก `commonEventObject.parameters`
3. ตรวจ token, requester `user.name`, space, confirmation `message.name`, TTL และ state จาก server-side store
4. claim งานด้วย atomic transaction ให้เปลี่ยน state ได้ครั้งเดียว
5. ตอบ `hostAppDataAction.chatDataAction.updateMessageAction` ภายใน 30 วินาที เพื่อลบปุ่มและแสดงสถานะกำลังลบ
6. กำหนดเวลาลบแรกให้เว้นจาก message update ตาม write quota แล้วปลุก durable worker
7. worker ลบเฉพาะ message names ใน snapshot ทีละรายการ
8. persist ผลหลังแต่ละรายการและส่ง final summary เมื่อจบ

### 4.4 เมื่อกด “ยกเลิก”

1. ตรวจ identity/binding/TTL แบบเดียวกับ confirm
2. atomic transition จาก pending ไป `CANCELLED`
3. update card เพื่อนำปุ่มออกและแจ้งว่ายกเลิกแล้ว
4. ห้าม enqueue หรือลบข้อความใด

## 5. รูปแบบ event และ confirmation card

โปรเจกต์นี้ใช้ Google Workspace Add-on payload เป็นหลัก ไม่ใช่ legacy Chat app event:

- message: `chat.messagePayload`
- button callback: `chat.buttonClickedPayload`
- actor: `chat.user`
- action parameters: top-level `commonEventObject.parameters`
- synchronous card update: `hostAppDataAction.chatDataAction.updateMessageAction`

คง fallback ของ legacy message payload เดิมเพื่อไม่ทำให้ behavior ที่มีอยู่เสีย แต่ callback ใหม่ต้องออกแบบและทดสอบกับ Add-on payload เป็นหลัก และต้อง route callback ก่อน logic ที่ตีความ text ว่างเป็นข้อความปกติ

confirmation card ต้องแสดง:

- หัวข้อ `🛠️ ผู้ดูแลระบบ`
- cutoff ใน `Asia/Bangkok` และข้อความชัดเจนว่า “ลบข้อความที่สร้างก่อนเวลานี้”
- จำนวน candidate แยก HUMAN/BOT และจำนวนที่จะข้าม
- เวลาประมาณการขั้นต่ำจากอัตราไม่เกินหนึ่ง write ต่อวินาที
- อายุคำขอยืนยัน ค่าเริ่มต้น 10 นาที
- ปุ่ม `ยืนยันการลบ` และ `ยกเลิก`

`onClick.action.function` ของ Add-on card ต้องเป็น HTTPS URL เต็ม กำหนดด้วย `GCHAT_CARD_ACTION_URL` และห้าม derive จาก HTTP `Host` header ถ้าไม่ได้ตั้งค่าหรือ URL ไม่ผ่าน validation ให้ fail closed และไม่ส่งการ์ดที่กดไม่ได้

การป้องกัน forged/replayed action:

- สุ่ม opaque handle 256-bit แยกกันสำหรับ confirm และ cancel
- ใส่เฉพาะ handle ใน card parameter; ไม่ใส่ cutoff, count, message names, credential kind หรือ authority decision
- เก็บเฉพาะ SHA-256 hash ของแต่ละ handle ใน SQLite
- bind operation กับ requester resource name, space name, source command message name, confirmation card message name, cutoff, expiry และ exact item snapshot
- confirmation card ต้องถูกสร้างสำเร็จและ bind `message.name` ก่อนจึงยอมรับ click; card ที่ orphan/unbound ต้อง fail closed
- click ซ้ำ, Google retry และ confirm/cancel ที่แข่งกันต้องเปลี่ยน state สำเร็จได้เพียงครั้งเดียว
- request clear ใหม่ยกเลิก pending request เก่าของ DM เดียวกัน; ถ้ามีงานลบกำลัง RUNNING ให้ปฏิเสธงานใหม่

## 6. Google Chat API client

### 6.1 List และ stats

เรียก `spaces.messages.list` ด้วย:

- `parent=spaces/{id}` ของ DM ปัจจุบัน
- `pageSize=1000`
- `showDeleted=false`
- `pageToken` จน `nextPageToken` ว่าง
- `filter=createTime < "<RFC3339 UTC>"` สำหรับ clear preview
- partial fields เท่าที่จำเป็น: `messages(name,createTime,sender(name,type)),nextPageToken`

ทุก parameter ยกเว้น `pageToken` ต้องคงเดิมทุกหน้า รองรับ response ว่าง `{}` และห้ามสมมติว่ามี `messages: []`

ก่อนเริ่ม list ให้เรียก `spaces.get` และตรวจ `spaceType == DIRECT_MESSAGE` กับ `singleUserBotDm == true` จาก canonical resource ไม่อาศัย display name หรือ thread name

### 6.2 Immutable snapshot

- list ให้ครบทุกหน้าก่อนส่งการ์ดยืนยัน
- snapshot เก็บ `message.name`, `createTime`, normalized sender type/name และ credential partition เท่านั้น
- ไม่เก็บ message text, formatted text, cards หรือ attachment metadata
- ข้อความที่เข้ามาใหม่หลัง preview แม้จะมี timestamp ย้อนหลัง ต้องไม่ถูกลบโดย operation เดิม
- ถ้ากระบวนการหยุดระหว่าง PREPARING ให้ทิ้ง incomplete snapshot แล้ว list ใหม่ก่อนส่งการ์ด
- หลังเป็น `PENDING_CONFIRMATION` แล้ว snapshot ต้อง immutable

### 6.3 Delete, quota และ retry

เรียก `spaces.messages.delete(name=..., force=False)` ทีละรายการ เนื่องจาก API ไม่มี bulk delete หรือ time-range delete

ข้อกำหนดการรัน:

- มี per-space write pacer กลางที่ gateway และ history worker ใช้ร่วมกัน
- เว้นอย่างน้อย 1.1 วินาทีระหว่าง write เพื่อเผื่อ quota 1 write/second/space
- card create/update/final notification และ delete อยู่ใน quota เดียวกัน จึงต้องนำมาคิดร่วมกันเมื่ออยู่ใน flow นี้
- งานลบเรียง oldest-first เพื่อให้ผลและการ resume คาดเดาได้
- ไม่ถือ global `ProcessingGate` ตลอดงานลบ; ใช้ history-job lock แยก
- ใช้ singleton worker lock ข้าม WSGI process และ persist `next_write_at`/item state เพื่อป้องกัน worker ซ้ำ

นโยบาย error:

- `404`: ถือเป็น `ALREADY_ABSENT` และสำเร็จแบบ idempotent
- `429`: เคารพ `Retry-After` แล้วใช้ truncated exponential backoff พร้อม jitter
- `5xx`, timeout, connection reset: retry แบบมีจำนวนครั้งและเวลาสูงสุด
- `401`: refresh/rebuild credential ได้หนึ่งครั้ง แล้วจึงเป็น permanent failure
- `400`, `403` และ 4xx อื่นที่ไม่ transient: ไม่ retry แบบวนซ้ำ บันทึก safe category แล้วทำตามนโยบาย partition
- ถ้า credential partition ใดมี systemic auth failure ให้หยุดยิง request ที่จะล้มเหลวซ้ำสำหรับ partition นั้น แต่ยังทำอีก partition ที่ปลอดภัยได้
- ห้าม fallback จาก user credential ไป bot credential หรือกลับกันหลัง permission error

final summary ต้องแยกอย่างน้อย:

- deleted
- already absent
- skipped/unknown sender
- failed
- แยก HUMAN และ BOT เมื่อมีประโยชน์

ถ้าส่ง final notification ไม่สำเร็จ ให้ retry เฉพาะ notification โดยห้ามรัน delete items ที่ terminal แล้วซ้ำ

## 7. Durable state และ state machine

เพิ่ม SQLite ledger ใน directory ส่วนตัว:

- environment: `JINX_CHAT_HISTORY_STATE_DIR`
- default: `~/.openclaw/state/jinx-gchat/chat-history`
- directory mode `0700`
- database mode `0600`
- ใช้ WAL, foreign keys, `busy_timeout` และ transaction ที่เหมาะกับ crash recovery

ตาราง `clear_jobs` เก็บอย่างน้อย:

- operation ID และ unique source command `message.name`
- requester `users/{id}` และ `spaces/{id}`
- cutoff UTC, display timezone และ original normalized argument
- source event time, created/updated/expires timestamps
- confirmation message name/client-assigned ID
- confirm/cancel token hashes
- status และ snapshot-complete flag
- candidate/deleted/already-absent/skipped/failed counts
- notification state และ safe error category

ตาราง `clear_items` เก็บอย่างน้อย:

- operation ID และ Google Chat message resource name
- create time, sender name/type และ credential partition
- item status, attempts, retry time และ safe error category
- unique key `(operation_id, message_name)`

ห้ามเก็บเนื้อความ, OAuth token, raw API error body หรือ raw card handle

job state machine:

```text
PREVIEW_QUEUED -> PREPARING -> PENDING_CONFIRMATION
PENDING_CONFIRMATION -> DELETE_QUEUED -> RUNNING -> COMPLETED | PARTIAL_FAILED | FAILED
PENDING_CONFIRMATION -> CANCELLED
PENDING_CONFIRMATION -> EXPIRED
```

item state machine:

```text
PENDING -> RUNNING -> DELETED | ALREADY_ABSENT | SKIPPED | FAILED
```

confirm/cancel ใช้ `BEGIN IMMEDIATE` และ compare-and-swap โดยตรวจ token hash, user, space, card message, expiry และ current state ใน transaction เดียว มีเพียง transaction เดียวที่ claim ได้

worker startup ต้อง:

- expire pending jobs ที่หมดเวลา
- resume `PREVIEW_QUEUED`, incomplete `PREPARING`, `DELETE_QUEUED` และ `RUNNING`
- ไม่แตะ terminal items
- รองรับกรณี process crash หลัง remote delete สำเร็จแต่ก่อน commit โดย retry แล้วรับ `404` เป็น already absent

ใช้ client-assigned message ID หรือ idempotency identifier ที่เสถียรกับ confirmation/final card เพื่อ recover จากกรณีส่งสำเร็จแต่ process หยุดก่อนบันทึก response และยังคง bind actual `message.name` จาก REST response ก่อนยอมรับ action

## 8. การแก้ไขไฟล์

### ไฟล์ใหม่

`helpers/google_scopes.py`

- เป็น source of truth เดียวของ Drive user scopes, `chat.messages` และ `chat.bot`
- ให้ `app.py` และ token tools import จากที่เดียวกัน

`helpers/chat_events.py`

- normalize `messagePayload` และ `buttonClickedPayload` เป็น typed event
- ดึง stable user/space/message names, event time, text, attachments และ safe action parameters
- validate required fields และ fail closed เมื่อ event ไม่สมบูรณ์

`helpers/chat_history_time.py`

- command recognizer และ strict one-token parser
- duration/date/datetime parsing
- calendar subtraction, timezone normalization และ cutoff formatting
- เป็น pure logic พร้อม injected clock เพื่อ unit test

`helpers/chat_history_client.py`

- สร้าง user/bot Chat clients ด้วย timeout
- get/validate DM, paginate list, stats/snapshot และ delete routing
- per-space pacing, Retry-After/backoff และ safe error classification

`helpers/chat_clear_store.py`

- SQLite schema/migration
- immutable job/items
- token hashing, atomic confirmation/cancel และ restart recovery queries
- cross-process singleton worker lock และ write pacing state

`helpers/chat_history_service.py`

- route stats/clear/click actions
- enqueue preview, build/bind confirmation, start/resume deletion และส่ง final summary
- ทำ background work โดยไม่ถือ ProcessingGate

### ไฟล์ที่แก้

`app.py`

- ใช้ scope module กลาง
- initialize history store/client/service/worker และ shutdown hook
- ย้าย incoming raw-body logging ให้อยู่หลัง auth และผ่าน redaction
- normalize event และ route button callback ก่อน normal message path
- route `/chat...` ก่อน MessageOrchestrator/attachment watcher
- return Add-on `updateMessageAction` สำหรับ button callback

`helpers/message_orchestrator.py`

- เพิ่ม defense-in-depth reservation สำหรับ command boundary `/chat`
- malformed history command ต้องไม่ถึง provider แม้ route ชั้นบนพลาด

`helpers/services/chat_services.py`

- เพิ่ม presenter สำหรับ stats, usage, confirmation, running, cancelled, expired และ final summary cards
- สร้าง Add-on button actions และ update-message envelope
- escape/sanitize ค่าจาก API ก่อน render

`helpers/chat_gateway.py`

- ส่ง structured `cardsV2` โดยตรง
- ให้ `_post_message()` คืน response JSON และ `message.name` โดยรักษา behavior เดิม
- รองรับ stable client-assigned message ID/idempotent recovery
- ใช้ per-space write pacer กับ REST writes ใน history flow
- redact action handles จาก incoming/outgoing diagnostic logs

`helpers/token_tools/get_token.py`  
`helpers/token_tools/get_token_manual.py`  
`helpers/token_tools/manual_token.py`

- ใช้ scope module กลาง
- ไม่ hard-code authorization code/secret
- แจ้งชัดว่าต้องสร้าง token ใหม่เมื่อ scope เปลี่ยน

`README.md`

- อธิบาย command grammar และตัวอย่าง
- อธิบาย DM-only, cutoff แบบ strict, confirmation TTL, quota และ partial result
- เพิ่ม environment variables และขั้น reauthorize
- ระบุขอบเขต Google Chat เทียบกับ local logs/OpenClaw/Drive/Vault
- เพิ่ม troubleshooting สำหรับ missing scope, invalid card action URL, revoked token และ permission failure

ไม่ต้องเพิ่ม dependency สำหรับเวลา: ใช้ Python stdlib `datetime`, `zoneinfo` และ `calendar`; `google-api-python-client` มีอยู่แล้ว

## 9. Logging และข้อมูลอ่อนไหว

ปัจจุบัน route บันทึก raw incoming body ก่อน auth ซึ่งจะทำให้ action handle อยู่ใน `chat-in.jsonl` ต้องแก้ก่อนเปิดใช้ปุ่ม:

- authenticate ก่อนบันทึก payload ที่ใช้งานได้
- parse แล้ว redact `commonEventObject.parameters` และ field ที่อาจเป็น token
- rejected request บันทึกได้เฉพาะ metadata ที่ไม่ใช่ raw body
- ห้าม log/persist fetched message text, OAuth token, raw authorization code หรือ raw Google API error body
- error ที่แสดงผู้ใช้และ ledger เก็บเฉพาะ canonical status/category กับข้อความที่ sanitize แล้ว
- ทดสอบยืนยันว่า opaque handles ไม่ปรากฏใน log

## 10. แผนการทดสอบ

### 10.1 Parser และเวลา

เพิ่ม `tests/test_chat_history_time.py`:

- accept ทุกตัวอย่าง duration, compound, date, datetime, offset และ `Z`
- command boundary: `/chat` เทียบกับ `/chatty`
- reject missing/extra arguments, whitespace ภายใน, Thai units, uppercase, zero, negative, decimal, duplicate/reversed units และ garbage suffix
- month-end clamp, leap year และลำดับ calendar/fixed subtraction
- date-only/no-offset ใน Bangkok และ explicit offset equivalence
- invalid date, nonexistent/overflow/future cutoff
- injected event time ให้ webhook retry ได้ cutoff เดิม

### 10.2 Event และ route

เพิ่ม `tests/test_chat_route_events.py`:

- verify auth ก่อน handle MESSAGE และ button click
- parse `chat.messagePayload`, `chat.buttonClickedPayload`, `chat.user` และ `commonEventObject.parameters`
- stable `user.name`, `space.name`, canonical DM fields และ `message.name`
- malformed JSON/event fail closed
- `/chat`, valid/invalid `/chat clear...` และ callbacks ไม่เข้า OpenClaw
- `/chatty` ยังเข้า normal provider path
- non-DM, wrong user/space และ unknown action ถูกปฏิเสธ
- attachment บน history command ถูกข้าม
- opaque token ถูก redact จาก raw logs

### 10.3 API client

เพิ่ม `tests/test_chat_history_client.py`:

- empty response `{}` และ pagination มากกว่า 1,000 ข้อความ
- parameters คงเดิมทุกหน้าและ follow `nextPageToken`
- stats count/range/sender split และ exclude triggering command
- exact `createTime < cutoff`, partial fields และ `showDeleted=false`
- snapshot เสร็จก่อน delete request แรก
- HUMAN ใช้ user OAuth, BOT ใช้ bot auth, unknown/mismatch skipped และไม่มี credential fallback
- `force=False`
- shared fake-clock pacer เว้นอย่างน้อย quota ที่กำหนดข้าม user/bot writes
- `429` เคารพ Retry-After, `5xx`/timeout backoff, bounded attempts
- `401` refresh ครั้งเดียว, `400/403` ไม่ retry, `404` idempotent success

### 10.4 Store, confirmation และ concurrency

เพิ่ม `tests/test_chat_clear_store.py` และ `tests/test_chat_clear_confirmation.py`:

- schema/migration/file permissions และไม่เก็บ message text/raw token
- source webhook deduplication และ one-active-job rule
- frozen snapshot และ token ที่เก็บเป็น hash
- wrong user ที่ displayName เหมือนกัน, wrong space/card message, forged token และ non-DM fail closed
- expiry, cancel once, confirm-after-cancel, cancel-after-confirm และ superseded pending request
- สอง thread/connection กด confirm พร้อมกัน claim ได้หนึ่งครั้ง
- confirmation card แสดง cutoff/count/TTL/estimated time และมีปุ่มถูกต้อง
- card parameters มีเฉพาะ opaque handle
- callback หลักเป็น `buttonClickedPayload` และตอบ `updateMessageAction`
- duplicate delivery/double click start worker ครั้งเดียว
- cancel/expired/unauthorized ไม่เรียก delete

### 10.5 Worker และ recovery

เพิ่ม `tests/test_chat_history_worker.py`:

- ลบเฉพาะ immutable preview snapshot; ข้อความใหม่/backdated หลัง previewไม่ถูก sweep
- mixed HUMAN/BOT และลำดับ oldest-first
- success, already absent, skipped และ partial failures สรุปถูกต้อง
- systemic auth failure หยุดเฉพาะ credential partition ที่เสีย
- restart resume queued/running โดยไม่ทำ terminal item ซ้ำ
- crash หลัง API success ก่อน commit แล้ว retry/404 ได้อย่างปลอดภัย
- singleton worker ป้องกัน worker ซ้ำ
- final notification retry ไม่ทำ delete ซ้ำ
- missing/revoked scope, list/page error, malformed API record และ DB unavailable/corrupt ล้มแบบ fail closed

### 10.6 Regression

ขยาย tests เดิม:

- `tests/test_chat_gateway_file_delivery.py` สำหรับ structured card, returned message name และ idempotent card delivery
- `tests/test_attachment_notifications.py` สำหรับ route behavior และ callback ที่ไม่เริ่ม watcher/provider
- command/orchestrator tests สำหรับ reserved `/chat` boundary และ ProcessingGate ที่ไม่ถูกถือระหว่างลบ

คำสั่งตรวจสอบขั้นต่ำ:

```bash
python -m unittest discover -s tests -v
ruff check .
git diff --check
```

## 11. ลำดับ implementation

### Phase 1 — Foundation

- [ ] รวม scopes และแก้ token helpers
- [ ] เพิ่ม event normalization และ strict parser
- [ ] เพิ่ม DM/allowlist validation
- [ ] reserve `/chat` ไม่ให้ถึง OpenClaw
- [ ] เพิ่ม unit tests ของ parser/event/route

### Phase 2 — Read-only stats

- [ ] เพิ่ม user-auth list client และ pagination
- [ ] เพิ่ม `/chat` stats card และ zero state
- [ ] ทดสอบ empty/>1000/error/timezone
- [ ] deploy ทดสอบ read-only ก่อนเปิด clear

### Phase 3 — Durable preview และ confirmation

- [ ] เพิ่ม SQLite schema/state machine
- [ ] สร้าง exact snapshot และ confirmation card
- [ ] เพิ่ม full-URL card action configuration
- [ ] เพิ่ม hashed one-time confirm/cancel handles และ click binding
- [ ] แก้ raw-log redaction ก่อนทดสอบปุ่มจริง

### Phase 4 — Delete worker

- [ ] เพิ่ม sender-based credential routing
- [ ] เพิ่ม cross-process-safe pacing/retry/recovery
- [ ] เพิ่ม partial/final reporting
- [ ] ทดสอบ crash, duplicate click และ final notification failure

### Phase 5 — Rollout และ documentation

- [ ] reauthorize `token.json` ด้วย `chat.messages`
- [ ] ตั้ง allowed user/space, action URL และ private state directory
- [ ] รัน automated suite และ manual acceptance
- [ ] อัปเดต README/troubleshooting
- [ ] เปิด feature หลังผ่าน cancel/expiry/restart tests

## 12. Manual acceptance checklist

ใช้ test 1:1 DM ที่มีทั้งข้อความคนและบอตซึ่งเก่า/ใหม่กว่าค่า cutoff:

- [ ] `/chat` แสดง count และช่วงเวลาถูกต้อง
- [ ] malformed command แสดง usage และไม่เรียก OpenClaw
- [ ] `/chat clear 1w` แสดง preview โดยยังไม่ลบ
- [ ] กด `ยกเลิก` แล้วไม่มี delete request
- [ ] ปล่อย TTL หมดแล้วกดยืนยันไม่ได้และไม่มี delete request
- [ ] กด confirm แล้วลบเฉพาะ exact old snapshot
- [ ] ข้อความที่เวลาเท่ากับ cutoff และใหม่กว่า cutoff ไม่ถูกลบ
- [ ] double click/webhook retry เริ่มงานครั้งเดียว
- [ ] restart ระหว่างงานแล้ว resume โดยไม่ลบซ้ำแบบอันตราย
- [ ] revoke user token ระหว่างงานแล้วได้ partial report โดยไม่ fallback credential
- [ ] web และ mobile Google Chat แสดง/กด card ได้
- [ ] confirmation token ไม่ปรากฏใน logs หรือ SQLite แบบ plaintext
- [ ] group/named space และผู้ใช้อื่นใช้งานไม่ได้

## 13. Definition of Done

งานถือว่าเสร็จเมื่อ:

- `/chat` และทุก `/chat...` command ถูก route อย่าง deterministic และไม่ถึง OpenClaw
- stats paginate ครบและแสดงเวลา Bangkok ถูกต้อง
- clear ใช้ได้เฉพาะ allowed single-user bot DM
- ไม่มีข้อความถูกลบก่อน atomic, unexpired, requester-bound confirmation
- งานลบแตะเฉพาะ immutable snapshot และใช้ credential ตาม sender เท่านั้น
- retry/quota/restart/double-click ปลอดภัยและมีผลสรุปตรวจสอบได้
- ไม่มี message content, OAuth token หรือ confirmation handle รั่วใน ledger/logs
- automated tests, lint และ manual acceptance ผ่าน
- README ระบุข้อจำกัดเรื่อง scope, quota, local data และ Vault ครบ

## 14. เอกสารอ้างอิงที่ตรวจแล้ว

- [List messages](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/list)
- [Get a message](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/get)
- [Delete a message](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/delete)
- [Authenticate and authorize Chat apps](https://developers.google.com/workspace/chat/authenticate-authorize)
- [Google Chat API limits](https://developers.google.com/workspace/chat/limits)
- [Space resource and history state](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces)
- [Google Workspace Add-on event objects](https://developers.google.com/workspace/add-ons/concepts/event-objects)
- [Send and update Chat messages from an Add-on](https://developers.google.com/workspace/add-ons/chat/send-messages)
- [Google Vault retention for Chat](https://support.google.com/vault/answer/2990828)

เอกสารอ้างอิงชุดนี้ตรวจล่าสุดวันที่ 2026-08-10 ก่อนเขียนแผนนี้
