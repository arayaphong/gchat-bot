from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import google_auth_httplib2
import httplib2
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from helpers.file_access_policy import SendableFilePolicy
from helpers.jsonl_log import append_jsonl
from helpers.services import CardPresenter, CredentialService

DRIVE_UPLOAD_TIMEOUT_SECONDS = 60


class ChatGateway:
    def __init__(
        self,
        credential_service: CredentialService,
        card_presenter: CardPresenter,
        chat_in_log: Path,
        chat_out_log: Path,
        file_policy: SendableFilePolicy,
        drive_folder_id: str,
    ) -> None:
        self._credential_service = credential_service
        self._card_presenter = card_presenter
        self._chat_in_log = chat_in_log
        self._chat_out_log = chat_out_log
        self._file_policy = file_policy
        self._drive_folder_id = drive_folder_id

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

    def _post_message(self, space: str, thread: str, body: dict[str, Any]) -> None:
        token = self._credential_service.get_bot_token()
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

    def send_followup(
        self, space: str, thread: str, text: str, provider: str = "openclaw"
    ) -> None:
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        try:
            if not text:
                text = " "
            envelope = (
                self._card_presenter.build_card(text, provider)
                if provider == "jinx_system"
                else self._card_presenter.build_text(text)
            )
            body = envelope["hostAppDataAction"]["chatDataAction"]["createMessageAction"][
                "message"
            ]
            self._post_message(space, thread, body)
        except Exception as e:  # noqa: BLE001
            print(f"[send_followup error] {e}")

    def _build_user_drive_service(self) -> Any:
        # files.create (drive.file scope) requires user auth — we upload
        # under the user's own Drive (impersonation), which sidesteps Chat's
        # "multiple attachments must be media-only" restriction entirely.
        # Built with an explicit timeout — googleapiclient's execute() has
        # none of its own, and a hung upload would jam the single-flight
        # lock permanently.
        authed_http = google_auth_httplib2.AuthorizedHttp(
            self._credential_service.get_user_creds(),
            http=httplib2.Http(timeout=DRIVE_UPLOAD_TIMEOUT_SECONDS),
        )
        return build("drive", "v3", http=authed_http)

    def _upload_to_drive(
        self, drive_service: Any, file_path: Path, display_name: str
    ) -> dict[str, Any]:
        media = MediaFileUpload(str(file_path), resumable=True)
        return (
            drive_service.files()
            .create(
                body={"name": display_name, "parents": [self._drive_folder_id]},
                media_body=media,
                fields="id,name,webViewLink,webContentLink,thumbnailLink,iconLink",
            )
            .execute()
        )

    def send_files(
        self, space: str, thread: str, files: list[dict[str, str]], fallback_text: str = ""
    ) -> None:
        """
        files: [{filePath, filename, caption}]
        อัปโหลดขึ้น Google Drive ของผู้ใช้ (impersonate) แล้วส่งเป็น preview card รวมเดียว
        """
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        drive_service = self._build_user_drive_service()
        previews: list[dict[str, str]] = []

        for f in files:
            try:
                fp = Path(f.get("filePath", "")).resolve()

                if not fp.exists():
                    print(f"[send_files] skip not exists: {fp}")
                    continue

                if not self._file_policy.is_allowed(fp):
                    print(f"[send_files] skip file outside allowed roots: {fp}")
                    continue

                display_name = Path(f.get("filename") or "").name or fp.name
                uploaded = self._upload_to_drive(drive_service, fp, display_name)
                web_view_link = uploaded.get("webViewLink", "")
                if web_view_link:
                    previews.append(
                        {
                            "name": uploaded.get("name", display_name),
                            "webViewLink": web_view_link,
                            "thumbnailLink": uploaded.get("thumbnailLink", ""),
                        }
                    )
            except Exception as e:  # noqa: BLE001
                print(f"[send_files error] {e} file={f}")

        if not previews:
            if fallback_text:
                self.send_followup(space, thread, fallback_text)
            return

        try:
            envelope = self._card_presenter.build_file_preview_card(fallback_text, previews)
            body = envelope["hostAppDataAction"]["chatDataAction"]["createMessageAction"][
                "message"
            ]
            self._post_message(space, thread, body)
        except Exception as e:  # noqa: BLE001
            print(f"[send_files error] failed to post preview card: {e}")
