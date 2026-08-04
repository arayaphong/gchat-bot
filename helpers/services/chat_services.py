from __future__ import annotations

import html
import io
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from helpers.chat_card_markdown_parser import (
    MAX_CARD_WIDGETS,
    markdown_to_gchat_text,
    markdown_to_gchat_widgets,
)


@dataclass(frozen=True)
class ChatAuthSettings:
    audiences: set[str]
    issuers: set[str]
    trusted_emails: set[str]
    service_email_re: re.Pattern[str] | None
    auth_debug: bool

    @staticmethod
    def from_env() -> ChatAuthSettings:
        project_number = os.environ.get("GCHAT_PROJECT_NUMBER")
        audience = os.environ.get("GCHAT_AUDIENCE", "").strip()
        audiences = set(filter(None, map(str.strip, audience.split(","))))
        if project_number:
            audiences.add(project_number)

        trusted_emails = set(
            filter(
                None,
                map(
                    str.strip,
                    os.environ.get("GCHAT_TRUSTED_EMAILS", "").split(","),
                ),
            )
        )
        if project_number:
            trusted_emails.add(
                f"service-{project_number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
            )

        service_email_re = (
            re.compile(
                rf"^service-{re.escape(project_number)}@gcp-sa-gsuiteaddons\.iam\.gserviceaccount\.com$"
            )
            if project_number
            else None
        )

        auth_debug = os.environ.get("FLASK_DEBUG", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        return ChatAuthSettings(
            audiences=audiences,
            issuers={"https://accounts.google.com", "accounts.google.com"},
            trusted_emails=trusted_emails,
            service_email_re=service_email_re,
            auth_debug=auth_debug,
        )


class ChatAuthVerifier:
    def __init__(self, settings: ChatAuthSettings):
        self.settings = settings

    def verify(self, req: Any) -> bool:
        if not self.settings.audiences:
            return False

        auth_header = req.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[len("Bearer ") :]

        try:
            claims = google_id_token.verify_oauth2_token(
                token, Request(), audience=list(self.settings.audiences)
            )
        except Exception:  # noqa: BLE001
            return False

        issuer = claims.get("iss")
        email = claims.get("email")
        issuer_ok = issuer in self.settings.issuers
        email_ok = bool(email) and (
            email in self.settings.trusted_emails
            or (
                bool(self.settings.service_email_re)
                and bool(self.settings.service_email_re.match(email))
            )
        )

        return issuer_ok and email_ok


class CredentialService:
    def __init__(
        self,
        bot_cred: Path,
        token_file: Path,
        scopes_bot: list[str],
        scopes_user: list[str],
    ):
        self.bot_cred = bot_cred
        self.token_file = token_file
        self.scopes_bot = scopes_bot
        self.scopes_user = scopes_user

    @staticmethod
    def _atomic_write_secret(path: Path | str, content: str) -> None:
        target = Path(path)
        d = target.parent
        tmp = d / f".{target.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, target)

    def get_user_creds(self) -> UserCreds:
        creds = UserCreds.from_authorized_user_file(
            str(self.token_file), self.scopes_user
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._atomic_write_secret(self.token_file, creds.to_json())
        return creds

    def get_bot_creds(self) -> service_account.Credentials:
        creds = service_account.Credentials.from_service_account_file(
            str(self.bot_cred), scopes=self.scopes_bot
        )
        creds.refresh(Request())
        return creds

    def get_bot_token(self) -> str:
        return self.get_bot_creds().token


GOOGLE_WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
GOOGLE_WORKSPACE_EXPORT_MIME_TYPE = "application/pdf"


class AttachmentService:
    def __init__(
        self,
        download_dir: Path,
        max_attachment_bytes: int,
        credential_service: CredentialService,
    ):
        self.download_dir = download_dir
        self.max_attachment_bytes = max_attachment_bytes
        self.credential_service = credential_service

    @staticmethod
    def _attachment_meta(att: dict[str, Any]) -> dict[str, Any]:
        uri = att.get("uri", "")
        try:
            fallback_name = Path(urlsplit(uri).path).name if uri else ""
        except ValueError:
            fallback_name = ""
        content_name = att.get("contentName") or fallback_name or "unknown"
        content_type = att.get("contentType") or (
            mimetypes.guess_type(content_name)[0] if fallback_name else ""
        ) or ""
        return {
            "contentName": content_name,
            "contentType": content_type,
            "size": att.get("size", ""),
            "driveFileId": att.get("driveDataRef", {}).get("driveFileId", ""),
            "resourceName": att.get("attachmentDataRef", {}).get("resourceName", ""),
            "uri": uri,
            "isSticker": bool(att.get("isSticker", False)),
        }

    def _unique_path(self, filename: str) -> Path:
        candidate = self.download_dir / filename
        if not candidate.exists():
            return candidate
        stem, suffix = Path(filename).stem, Path(filename).suffix
        return next(
            c
            for n in count(1)
            if not (c := self.download_dir / f"{stem}.{n}{suffix}").exists()
        )

    def _download_chunks(self, dl: MediaIoBaseDownload, fh: io.FileIO) -> bool:
        _, done = dl.next_chunk()
        return (
            True
            if fh.tell() > self.max_attachment_bytes
            else (False if done else self._download_chunks(dl, fh))
        )

    def _download_uri(self, uri: str, target_fp: Path) -> bool:
        with requests.get(uri, stream=True, timeout=15) as resp:
            resp.raise_for_status()
            written = 0
            with target_fp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    fh.write(chunk)
                    written += len(chunk)
                    if written > self.max_attachment_bytes:
                        return True
        return False

    @staticmethod
    def _target_filename(content_name: str, content_type: str) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", content_name)[:120]
        stem = Path(safe_name).stem or "attachment"
        mime_suffix = mimetypes.guess_extension(content_type, strict=False) or ""
        fallback_suffix = Path(safe_name).suffix
        suffix = mime_suffix or fallback_suffix
        return f"{stem}{suffix}" if suffix else stem

    def _download_one(
        self, att: dict[str, Any], drive: Any, chat_api: Any
    ) -> dict[str, Any]:
        meta = self._attachment_meta(att)
        target_fp: Path | None = None
        try:
            raw_size = meta.get("size")
            declared_size = (
                int(raw_size)
                if isinstance(raw_size, (int, float, str)) and str(raw_size).isdigit()
                else None
            )
            if declared_size is not None and declared_size > self.max_attachment_bytes:
                meta["errorCode"] = "too_large"
                meta["error"] = (
                    f"attachment exceeds {self.max_attachment_bytes} byte limit"
                )
                return {"fp": None, "meta": meta}

            ctype = meta["contentType"]
            is_native_workspace_file = ctype.startswith(GOOGLE_WORKSPACE_MIME_PREFIX)
            download_ctype = (
                GOOGLE_WORKSPACE_EXPORT_MIME_TYPE if is_native_workspace_file else ctype
            )
            target_filename = self._target_filename(meta["contentName"], download_ctype)
            target_fp = self._unique_path(target_filename)

            if "driveDataRef" in att:
                fid = meta["driveFileId"]
                # native Google Docs/Sheets/Slides have no raw binary content —
                # get_media() 404s on them; must export to a concrete format
                req = (
                    drive.files().export_media(
                        fileId=fid, mimeType=GOOGLE_WORKSPACE_EXPORT_MIME_TYPE
                    )
                    if is_native_workspace_file
                    else drive.files().get_media(fileId=fid)
                )
                with io.FileIO(target_fp, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, req)
                    too_big = self._download_chunks(dl, fh)
                if is_native_workspace_file:
                    meta["originalContentType"] = ctype
                    meta["contentType"] = GOOGLE_WORKSPACE_EXPORT_MIME_TYPE
            elif "attachmentDataRef" in att:
                req = chat_api.media().download_media(resourceName=meta["resourceName"])
                with io.FileIO(target_fp, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, req)
                    too_big = self._download_chunks(dl, fh)
            elif "uri" in att:
                too_big = self._download_uri(meta["uri"], target_fp)
            else:
                meta["errorCode"] = "unsupported_reference"
                meta["error"] = (
                    "attachment has neither driveDataRef, attachmentDataRef, nor uri"
                )
                return {"fp": None, "meta": meta}

            if too_big:
                if target_fp.exists():
                    try:
                        target_fp.unlink()
                    except OSError as cleanup_error:
                        meta["partialCleanupError"] = str(cleanup_error)
                        meta["cleanupPath"] = str(target_fp)
                meta["errorCode"] = "too_large"
                meta["error"] = (
                    f"attachment exceeds {self.max_attachment_bytes} byte limit"
                )
                return {"fp": None, "meta": meta}

            if target_fp.exists():
                meta["localPath"] = str(target_fp)
                meta["savedSize"] = target_fp.stat().st_size
                return {"fp": str(target_fp), "meta": meta}

            meta["errorCode"] = "missing_after_download"
            meta["error"] = "attachment download completed but file not found"
            return {"fp": None, "meta": meta}
        except Exception as e:  # noqa: BLE001
            if target_fp is not None and target_fp.exists():
                try:
                    target_fp.unlink()
                except OSError as cleanup_error:
                    meta["partialCleanupError"] = str(cleanup_error)
                    meta["cleanupPath"] = str(target_fp)
            return {
                "fp": None,
                "meta": {
                    **meta,
                    "errorCode": "download_failed",
                    "error": type(e).__name__,
                },
            }

    def download_with_meta(self, atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not atts:
            return []

        needs_drive = any("driveDataRef" in att for att in atts)
        needs_chat = any("attachmentDataRef" in att for att in atts)
        drive = (
            build("drive", "v3", credentials=self.credential_service.get_user_creds())
            if needs_drive
            else None
        )
        chat_api = (
            build("chat", "v1", credentials=self.credential_service.get_bot_creds())
            if needs_chat
            else None
        )
        return [self._download_one(att, drive, chat_api) for att in atts]

    @staticmethod
    def cleanup(files_with_meta: list[dict[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        removed = 0
        failed: list[dict[str, str]] = []

        for item in files_with_meta:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            fp = item.get("fp") or meta.get("cleanupPath")
            if not isinstance(fp, str) or not fp or fp in seen:
                continue
            seen.add(fp)
            path = Path(fp)
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except Exception as error:  # noqa: BLE001
                reason = (
                    (error.strerror or str(error))
                    if isinstance(error, OSError)
                    else type(error).__name__
                )
                failed.append({"name": path.name, "error": reason})

        return {"removed": removed, "failed": failed}


class CardPresenter:
    @staticmethod
    def _provider_label(provider: str) -> str:
        return "🛠️ ผู้ดูแลระบบ" if provider == "jinx_system" else "OpenClaw"

    def card_title(self, provider: str) -> str:
        return self._provider_label(provider)

    def build_card(
        self,
        text: str,
        provider: str = "openclaw",
    ) -> dict[str, Any]:
        try:
            widgets = markdown_to_gchat_widgets(text)
            if not widgets:
                widgets = [{"textParagraph": {"text": text[:4000]}}]
        except Exception as e:  # noqa: BLE001
            widgets = [
                {
                    "textParagraph": {
                        "text": f"{text[:3800]}<br><br><font color='#cc0000'>parse err: {e}</font>"
                    }
                }
            ]

        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {
                        "message": {
                            "cardsV2": [
                                {
                                    "cardId": "r",
                                    "card": {
                                        "header": {
                                            "title": self.card_title(provider),
                                        },
                                        "sections": [
                                            {"widgets": widgets[:MAX_CARD_WIDGETS]}
                                        ],
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }

    @staticmethod
    def build_text(text: str) -> dict[str, Any]:
        try:
            rendered = markdown_to_gchat_text(text)
        except Exception:  # noqa: BLE001
            rendered = text

        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {
                        "message": {"text": rendered}
                    }
                }
            }
        }

    @staticmethod
    def build_file_preview_card(text: str, files: list[dict[str, str]]) -> dict[str, Any]:
        file_widget_groups: list[list[dict[str, Any]]] = []
        for f in files:
            name = html.escape(f.get("name", ""), quote=True)
            url = f.get("webViewLink", "")
            thumbnail = f.get("thumbnailLink", "")
            group: list[dict[str, Any]] = []
            if thumbnail:
                group.append(
                    {"image": {"imageUrl": thumbnail, "onClick": {"openLink": {"url": url}}}}
                )
            group.append(
                {
                    "decoratedText": {
                        "text": f"📎 {name}",
                        "wrapText": True,
                        "button": {
                            "text": "เปิดไฟล์",
                            "onClick": {"openLink": {"url": url}},
                        },
                    }
                }
            )
            file_widget_groups.append(group)

        # include only whole per-file widget groups so a cutoff never
        # separates a file's image widget from its open-file button
        widgets: list[dict[str, Any]] = []
        included_count = 0
        for group in file_widget_groups:
            if len(widgets) + len(group) > MAX_CARD_WIDGETS:
                break
            widgets.extend(group)
            included_count += 1

        if included_count < len(file_widget_groups):
            print(
                f"[build_file_preview_card] dropping "
                f"{len(file_widget_groups) - included_count} file(s) "
                "to stay within MAX_CARD_WIDGETS"
            )

        message: dict[str, Any] = {
            "cardsV2": [
                {
                    "cardId": "file-preview",
                    "card": {"sections": [{"widgets": widgets}]},
                }
            ]
        }
        if text:
            try:
                message["text"] = markdown_to_gchat_text(text)
            except Exception:  # noqa: BLE001
                message["text"] = text

        return {
            "hostAppDataAction": {
                "chatDataAction": {"createMessageAction": {"message": message}}
            }
        }
