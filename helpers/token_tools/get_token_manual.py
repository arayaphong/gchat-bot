from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from helpers.token_tools.oauth_config import USER_OAUTH_SCOPES, write_oauth_token
except ModuleNotFoundError:  # Direct execution by file path.
    from oauth_config import USER_OAUTH_SCOPES, write_oauth_token


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json",
        USER_OAUTH_SCOPES,
    )
    flow.redirect_uri = "http://localhost"

    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\n1. เปิดลิงก์นี้ในเบราว์เซอร์:")
    print(auth_url)
    print(
        "\n2. Login เสร็จมันจะเด้งไป http://localhost/?code=... "
        "ก็อป URL ทั้งบรรทัดมาวางข้างล่าง\n"
    )

    redirect_url = input("วาง Full Redirect URL ที่ได้มา: ").strip()
    flow.fetch_token(authorization_response=redirect_url)

    write_oauth_token(Path("token.json"), flow.credentials.to_json())
    print("\nOK token.json created!")


if __name__ == "__main__":
    main()
