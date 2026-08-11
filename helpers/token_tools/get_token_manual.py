import hmac
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from helpers.token_tools.oauth_config import USER_OAUTH_SCOPES, write_oauth_token
except ModuleNotFoundError:  # Direct execution by file path.
    from oauth_config import USER_OAUTH_SCOPES, write_oauth_token


def _authorization_code_from_redirect(
    redirect_url: str,
    expected_state: str,
) -> str:
    """Validate a localhost redirect and return its one-time OAuth code."""
    try:
        parsed = urlsplit(redirect_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Redirect URL ไม่ถูกต้อง") from error

    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or port not in {None, 80}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.fragment
    ):
        raise ValueError("Redirect URL ต้องเป็น http://localhost/ เท่านั้น")

    parameters = parse_qs(parsed.query, keep_blank_values=True)
    if "error" in parameters:
        raise ValueError("Google ปฏิเสธคำขอ OAuth")

    returned_states = parameters.get("state", [])
    if (
        len(returned_states) != 1
        or not expected_state
        or not hmac.compare_digest(returned_states[0], expected_state)
    ):
        raise ValueError("OAuth state ไม่ตรงกัน กรุณาเริ่มขั้นตอนใหม่")

    codes = parameters.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError("Redirect URL ไม่มี authorization code")
    return codes[0]


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json",
        USER_OAUTH_SCOPES,
    )
    flow.redirect_uri = "http://localhost"

    auth_url, expected_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    print("\n1. เปิดลิงก์นี้ในเบราว์เซอร์:")
    print(auth_url)
    print(
        "\n2. Login เสร็จมันจะเด้งไป http://localhost/?code=... "
        "ก็อป URL ทั้งบรรทัดมาวางข้างล่าง\n"
    )

    redirect_url = input("วาง Full Redirect URL ที่ได้มา: ").strip()
    authorization_code = _authorization_code_from_redirect(
        redirect_url,
        expected_state,
    )
    flow.fetch_token(code=authorization_code)

    write_oauth_token(Path("token.json"), flow.credentials.to_json())
    print("\nOK token.json created!")


if __name__ == "__main__":
    main()
