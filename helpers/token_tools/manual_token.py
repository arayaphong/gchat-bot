from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/chat.messages",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
flow.redirect_uri = "http://localhost"
code = "4/0AXEQxICNUak_NK7gpYygSOAUVaCFxzT4AEYS7RhPa3btknEUrpmoD0wJpm-5WC93kA1SSg"
flow.fetch_token(code=code)
with open("token.json", "w") as f:
    f.write(flow.credentials.to_json())
print("OK token.json created - long lived")
