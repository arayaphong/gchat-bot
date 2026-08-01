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
