# แนวทาง: ตรวจ error ของ OpenClaw run หลังส่งข้อความ (runId-based logcheck)

## เป้าหมาย

หลังบอทส่งข้อความเข้า OpenClaw gateway แล้ว ให้บอทตรวจยืนยันจาก log ของ gateway ว่า run นั้นจบปกติหรือมี error — โดยอ้างอิงด้วย **runId** ของ run นั้นโดยเฉพาะ ไม่ปนกับ run อื่นที่กำลังทำงานพร้อมกัน

## หลักการ

1. response ของ `POST /v1/chat/completions` มี field `id` รูปแบบ `chatcmpl_<uuid>` — ค่านี้คือ **runId ตัวเดียวกับ**ที่ gateway พิมพ์ลง journal (เช่น `runId=chatcmpl_05527b78-...`)
2. gateway log ทั้งหมดอยู่ใน journald ของ user (`openclaw-gateway.service`) ไม่มี log ไฟล์แยกที่อัปเดตสด
3. ดังนั้นจับ `id` จาก response → ยิง `journalctl --user -u openclaw-gateway --since <เวลาก่อนส่ง>` → กรองเฉพาะบรรทัดที่มี runId และตรง pattern error

## Error patterns ที่ควรจับ

จากการสำรวจ journal จริง (ดูตัวอย่างเคส timeout 2026-08-05 กลางคืน):

| Pattern | ความหมาย |
| --- | --- |
| `isError=true` | run จบด้วย error (run ปกติจะเป็น `isError=false` — ห้าม grep คำว่า `error` ลอยๆ เพราะจะโดนบรรทัดนี้ทั้งหมด) |
| `[model-fetch] error` | เรียก provider ไม่สำเร็จ เช่น `AbortError` จาก timeout 120s |
| `timedOut=true` | run ถูกตัดเพราะ timeout |
| `errorMessage` | event `chat state:"error"` มีรายละเอียด error |

## โครงสร้างโค้ดที่แนะนำ

### 1. `helpers/providers/openclaw_logcheck.py` (ไฟล์ใหม่)

หน้าที่เดียว: ถาม journal ด้วย runId แล้วคืนบรรทัดที่เป็น error

```python
def check_run_errors(run_id: str, since: datetime) -> list[str]:
    if not run_id:
        return []
    # subprocess.run(["journalctl", "--user", "-u", "openclaw-gateway",
    #                 "--since", since.strftime("%Y-%m-%d %H:%M:%S"),
    #                 "--no-pager", "--output=short-iso"], ...)
    # กรอง: run_id in line and ERROR_PATTERN.search(line)
```

ข้อกำหนดสำคัญ:

- runId ว่าง (response ไม่มี `id`, หรือตอน unit test mock) → คืน `[]` ทันที ห้ามยิง journalctl
- `journalctl` หาย / timeout / อ่าน journal ไม่ได้ → catch แล้วคืน `[]` (logcheck เป็น watchdog ห้ามทำ flow หลักพัง)
- ใส่ `timeout` ให้ subprocess เสมอ (แนะนำ 10 วินาที)

### 2. `helpers/providers/openclaw_provider.py`

`ask_openclaw_direct` ดึง `id` ออกจาก response body แล้วคืนเพิ่ม:

```python
run_id = response_body.get("id", "") if isinstance(response_body, dict) else ""
return {"text": "", "run_id": run_id}
```

### 3. `helpers/providers/openclaw_client.py`

ใน `OpenClawClient.send_turn`: จับเวลาก่อนส่ง → หลังได้ response ก็เช็ค → ตอนนี้แค่ print เตือน ยังไม่เปลี่ยน flow ตอบกลับ

```python
sent_at = datetime.now()
result = ask_openclaw_direct(...)
for line in check_run_errors(result.get("run_id", ""), sent_at):
    print(f"⚠️ [logcheck] run={result['run_id']}: {line}")
```

### 4. Tests

- mock response ใส่ `"id": "chatcmpl_abc"` แล้ว assert ว่า `run_id` ไหลออกมา
- mock `ask_openclaw_direct` คืน `{"text": ...}` โดยไม่มี `run_id` — logcheck ต้อง no-op (เป็นเหตุผลที่ต้อง guard runId ว่าง)

## ข้อจำกัด / ระวัง

- **ต้องรันบนเครื่องเดียวกับ gateway และ user เดียวกัน** ถึงจะอ่าน user journal ได้ (ปัจจุบันบอทรันบน t495 อยู่แล้ว) ถ้าแยกเครื่องในอนาคตต้องยิงผ่าน ssh หรือ `journalctl --remote`
- **race condition**: ถ้า run ยังไม่จบตอนเช็ค (เช่น gateway ตอบ HTTP กลับก่อน embedded run เสร็จ) error อาจยังไม่เขียนลง journal — ถ้าเจอบ่อยให้เพิ่ม delay สั้นๆ หรือ re-check แบบ background
- ตอนนี้เช็คเฉพาะเคสส่งสำเร็จ — ถ้าอยากเช็คตอน `ask_openclaw_direct` throw ด้วย (เช่น requests timeout 120s) ให้ย้าย logcheck ไปไว้ใน `except`/`finally`
- journald มี rotation — ย้อนหลังได้จำกัด อย่าออกแบบให้พึ่ง log เก่าหลายวัน

## แนวทางต่อยอด (ยังไม่ทำ)

- ส่งเตือนเข้า Google Chat แทน print (ต่อผ่าน `message_orchestrator`)
- นับ error ซ้ำของ provider เดียวกันแล้ว fallback ไป model สำรอง
- เก็บผล logcheck ลง jsonl เพื่อทำรายงาน uptime ของ run
