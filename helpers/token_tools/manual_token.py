from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
flow.redirect_uri = "http://localhost"
code = "4/0AXEQxICNUak_NK7gpYygSOAUVaCFxzT4AEYS7RhPa3btknEUrpmoD0wJpm-5WC93kA1SSg"
flow.fetch_token(code=code)
Path("token.json").write_text(flow.credentials.to_json(), encoding="utf-8")
print("OK token.json created - long lived")
