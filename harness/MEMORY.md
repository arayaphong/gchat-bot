# MEMORY.md - Long-term Memory

## Check Kimi Balance
When the user mentions: check balance, remaining balance, balance, Moonshot API, or Kimi API
run: `curl https://api.moonshot.ai/v1/users/me/balance -H "Authorization: Bearer $MOONSHOT_API_KEY"`
The response amount is in US dollars.

## [RULE] Do Not Use Markdown Tables
- Most chat clients do not render Markdown tables correctly.
- Always use **bullet lists** or **bold text** instead.
- Some clients may support tables, but never use them in Google Chat.

## [SYSTEM CAPABILITY: FILE ATTACHMENT]
You can send file attachments to users in Google Chat.
- Write every final deliverable file directly under `/home/arme/.openclaw/workspace/uploads`.
- Overwrite existing files; do not rename them.
- The Google Chat bridge detects newly created files in that directory automatically.
