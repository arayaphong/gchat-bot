from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from helpers.jsonl_log import append_jsonl
from helpers.services import CardPresenter, CredentialService


class ChatGateway:
    def __init__(
        self,
        credential_service: CredentialService,
        card_presenter: CardPresenter,
        chat_in_log: Path,
        chat_out_log: Path,
    ) -> None:
        self._credential_service = credential_service
        self._card_presenter = card_presenter
        self._chat_in_log = chat_in_log
        self._chat_out_log = chat_out_log

    def record_incoming(self, raw_body: str) -> None:
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {"raw": raw_body}
        append_jsonl(self._chat_in_log, body)

    def record_outgoing(self, body: dict[str, Any]) -> None:
        append_jsonl(self._chat_out_log, body)

    def ack(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        self.record_outgoing(body)
        return body

    def send_followup(
        self, space: str, thread: str, text: str, provider: str = "openclaw"
    ) -> None:
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        try:
            token = self._credential_service.get_bot_token()
            body = self._card_presenter.build_card(text, provider)[
                "hostAppDataAction"
            ]["chatDataAction"]["createMessageAction"]["message"]

            if thread:
                body["thread"] = {"name": thread}

            self.record_outgoing(body)

            url = f"https://chat.googleapis.com/v1/{space}/messages"
            requests.post(
                url,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=15,
            )
        except Exception:  # noqa: BLE001, S110
            pass
