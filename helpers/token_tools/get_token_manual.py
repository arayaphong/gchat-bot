from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/chat.messages",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
flow.redirect_uri = "http://localhost"

auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
print("\n1. เปิดลิงก์นี้ในเบราว์เซอร์:")
print(auth_url)
print("\n2. Login เสร็จมันจะเด้งไป http://localhost/?code=... ก็อป URL ทั้งบรรทัดมาวางข้างล่าง\n")

redirect_url = input("วาง Full Redirect URL ที่ได้มา: ").strip()
# ดึง code จาก url นั้น แต่ fetch_token ต้องใช้ทั้ง url เพื่อให้ verifier ตรง
flow.fetch_token(authorization_response=redirect_url)

with open('token.json','w') as f:
    f.write(flow.credentials.to_json())
print("\nOK token.json created!")
