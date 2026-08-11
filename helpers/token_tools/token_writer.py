"""Validated persistence for an interactive user OAuth credential."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from helpers.credential_storage import locked_atomic_write_secret
from helpers.google_scopes import USER_SCOPES


class TokenScopeGrantError(RuntimeError):
    """Raised when an OAuth response does not grant every required scope."""

    def __init__(self, missing_scopes: Sequence[str]) -> None:
        self.missing_scopes = tuple(sorted(set(missing_scopes)))
        super().__init__(
            "Google did not grant every required scope; token.json was not changed. "
            "Run the authorization flow again with consent."
        )


def save_user_token(credentials: Any, token_file: Path | str) -> None:
    """Validate granted scopes and atomically persist a user token."""

    granted = credentials.granted_scopes
    if granted is None:
        granted = credentials.scopes
    granted_scopes = tuple(granted or ())
    missing = set(USER_SCOPES).difference(granted_scopes)
    if missing:
        raise TokenScopeGrantError(tuple(missing))

    token_info = json.loads(credentials.to_json())
    if not isinstance(token_info, dict):
        raise TypeError("OAuth credential serialization is invalid")
    token_info["scopes"] = list(granted_scopes)
    locked_atomic_write_secret(
        token_file,
        json.dumps(token_info, separators=(",", ":")),
    )
