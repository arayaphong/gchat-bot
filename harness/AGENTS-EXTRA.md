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

## [SYSTEM CAPABILITY: SCHEDULED REMINDERS]
When the user asks to be reminded or to run something at a later time, create an
OpenClaw cron job with the `openclaw cron add` CLI — never wait with `sleep`
inside a turn and never promise a reminder without creating the job.

- The current request contains a `[SESSION_CONTEXT]` block with the exact
  `sessionKey` of this thread. Always pin the job to it with
  `--session "session:<sessionKey>"` so the result is delivered back to this
  thread. Never invent or alter the key, and never create reminder jobs
  without it.
- Use `--tz Asia/Bangkok` for every job.
- One-shot reminders: use `--at` together with `--delete-after-run`.
- Pass the reminder text via `--message`; when the job fires, the message is
  injected into this session and your reply is relayed to the user in Google
  Chat automatically.
- Add `--agent main` to every job.
- After creating a job, confirm to the user briefly with the scheduled time.

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
