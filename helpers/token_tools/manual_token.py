"""Backward-compatible entry point for the interactive manual OAuth flow."""

try:
    from helpers.token_tools.get_token_manual import main
except ModuleNotFoundError:  # Direct execution by file path.
    from get_token_manual import main


if __name__ == "__main__":
    main()
