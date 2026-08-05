# kimi-chat-poc

สคริปต์ Python (stdlib ล้วน ไม่ต้อง install อะไร) สำหรับคุยกับ Kimi bot chat ผ่าน API ชุดเดียวกับที่เว็บแอป kimi.com ใช้ — ส่งข้อความ (พร้อมไฟล์แนบได้) แล้วรอ stream คำตอบกลับมาที่ terminal, และจัดการห้องแชท (list / create / delete)

ข้อความที่ส่งจะวิ่ง pipeline เดียวกับการพิมพ์ในแอปทุกประการ: cloud ของ Kimi รับข้อความ → push ลง `[kimi-bridge]` ที่เครื่อง → local agent ประมวลผล → คำตอบย้อนกลับเข้าแชท (ดูในแอปได้) และสคริปต์ stream กลับมาให้ที่ stdout ด้วย

## ไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `send-message.py` | ส่งข้อความเข้าแชท (แนบไฟล์ได้) แล้ว stream คำตอบของ agent กลับมา |
| `listen-messages.py` | คอยฟังข้อความใหม่ในแชทแล้วพิมพ์ออกมาเรื่อย ๆ (polling ทุก 2 วิ) |
| `kimi-rooms.py` | จัดการห้องแชท: `list` / `create` / `delete` |

## สิ่งที่ต้องมี (Credentials)

สคริปต์ใช้ credential 2 ตัว คนละหน้าที่:

| Token | ใช้ทำอะไร | เอามาจากไหน |
|---|---|---|
| `KIMI_TOKEN` (user JWT) | ส่งข้อความ / อ่านข้อความ / จัดการห้อง ในนามตัวเรา | capture จากเบราว์เซอร์ (devtools → Network → header `authorization: Bearer ...` หรือคุกกี้ `kimi-auth`) — **หมดอายุ ~30 วัน** |
| bot token (`km_b_prod_...`) | อัปโหลดไฟล์แนบผ่าน `/api-claw/files:upload` | อ่านอัตโนมัติจาก `~/.openclaw/openclaw.json` (path `plugins.entries.kimi-claw.config.bridge.token`) หรือตั้งเองผ่าน `KIMI_BOT_TOKEN` |

ตั้งค่าใน `~/.bashrc` (ทำไว้แล้วบนเครื่องนี้):

```bash
export KIMI_TOKEN=eyJhbGciOi...   # user JWT จากเบราว์เซอร์
```

แล้ว `source ~/.bashrc` หรือเปิด terminal ใหม่

## send-message.py

```bash
./send-message.py "สวัสดี"                          # ส่งข้อความ แล้ว stream คำตอบ
echo "สรุปข่าววันนี้" | ./send-message.py           # รับข้อความจาก stdin
./send-message.py --file ./report.pdf "สรุปไฟล์นี้"  # แนบไฟล์ (agent อ่านเนื้อไฟล์ได้)
./send-message.py --file a.png --file b.csv "เทียบ 2 ไฟล์"   # แนบหลายไฟล์
./send-message.py --chat-id <uuid> "hi"             # ส่งเข้าห้องอื่น
./send-message.py --timeout 1800 "งานยาว ๆ"          # รอคำตอบนานสุด 30 นาที (default 600s)
```

พฤติกรรม:

- พิมพ์ log ไปที่ stderr (`[sent message_id=...]`, `[attached ...]`) ส่วน **คำตอบของ agent พิมพ์ที่ stdout** — เอาไป pipe ต่อได้ เช่น `./send-message.py "..." | tee reply.txt`
- poll คำตอบทุก 1 วินาที จนสถานะข้อความเป็น `COMPLETED`

Exit codes:

| code | ความหมาย |
|---|---|
| 0 | ได้คำตอบครบ (COMPLETED) |
| 1 | RPC/transport error (เช่น token หมดอายุ → HTTP 401) |
| 2 | หมดเวลารอ (คำตอบอาจยังมาทีหลังในแอป) |
| 3 | คำตอบจบด้วยสถานะผิดปกติ (CANCELLED/ERROR/TRUNCATED) |

ตัวแปร env เสริม: `KIMI_CHAT_ID` (ห้อง default), `KIMI_BOT_TOKEN` (override bot token)

## listen-messages.py

```bash
./listen-messages.py                        # ฟังห้อง default (bridge) — Ctrl+C หยุด
./listen-messages.py --chat-id <uuid>       # ฟังห้องอื่น
./listen-messages.py --interval 3           # poll ทุก 3 วิ (default 2)
./listen-messages.py --stream               # พิมพ์คำตอบขณะบอทกำลังพิมพ์ (default: รอจนจบ)
./listen-messages.py --include-existing     # พิมพ์ข้อความล่าสุดที่มีอยู่ก่อนด้วย
```

พิมพ์เฉพาะข้อความ `role: assistant` เท่านั้น (ข้อความ user ถูกข้าม)

รูปแบบ output: `[assistant] ชื่อผู้ส่ง: ข้อความ` ตามด้วย JSON เต็มของข้อความนั้น (raw response object ทั้งก้อน) พิมพ์ต่อท้ายทันทีที่ข้อความ stream จบ (ถึงสถานะ COMPLETED/CANCELLED/TRUNCATED/ERROR)

ข้อจำกัด: realtime `Subscribe` RPC สงวนไว้ให้ bot token (user JWT โดนปฏิเสธ `invalid_argument`) และการใช้ bot token ไป subscribe จะชน stream ของ bridge — สคริปต์นี้จึง poll `ListMessages` แทน ข้อความหน่วงประมาณ `--interval` วินาที

## kimi-rooms.py

```bash
./kimi-rooms.py list                                   # ห้องทั้งหมด: id, type, จำนวนสมาชิก, ชื่อ
./kimi-rooms.py create "ชื่อห้อง"                       # สร้างห้องกลุ่ม + เชิญบอท Jinx อัตโนมัติ
./kimi-rooms.py create "ชื่อห้อง" --instruction "..."    # กำหนด instruction ของห้อง
./kimi-rooms.py create "ชื่อห้อง" --bot-id ""           # สร้างโดยไม่เชิญบอท
./kimi-rooms.py delete <room_id>                       # ลบห้อง
```

- `create` พิมพ์ `room_id` ของห้องใหม่บรรทัดแรก — เอาไปใช้กับ `send-message.py --chat-id` ได้ทันที
- `list` รองรับ pagination อัตโนมัติ (`--page-size` ปรับได้)

## ข้อจำกัดที่รู้แล้ว

- **สร้างห้อง direct (DM) ผ่าน API ไม่ได้** — `CreateRoom` ได้แต่ห้องกลุ่ม; ห้อง direct กับบอทต้องสร้างจากหน้าแอป Kimi
- **local agent ตอบเฉพาะห้องที่ bridge ผูกไว้** (ห้อง direct เดิม `19ec0b57-e362-8944-8000-092b3b0f50ef`) — ส่งเข้าห้องอื่นข้อความจะถึง แต่ agent ในเครื่องไม่ dispatch
- ชื่อห้องบางคำโดน content filter ของ server ปฏิเสธ (HTTP 400 `invalid_argument`) — เปลี่ยนชื่อแล้วลองใหม่
- `KIMI_TOKEN` ตายทันทีถ้ากด logout ในเบราว์เซอร์ แม้ยังไม่ถึงวันหมดอายุ
- token **ไม่ได้ผูกกับเครื่อง/เบราว์เซอร์ต้นทาง** (ทดสอบแล้ว: ยิงจากเครื่องอื่นโดยส่งแค่ `authorization: Bearer` ล้วน ๆ ก็ผ่าน) — copy สคริปต์ + token ไปรันเครื่องไหนก็ได้; ถ้าแนบไฟล์ต้องตั้ง `KIMI_BOT_TOKEN` เพิ่มบนเครื่องนั้น

## เมื่อ token หมดอายุ (HTTP 401)

1. เปิด kimi.com ในเบราว์เซอร์ → F12 → Network → ส่งข้อความอะไรก็ได้ 1 ครั้ง
2. หา request ไปที่ `www.kimi.com/apiv2/...` → คัดลอกค่า header `authorization: Bearer ...`
3. แทนที่ค่าใน `~/.bashrc` บรรทัด `export KIMI_TOKEN=...` แล้ว `source ~/.bashrc`

## ความปลอดภัย

- `KIMI_TOKEN` คือ session จริงของบัญชี — ใครได้ไปก็อ่าน/ส่งข้อความในนามเราได้ อย่า commit หรือวางที่สาธารณะ
- สคริปต์อ่าน bot token จาก `~/.openclaw/openclaw.json` — ไฟล์นั้นควร `chmod 600`
- API พวกนี้ไม่มีเอกสาร official อาจเปลี่ยนโดยไม่แจ้ง; การ automate บัญชีตัวเองอาจขัด ToS ของ Kimi — ใช้ปริมาณพอเหมาะ
