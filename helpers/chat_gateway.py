from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
CHAT_API_TIMEOUT_SECONDS = 15
CHAT_API_BASE_URL = "https://chat.googleapis.com/v1"

SPACE_NAME_RE = re.compile(r"^spaces/[^/?#\s]+$")
THREAD_NAME_RE = re.compile(r"^(spaces/[^/?#\s]+)/threads/[^/?#\s]+$")


class DriveUploadResponseError(RuntimeError):
    """Raised when Drive accepts an upload but returns no usable file link."""


class ChatApiResponseError(RuntimeError):
    """Raised when Google Chat returns a successful but unusable response."""


@dataclass(frozen=True, slots=True)
class FileDeliveryResult:
    """Outcome of one bot-to-user file delivery attempt.

    ``error`` retains the original exception so the caller can decide whether
    to retry and, after retries are exhausted, how to notify the user.  The
    gateway deliberately does not emit a Jinx failure message on its own.
    """

    file_path: Path
    display_name: str
    drive_file_id: str = ""
    web_view_link: str = ""
    error: Exception | None = None

    @property
    def success(self) -> bool:
        return self.error is None


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

    @staticmethod
    def _normalize_resource_name(
        value: str,
        pattern: re.Pattern[str],
        label: str,
    ) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a string")
        normalized = value.strip()
        if not pattern.fullmatch(normalized):
            raise ValueError(f"invalid {label} resource name")
        return normalized

    def _bot_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credential_service.get_bot_token()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _json_object(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise ChatApiResponseError(
                f"Google Chat returned invalid JSON for {operation}"
            ) from error
        if not isinstance(payload, dict):
            raise ChatApiResponseError(
                f"Google Chat returned a non-object response for {operation}"
            )
        return payload

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

    def _post_message(
        self,
        space: str,
        thread: str,
        body: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> None:
        if thread:
            body["thread"] = {"name": thread}

        self.record_outgoing(body)

        url = f"{CHAT_API_BASE_URL}/{space}/messages"
        response = requests.post(
            url,
            headers=self._bot_headers(),
            params={"requestId": request_id} if request_id else None,
            json=body,
            timeout=CHAT_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    def _message_body(self, text: str, provider: str) -> dict[str, Any]:
        if not text:
            text = " "
        envelope = (
            self._card_presenter.build_card(text, provider)
            if provider == "jinx_system"
            else self._card_presenter.build_text(text)
        )
        return envelope["hostAppDataAction"]["chatDataAction"][
            "createMessageAction"
        ]["message"]

    def create_root_thread(
        self,
        space: str,
        text: str,
        provider: str = "jinx_system",
        *,
        request_id: str | None = None,
    ) -> str:
        """Post a new root message in ``space`` and return its full thread name."""
        normalized_space = self._normalize_resource_name(
            space,
            SPACE_NAME_RE,
            "Google Chat space",
        )
        body = self._message_body(text, provider)
        self.record_outgoing(body)
        response = requests.post(
            f"{CHAT_API_BASE_URL}/{normalized_space}/messages",
            headers=self._bot_headers(),
            params={"requestId": request_id} if request_id else None,
            json=body,
            timeout=CHAT_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        created_message = self._json_object(response, "root message creation")
        thread = created_message.get("thread")
        thread_name = thread.get("name") if isinstance(thread, dict) else None
        match = (
            THREAD_NAME_RE.fullmatch(thread_name)
            if isinstance(thread_name, str)
            else None
        )
        if match is None or match.group(1) != normalized_space:
            raise ChatApiResponseError(
                "Google Chat root message response has no valid thread name"
            )
        return thread_name

    def send_followup(
        self,
        space: str,
        thread: str,
        text: str,
        provider: str = "openclaw",
        *,
        request_id: str | None = None,
    ) -> bool:
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        try:
            body = self._message_body(text, provider)
            self._post_message(space, thread, body, request_id=request_id)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[send_followup error] {e}")
            return False

    def _build_user_drive_service(self) -> Any:
        # files.create (drive.file scope) requires user auth — we upload
        # under the OAuth user's own Drive, which sidesteps Chat's
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

    @staticmethod
    def _display_name(file_path: Path, filename: str | None) -> str:
        requested = Path(filename or "").name
        if requested and requested not in {".", ".."}:
            return requested
        return file_path.name or "attachment"

    def send_file(
        self,
        space: str,
        thread: str,
        file_path: str | Path,
        *,
        filename: str | None = None,
        fallback_text: str = "",
        request_id: str | None = None,
        drive_file_id: str = "",
        web_view_link: str = "",
    ) -> FileDeliveryResult:
        """Upload one local file to Drive and post its preview card.

        A successful result means the Google Chat card post completed and, if
        no existing Drive receipt was supplied, the Drive upload completed too.
        Passing a non-empty ``web_view_link`` resumes at the card-post phase and
        avoids another Drive copy.  Failures retain the original exception and
        any newly created receipt so callers can retry idempotently.
        """
        if not space and "/threads/" in thread:
            space = thread.split("/threads/")[0]

        requested_path = Path(file_path).expanduser()
        display_name = self._display_name(requested_path, filename)
        resolved_path = requested_path
        drive_file_id = str(drive_file_id or "")
        web_view_link = str(web_view_link or "").strip()
        try:
            uploaded: dict[str, Any] = {}
            if not web_view_link:
                resolved_path = requested_path.resolve(strict=True)
                display_name = self._display_name(resolved_path, filename)

                if not resolved_path.is_file():
                    raise IsADirectoryError(f"not a regular file: {resolved_path}")
                if not self._file_policy.is_allowed(resolved_path):
                    raise PermissionError(
                        f"file is outside configured send roots: {resolved_path}"
                    )

                drive_service = self._build_user_drive_service()
                uploaded = self._upload_to_drive(
                    drive_service, resolved_path, display_name
                )
                drive_file_id = str(uploaded.get("id", ""))
                web_view_link = str(uploaded.get("webViewLink", "")).strip()
                if not web_view_link:
                    raise DriveUploadResponseError(
                        "Drive upload response did not include webViewLink"
                    )

            preview = {
                "name": str(uploaded.get("name") or display_name),
                "webViewLink": web_view_link,
                "thumbnailLink": str(uploaded.get("thumbnailLink", "")),
            }
            envelope = self._card_presenter.build_file_preview_card(
                fallback_text, [preview]
            )
            body = envelope["hostAppDataAction"]["chatDataAction"][
                "createMessageAction"
            ]["message"]
            self._post_message(space, thread, body, request_id=request_id)
        except Exception as error:  # noqa: BLE001
            return FileDeliveryResult(
                file_path=resolved_path,
                display_name=display_name,
                drive_file_id=drive_file_id,
                web_view_link=web_view_link,
                error=error,
            )

        return FileDeliveryResult(
            file_path=resolved_path,
            display_name=display_name,
            drive_file_id=drive_file_id,
            web_view_link=web_view_link,
        )
