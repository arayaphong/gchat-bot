## Google Chat Integration Rules
Apply only the Google Chat sections below when the session runtime metadata contains `channel=googlechat`.

## [RULE] Do Not Use Markdown Tables
- Do not use Markdown tables in Google Chat responses.
- Use bullet lists or bold text for structured content instead.

## [SYSTEM CAPABILITY: FILE ATTACHMENT]
To send a file attachment:
- Use the exact per-thread directory supplied in the current request inside
  `[THREAD_UPLOAD_DIRECTORY]`.
- Write each final deliverable directly into that directory. Never write a
  deliverable directly to its parent `/home/arme/.openclaw/workspace/uploads`.
- Use the requested filename. Overwrite an existing file with that name; do not create a renamed copy.
- The Google Chat bridge automatically detects files created in the supplied
  directory and sends them to that request's thread.

## [SYSTEM CAPABILITY: MEDIA ATTACHMENT]
To attach media, append a `MEDIA:` tag followed immediately by the absolute file path in the final response.

Example: `MEDIA:/home/arme/.openclaw/workspace/cat.jpg`

## Check Kimi Balance
When the user mentions checking a balance, remaining balance, the Moonshot API, or the Kimi API, run:

`curl https://api.moonshot.ai/v1/users/me/balance -H "Authorization: Bearer $MOONSHOT_API_KEY"`

Report the response amount in US dollars.

## Desktop Automation MCP
A desktop-automation MCP tool may be available in this environment.

- It can control GNOME desktop applications and system UI directly.
- Use it for GUI tasks such as opening apps, interacting with windows, managing files, changing settings, handling dialogs, and operating browser-based flows when a desktop action is more direct than shell commands.
- Prefer the MCP tool over giving the user manual click-by-click GNOME instructions when the task can be completed safely through automation.
- If a desktop action could be destructive or the target app/window is ambiguous, confirm intent before proceeding.
