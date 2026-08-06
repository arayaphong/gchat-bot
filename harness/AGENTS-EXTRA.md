# AGENTS-EXTRA.md - Google Chat Integration Rules
Apply this document only to Google Chat sessions. Check the session runtime metadata for `channel=googlechat` to determine whether it applies.

## [RULE] Do Not Use Markdown Tables
- Most chat clients do not render Markdown tables correctly.
- Always use **bullet lists** or **bold text** instead.
- Some clients may support tables, but never use them in Google Chat.

## [SYSTEM CAPABILITY: FILE ATTACHMENT]
You can send file attachments to users in Google Chat.
- Write every final deliverable file directly under `/home/arme/.openclaw/workspace/uploads`.
- Overwrite existing files; do not rename them.
- The Google Chat bridge detects newly created files in that directory automatically.
