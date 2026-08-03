# gchat-bot

Google Chat bot webhook (Flask) that:
- receives Chat events at /chat
- verifies Google Chat bearer tokens
- downloads Drive attachments from incoming messages
- uses the Kimiclaw WebSocket provider by default for every session model
- renders markdown-like responses into Google Chat cards

License: GNU GPL v3.0 (see LICENSE).

## Project Files

- app.py: main webhook server
- helpers/md_to_gchat.py: markdown -> Google Chat card widgets
- helpers/get_token.py: OAuth token helper via local callback server
- helpers/get_token_manual.py: OAuth token helper via manual redirect URL paste
- helpers/manual_token.py: one-off token fetch script with hardcoded code value

## Requirements

Python 3.10+ is recommended. The Kimiclaw WebSocket client runs natively in
Python; the bot no longer starts a Node.js bridge process. The `openclaw` CLI
must still be available for the existing `/models`, `/abort`, and session
administration commands.

Install dependencies:

```fish
python -m pip install -r requirements.txt
```

## Credentials and Tokens

This project uses two auth paths:

1. Bot service account credentials (for Google Chat bot send API)
2. User OAuth token (for Google Drive attachment download)

Expected default files in project root:
- credentials.json (service account)
- client_secret.json (OAuth client)
- token.json (user OAuth token)

Generate token.json with either:

```bash
python helpers/get_token.py
```

or

```bash
python helpers/get_token_manual.py
```

## Environment Variables

Required:
- GCHAT_AUDIENCE: exact Chat webhook URL audience (recommended), example: https://your-domain/chat

Optional:
- GCHAT_PROJECT_NUMBER: Google Cloud Project Number fallback audience
- GCHAT_BOT_CRED: path to service account credentials file (default: ./credentials.json)
- GCHAT_TOKEN_FILE: path to user OAuth token file (default: ./token.json)
- MAX_ATTACHMENT_BYTES: max bytes per downloaded attachment (default: 20971520)
- MAX_IMAGE_EMBED_BYTES: max bytes for base64 image embedding to model (default: 8388608)
- GCHAT_PROVIDER: agent transport, `kimiclaw` or `openclaw` (default: kimiclaw)
- OPENCLAW_GATEWAY_URL: OpenClaw WebSocket URL used by Kimiclaw (default: ws://127.0.0.1:18789)
- OPENCLAW_GATEWAY_WS_URL: legacy alias for OPENCLAW_GATEWAY_URL
- OPENCLAW_GATEWAY_TOKEN: gateway token, useful when connecting through a remote relay
- OPENCLAW_CONFIG_FILE: OpenClaw config read by the Kimiclaw bridge (default: ~/.openclaw/openclaw.json)

The gateway token is read from `OPENCLAW_GATEWAY_TOKEN` first. If it is unset,
the provider loads `gateway.auth.token` from the OpenClaw config file.

## Run

```bash
python app.py
```

On startup, the app prints GPL notice text and starts on:
- host: 0.0.0.0
- port: 8080

Endpoints:
- POST /chat
- GET /

## Google Chat Configuration Notes

In Google Chat API / Chat app settings:
- set bot endpoint URL to your public /chat URL
- ensure authentication token header is sent (Authorization: Bearer ...)
- use the same GCP project as GCHAT_PROJECT_NUMBER

## Security Notes

- Do not commit credentials.json, token.json, or client_secret.json.
- .gitignore already excludes these sensitive files.
- Incoming /chat requests are rejected unless JWT verification passes.
- Attachment and image sizes are capped to reduce abuse and memory pressure.

## Known Behavior

- If model response is fast, reply returns inline.
- If model response exceeds timeout, bot posts follow-up message in thread asynchronously.
- Attachments are saved using the MIME type to determine file extension.
- Kimiclaw is the default provider for every model and uses `channel: kimi-claw`
  with the same persisted session key. OpenClaw chooses the model from that session
  (or its configured default), so provider selection is not tied to a model key.
- Set `GCHAT_PROVIDER=openclaw` to use the legacy HTTP provider for normal
  agent requests.
- WebSocket deltas are assembled internally; Google Chat receives one final reply
  because the current Chat transport does not edit messages live.
- `/model ...` is sent through the gateway's `chat.send` command pipeline, so model
  changes work regardless of the currently selected model.

## Troubleshooting

1. 401 unauthorized on /chat
- verify GCHAT_AUDIENCE matches the exact endpoint URL configured in Google Chat
- confirm Chat app is calling this endpoint and includes Authorization header

2. Drive download fails
- regenerate token.json with Drive scope
- verify attachment file is accessible by the authenticated user

3. Module not found errors
- install dependencies in the same Python environment used to run app.py
