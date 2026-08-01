from __future__ import annotations

FILE_META_OPEN = "[FILE_META]"
FILE_META_CLOSE = "[/FILE_META]"

ATTACHMENT_INSTRUCTION_OPEN = "[ATTACHMENT_INSTRUCTION]"
ATTACHMENT_INSTRUCTION_CLOSE = "[/ATTACHMENT_INSTRUCTION]"

STICKER_KIND_LABEL = "kind: sticker (GIF)"

FAILED_ATTACHMENT_TEMPLATE = "[Attachment {name} failed to download: {error}]"

STICKER_INSTRUCTION = (
    "The sticker/GIF below was sent by the user as a reaction/expression "
    "(not an uploaded photo) — answer directly from what you see, keeping "
    "in mind it's a sticker:"
)
IMAGE_INSTRUCTION = (
    "The images below are already attached above — answer directly from "
    "what you see:"
)
OTHER_FILE_INSTRUCTION = (
    "Call the read tool on each path below before answering questions about it:"
)

FILE_SEND_CAPABILITY = """
[SYSTEM CAPABILITY: FILE ATTACHMENT]
คุณสามารถส่งไฟล์แนบกลับไปให้ผู้ใช้ใน Google Chat ได้!

วิธีส่งไฟล์:
1. สร้างไฟล์จริงก่อนเสมอที่ /tmp/openclaw/ หรือ /home/arme/.openclaw/workspace/uploads/
   ตัวอย่าง Python: 
   ```python
   from pathlib import Path
   Path("/tmp/openclaw").mkdir(parents=True, exist_ok=True)
   Path("/tmp/openclaw/report.pdf").write_bytes(...)
   ```

2. เมื่อไฟล์พร้อมแล้ว ให้เรียก tool `upload-file`:
   - filePath: พาธเต็ม เช่น /tmp/openclaw/report.pdf (ต้องมีอยู่จริง)
   - filename: ชื่อไฟล์ที่ผู้ใช้จะเห็น เช่น report.pdf
   - message: คำอธิบายไฟล์

คุณส่งได้หลายไฟล์ในครั้งเดียวโดยเรียก tool หลายครั้ง
ถ้าผู้ใช้ขอ PDF, Excel, รายงาน, export ให้สร้างไฟล์จริงแล้วเรียก upload-file ทันที ห้ามตอบแค่บอกว่าสร้างแล้ว

ตัวอย่าง:
User: ขอรายงานเป็น PDF
-> สร้าง /tmp/openclaw/report.pdf แล้วเรียก upload-file filePath=/tmp/openclaw/report.pdf filename=report.pdf message="รายงานครับ"

ถ้าไม่สามารถเรียก tool ได้ ให้ใช้ fallback tag: [[ATTACH:/tmp/openclaw/report.pdf]]
"""
