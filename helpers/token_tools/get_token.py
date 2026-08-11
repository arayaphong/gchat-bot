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
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    write_oauth_token(Path("token.json"), creds.to_json())
    print("OK token.json created with Drive and Google Chat scopes")


if __name__ == "__main__":
    main()
