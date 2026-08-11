from __future__ import annotations

import getpass
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from helpers.google_scopes import USER_SCOPES
from helpers.token_tools.token_writer import save_user_token

CLIENT_SECRET_FILE = Path("client_secret.json")
TOKEN_FILE = Path("token.json")


def main() -> None:
    print(
        "This authorization includes Google Chat message access. "
        "An existing token must be replaced because adding a scope in code "
        "does not grant it to an old refresh token."
    )
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        USER_SCOPES,
    )
    flow.redirect_uri = "http://localhost"

    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\n1. เปิดลิงก์นี้ในเบราว์เซอร์:")
    print(auth_url)
    print(
        "\n2. Login เสร็จแล้ว ให้วาง Full Redirect URL "
        "จาก http://localhost ด้านล่าง (ค่าที่วางจะไม่แสดงบนหน้าจอ)\n"
    )

    redirect_url = getpass.getpass("วาง Full Redirect URL ที่ได้มา: ").strip()
    if not redirect_url:
        raise SystemExit("No redirect URL supplied; token.json was not changed")
    flow.fetch_token(authorization_response=redirect_url)

    save_user_token(flow.credentials, TOKEN_FILE)
    print("\nOK token.json created with Drive and Google Chat scopes (mode 0600)")


if __name__ == "__main__":
    main()
