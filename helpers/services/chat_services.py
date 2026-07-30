from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from helpers.chat_card_markdown_parser import (
    MAX_CARD_WIDGETS,
    markdown_to_gchat_widgets,
)

log = logging.getLogger(__name__)


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

        auth_debug = os.environ.get("GCHAT_AUTH_DEBUG", "").lower() in {
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
            log.error(
                "No chat audience configured; set GCHAT_AUDIENCE (preferred) or GCHAT_PROJECT_NUMBER"
            )
            return False

        auth_header = req.headers.get("Authorization", "")
        if self.settings.auth_debug:
            log.info("Auth header present=%s", bool(auth_header))

        if not auth_header.startswith("Bearer "):
            if self.settings.auth_debug:
                log.warning("Authorization header missing or not Bearer")
            return False

        token = auth_header[len("Bearer ") :]
        if self.settings.auth_debug and not token:
            log.warning("Bearer token is empty")

        try:
            claims = google_id_token.verify_oauth2_token(
                token, Request(), audience=list(self.settings.audiences)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Chat token verification failed: %s", e)
            return False

        if self.settings.auth_debug:
            log.info(
                "Chat auth claims: iss=%s email=%s aud=%s",
                claims.get("iss"),
                claims.get("email"),
                claims.get("aud"),
            )

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

        if self.settings.auth_debug and not (issuer_ok and email_ok):
            log.warning(
                "Issuer/email mismatch: allowed_issuers=%s got_iss=%s trusted_emails=%s got_email=%s",
                sorted(self.settings.issuers),
                issuer,
                sorted(self.settings.trusted_emails),
                email,
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

    def get_bot_token(self) -> str:
        creds = service_account.Credentials.from_service_account_file(
            str(self.bot_cred), scopes=self.scopes_bot
        )
        creds.refresh(Request())
        return creds.token


class AttachmentService:
    def __init__(
        self,
        upload_dir: Path,
        max_attachment_bytes: int,
        credential_service: CredentialService,
    ):
        self.upload_dir = upload_dir
        self.max_attachment_bytes = max_attachment_bytes
        self.credential_service = credential_service

    @staticmethod
    def _attachment_meta(att: dict[str, Any]) -> dict[str, Any]:
        return {
            "contentName": att.get("contentName", "unknown"),
            "contentType": att.get("contentType", ""),
            "size": att.get("size", ""),
            "driveFileId": att.get("driveDataRef", {}).get("driveFileId", ""),
        }

    def _download_chunks(self, dl: MediaIoBaseDownload, fh: io.FileIO) -> bool:
        _, done = dl.next_chunk()
        return (
            True
            if fh.tell() > self.max_attachment_bytes
            else (False if done else self._download_chunks(dl, fh))
        )

    def _download_one(
        self, att: dict[str, Any], drive: Any
    ) -> dict[str, Any]:
        meta = self._attachment_meta(att)
        try:
            safe = re.sub(r"[^a-zA-Z0-9._-]", "_", meta["contentName"])[:120]
            unique = (
                re.sub(r"[^a-zA-Z0-9]", "_", meta["driveFileId"]) or uuid.uuid4().hex
            )
            fp = self.upload_dir / f"{unique}_{safe}"
            if "driveDataRef" not in att:
                meta["error"] = (
                    "attachment has no driveDataRef; skipping non-Drive attachment"
                )
                return {"fp": None, "meta": meta}

            fid = meta["driveFileId"]
            ctype = meta["contentType"]
            req, target_fp = (
                (
                    drive.files().export_media(
                        fileId=fid,
                        mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                    Path(f"{fp}.xlsx"),
                )
                if "spreadsheet" in ctype or "ritz" in ctype
                else (drive.files().get_media(fileId=fid), fp)
            )

            with io.FileIO(target_fp, "wb") as fh:
                dl = MediaIoBaseDownload(fh, req)
                too_big = self._download_chunks(dl, fh)

            if too_big:
                if target_fp.exists():
                    target_fp.unlink()
                meta["error"] = (
                    f"attachment exceeds {self.max_attachment_bytes} byte limit"
                )
                return {"fp": None, "meta": meta}

            if target_fp.exists():
                meta["localPath"] = str(target_fp)
                meta["savedSize"] = target_fp.stat().st_size
                return {"fp": str(target_fp), "meta": meta}

            meta["error"] = "attachment download completed but file not found"
            return {"fp": None, "meta": meta}
        except Exception as e:  # noqa: BLE001
            return {"fp": None, "meta": {**meta, "error": str(e)}}

    def download_with_meta(self, atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not atts:
            return []

        creds = self.credential_service.get_user_creds()
        drive = build("drive", "v3", credentials=creds)
        return [self._download_one(att, drive) for att in atts]

    @staticmethod
    def cleanup(files_with_meta: list[dict[str, Any]]) -> None:
        seen = set()

        def cleanup_items(items: list[dict[str, Any]]) -> None:
            if not items:
                return
            item = items[0]
            rest = items[1:]
            fp = item.get("fp")
            if not fp or fp in seen:
                cleanup_items(rest)
                return
            seen.add(fp)
            try:
                p = Path(fp)
                if p.exists():
                    p.unlink()
            except Exception as e:  # noqa: BLE001
                log.warning("cleanup failed for %s: %s", fp, e)
            cleanup_items(rest)

        cleanup_items(files_with_meta)


class BalanceService:
    def __init__(self, api_url: str, cache_ttl_seconds: int, auth_debug: bool):
        self.api_url = api_url
        self.cache_ttl_seconds = max(cache_ttl_seconds, 1)
        self.auth_debug = auth_debug
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {"at": 0.0, "value": None}

    @staticmethod
    def _as_float_or_none(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def get_cached(self) -> dict[str, float] | None:
        api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
        if not api_key:
            return None

        now = time.time()
        with self._lock:
            cached_at = float(self._cache.get("at", 0.0) or 0.0)
            cached_value = self._cache.get("value")
            if cached_value and (now - cached_at) < self.cache_ttl_seconds:
                return cached_value

        try:
            resp = requests.get(
                self.api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=8,
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", {}) if isinstance(body, dict) else {}
            parsed = {
                "available_balance": self._as_float_or_none(
                    data.get("available_balance")
                )
                or 0.0,
                "voucher_balance": self._as_float_or_none(data.get("voucher_balance"))
                or 0.0,
                "cash_balance": self._as_float_or_none(data.get("cash_balance")) or 0.0,
            }
            with self._lock:
                self._cache["at"] = now
                self._cache["value"] = parsed
            if self.auth_debug:
                log.info(
                    "Moonshot balance: available=%s voucher=%s cash=%s",
                    parsed["available_balance"],
                    parsed["voucher_balance"],
                    parsed["cash_balance"],
                )
            return parsed
        except Exception as e:  # noqa: BLE001
            log.warning("Balance API call failed: %s", e)
            return None


class CardPresenter:
    @staticmethod
    def _balance_subtitle(balance: dict[str, float] | None) -> str:
        if not balance:
            return ""
        available = max(float(balance.get("available_balance", 0.0)), 0.0)
        return f"คงเหลือ ${available:.2f}"

    @staticmethod
    def _provider_label(provider: str) -> str:
        return "OpenClaw" if provider == "openclaw" else "Kimi K3"

    def card_title(self, provider: str, balance: dict[str, float] | None) -> str:
        subtitle = self._balance_subtitle(balance)
        base = f"{self._provider_label(provider)}"
        return base if not subtitle else f"{base} | {subtitle}"

    def build_card(
        self,
        text: str,
        balance: dict[str, float] | None = None,
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
                                            "title": self.card_title(provider, balance),
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
