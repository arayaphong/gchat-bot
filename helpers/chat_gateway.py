from __future__ import annotations

import json
import mimetypes
import shutil
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

CHAT_UPLOAD_TIMEOUT_SECONDS = 60


class ChatGateway:
    def __init__(
        self,
        credential_service: CredentialService,
        card_presenter: CardPresenter,
        chat_in_log: Path,
        chat_out_log: Path,
        file_policy: SendableFilePolicy,
    ) -> None:
        self._credential_service = credential_service
        self._card_presenter = card_presenter
        self._chat_in_log = chat_in_log
        self._chat_out_log = chat_out_log
        self._file_policy = file_policy

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
        except Exception as e:  # noqa: BLE001
            print(f"[send_followup error] {e}")

    def _build_user_chat_service(self) -> Any:
        # media.upload requires user auth (chat.messages scope). Built with an
        # explicit timeout — googleapiclient's execute() has none of its own,
        # and a hung upload would jam the single-flight lock permanently.
        authed_http = google_auth_httplib2.AuthorizedHttp(
            self._credential_service.get_user_creds(),
            http=httplib2.Http(timeout=CHAT_UPLOAD_TIMEOUT_SECONDS),
        )
        return build("chat", "v1", http=authed_http)

    @staticmethod
    def _upload_file(chat_service: Any, space: str, file_path: Path) -> dict[str, Any]:
        mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(file_path), mimetype=mimetype, resumable=True)
        return (
            chat_service.media()
            .upload(parent=space, body={"filename": file_path.name}, media_body=media)
            .execute()
        )

    def send_file_attachment(
        self, space: str, thread: str, file_path: Path, caption: str
    ) -> None:
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        chat_service = self._build_user_chat_service()

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"file not found: {file_path}")

        uploaded = self._upload_file(chat_service, space, file_path)

        body: dict[str, Any] = {"text": caption or f"📎 {file_path.name}", "attachment": [uploaded]}
        if thread:
            body["thread"] = {"name": thread}

        self.record_outgoing(body)
        chat_service.spaces().messages().create(parent=space, body=body).execute()

    def send_files(
        self, space: str, thread: str, files: list[dict[str, str]], fallback_text: str = ""
    ) -> None:
        """
        files: [{filePath, filename, caption}]
        อัปโหลดทุกไฟล์แล้วส่งรวมเป็นข้อความเดียว (พร้อมข้อความ fallback_text ถ้ามี)
        """
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        chat_service = self._build_user_chat_service()
        uploaded_attachments: list[dict[str, Any]] = []
        captions: list[str] = []

        for f in files:
            try:
                fp = Path(f.get("filePath", "")).resolve()

                if not fp.exists():
                    print(f"[send_files] skip not exists: {fp}")
                    continue

                if not self._file_policy.is_allowed(fp):
                    print(f"[send_files] skip file outside allowed roots: {fp}")
                    continue

                # rename via copy to temp; basename-only strips any path
                # traversal segments from the (model-controlled) filename
                desired_name = Path(f.get("filename") or "").name
                if desired_name and desired_name != fp.name:
                    tmp_path = fp.parent / desired_name
                    if tmp_path.exists():
                        print(f"[send_files] rename target already exists, keeping original name: {tmp_path}")
                    else:
                        shutil.copy(fp, tmp_path)
                        fp = tmp_path

                uploaded_attachments.append(self._upload_file(chat_service, space, fp))
                caption = f.get("caption") or f.get("message")
                if caption:
                    captions.append(caption)
            except Exception as e:  # noqa: BLE001
                print(f"[send_files error] {e} file={f}")

        if not uploaded_attachments:
            if fallback_text:
                self.send_followup(space, thread, fallback_text)
            return

        text = fallback_text or "\n".join(captions) or "📎 ไฟล์แนบ"
        body: dict[str, Any] = {"text": text, "attachment": uploaded_attachments}
        if thread:
            body["thread"] = {"name": thread}

        self.record_outgoing(body)
        chat_service.spaces().messages().create(parent=space, body=body).execute()
