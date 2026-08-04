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
- Include this tag in the response, with both `[[` and `]]`: `[[ATTACH:/tmp/openclaw/report.pdf]]`
- Include multiple tags to attach multiple files.