from __future__ import annotations

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
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    save_user_token(creds, TOKEN_FILE)
    print("OK token.json created with Drive and Google Chat scopes (mode 0600)")


if __name__ == "__main__":
    main()
