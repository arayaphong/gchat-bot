# gchat-bot

Google Chat bot webhook (Flask) that:
- receives Chat events at /chat
- verifies Google Chat bearer tokens
- downloads Drive attachments from incoming messages
- sends text + image context to Moonshot Kimi (model: kimi-k3)
- renders markdown-like responses into Google Chat cards

License: GNU GPL v3.0 (see LICENSE).

## Project Files

- app.py: main webhook server
- helpers/md_to_gchat.py: markdown -> Google Chat card widgets
- helpers/get_token.py: OAuth token helper via local callback server
- helpers/get_token_manual.py: OAuth token helper via manual redirect URL paste
- helpers/manual_token.py: one-off token fetch script with hardcoded code value

## Requirements

Python 3.10+ recommended.

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
- MOONSHOT_API_KEY: Moonshot API key
- GCHAT_AUDIENCE: exact Chat webhook URL audience (recommended), example: https://your-domain/chat

Optional:
- GCHAT_PROJECT_NUMBER: Google Cloud Project Number fallback audience
- GCHAT_BOT_CRED: path to service account credentials file (default: ./credentials.json)
- GCHAT_TOKEN_FILE: path to user OAuth token file (default: ./token.json)
- MAX_ATTACHMENT_BYTES: max bytes per downloaded attachment (default: 20971520)
- MAX_IMAGE_EMBED_BYTES: max bytes for base64 image embedding to model (default: 8388608)

OpenClaw gateway token is auto-loaded from ~/.openclaw/openclaw.json at path gateway.auth.token.

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

## Troubleshooting

1. 401 unauthorized on /chat
- verify GCHAT_AUDIENCE matches the exact endpoint URL configured in Google Chat
- confirm Chat app is calling this endpoint and includes Authorization header

2. Drive download fails
- regenerate token.json with Drive scope
- verify attachment file is accessible by the authenticated user

3. Module not found errors
- install dependencies in the same Python environment used to run app.py
