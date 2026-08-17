from __future__ import annotations

FILE_META_OPEN = "[FILE_META]"
FILE_META_CLOSE = "[/FILE_META]"

ATTACHMENT_INSTRUCTION_OPEN = "[ATTACHMENT_INSTRUCTION]"
ATTACHMENT_INSTRUCTION_CLOSE = "[/ATTACHMENT_INSTRUCTION]"

THREAD_UPLOAD_INSTRUCTION_OPEN = "[THREAD_UPLOAD_DIRECTORY]"
THREAD_UPLOAD_INSTRUCTION_CLOSE = "[/THREAD_UPLOAD_DIRECTORY]"
THREAD_UPLOAD_INSTRUCTION = (
    "For automatic delivery to this Google Chat thread, write each final "
    "deliverable directly into this exact directory. Do not write files "
    "directly into its parent uploads directory:"
)

STICKER_KIND_LABEL = "kind: sticker (GIF)"

FAILED_ATTACHMENT_TEMPLATE = "[Attachment {name} failed to download: {error}]"

GOOGLE_WORKSPACE_TYPE_LABELS = {
    "application/vnd.google-apps.spreadsheet": "Google Sheets (สเปรดชีต)",
    "application/vnd.google-apps.document": "Google Docs (เอกสาร)",
    "application/vnd.google-apps.presentation": "Google Slides (สไลด์)",
    "application/vnd.google-apps.drawing": "Google Drawings (ภาพวาด)",
    "application/vnd.google-apps.form": "Google Forms (ฟอร์ม)",
}
GOOGLE_WORKSPACE_TYPE_FALLBACK_LABEL = "ไฟล์ Google Workspace"
CONVERTED_FILE_NOTE_TEMPLATE = (
    "note: ไฟล์นี้ถูกแปลงจาก {original_label} เป็น PDF เพื่อให้คุณอ่านเนื้อหาได้เท่านั้น "
    'ผู้ใช้ยังเข้าใจว่านี่คือไฟล์ {original_label} ต้นฉบับอยู่ ห้ามเรียกไฟล์นี้ว่า "PDF" '
    "ตอนคุยกับผู้ใช้ ให้เรียกตามประเภทไฟล์ต้นฉบับแทน"
)

QUOTED_MESSAGE_OPEN = "[QUOTED_MESSAGE]"
QUOTED_MESSAGE_CLOSE = "[/QUOTED_MESSAGE]"
QUOTED_MESSAGE_INSTRUCTION = (
    "The user is replying to (quoting) this earlier message — use it as "
    'context for what "this"/"it" refers to in their new message:'
)

SESSION_CONTEXT_OPEN = "[SESSION_CONTEXT]"
SESSION_CONTEXT_CLOSE = "[/SESSION_CONTEXT]"
SESSION_CONTEXT_INSTRUCTION = (
    "Runtime routing metadata for this conversation — use sessionKey verbatim "
    "when scheduling OpenClaw cron jobs that must report back to this thread:"
)

STICKER_INSTRUCTION = (
    "The sticker/GIF below was sent by the user as a reaction/expression "
    "(not an uploaded photo) — answer directly from what you see, keeping "
    "in mind it's a sticker:"
)
IMAGE_INSTRUCTION = (
    "Call the read tool on each image path below to view it before answering:"
)
OTHER_FILE_INSTRUCTION = (
    "Call the read tool on each path below before answering questions about it:"
)
