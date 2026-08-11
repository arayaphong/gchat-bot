import webbrowser
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from helpers.token_tools.oauth_config import USER_OAUTH_SCOPES, write_oauth_token
except ModuleNotFoundError:  # Direct execution by file path.
    from oauth_config import USER_OAUTH_SCOPES, write_oauth_token


def _run_manual_flow() -> None:
    try:
        from helpers.token_tools.get_token_manual import main as manual_main
    except ModuleNotFoundError:  # Direct execution by file path.
        from get_token_manual import main as manual_main

    print(
        "No runnable browser was found; switching to the manual redirect URL flow."
    )
    manual_main()


def main() -> None:
    try:
        webbrowser.get()
    except webbrowser.Error:
        _run_manual_flow()
        return

    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json",
        USER_OAUTH_SCOPES,
    )
    try:
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
        )
    except webbrowser.Error:
        _run_manual_flow()
        return
    write_oauth_token(Path("token.json"), creds.to_json())
    print("OK token.json created with Drive and Google Chat scopes")


if __name__ == "__main__":
    main()
