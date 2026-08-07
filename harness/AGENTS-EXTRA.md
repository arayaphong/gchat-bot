## Google Chat Integration Rules
Apply these instructions only when the session runtime metadata contains `channel=googlechat`.

## [RULE] Do Not Use Markdown Tables
- Do not use Markdown tables in Google Chat responses.
- Use bullet lists or bold text for structured content instead.

## [SYSTEM CAPABILITY: FILE ATTACHMENT]
To send a file attachment:
- Write each final deliverable directly to `/home/arme/.openclaw/workspace/uploads`.
- Use the requested filename. Overwrite an existing file with that name; do not create a renamed copy.
- The Google Chat bridge automatically detects files created in this directory.

## [SYSTEM CAPABILITY: MEDIA ATTACHMENT]
To attach media, append a `MEDIA:` tag followed immediately by the absolute file path in the final response.

Example: `MEDIA:/home/arme/.openclaw/workspace/cat.jpg`

## Check Kimi Balance
When the user mentions checking a balance, remaining balance, the Moonshot API, or the Kimi API, run:

`curl https://api.moonshot.ai/v1/users/me/balance -H "Authorization: Bearer $MOONSHOT_API_KEY"`

Report the response amount in US dollars.