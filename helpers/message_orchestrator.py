from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from helpers.chat_gateway import ChatGateway
from helpers.orchestrator_messages import (
    ABORT_FAILURE_TEMPLATE,
    ABORT_SUCCESS_TEXT,
    BUSY_TEXT,
    MODELS_FAILURE_TEMPLATE,
    NEW_SESSION_TEXT,
    format_models_summary,
)
from helpers.providers import ProviderSettings, ask_provider
from helpers.providers.openclaw_cli import list_models as list_models_cli
from helpers.services import AttachmentService
from helpers.session_manager import SessionManager


class MessageOrchestrator:
    def __init__(
        self,
        gateway: ChatGateway,
        session_manager: SessionManager,
        attachment_service: AttachmentService,
    ) -> None:
        self._gateway = gateway
        self._session_manager = session_manager
        self._attachment_service = attachment_service
        self._processing_lock = threading.Lock()
        self._bypass_commands: dict[str, Callable[[str, str], None]] = {
            "/abort": self._handle_abort,
            "/models": self._handle_models,
            "/new": self._handle_new_session,
        }
        self._locked_commands: dict[str, Callable[[str, str], None]] = {}

    def dispatch(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        quoted_message: dict[str, str] | None = None,
    ) -> None:
        bypass_command = self._bypass_commands.get(text)
        if bypass_command:
            threading.Thread(
                target=bypass_command, args=(space, thread), daemon=True
            ).start()
            return

        print(f"✅ [chat-in] accepted request (space={space}, thread={thread})")

        if not self._processing_lock.acquire(blocking=False):
            print(f"🚫 [busy] rejecting concurrent request (space={space}, thread={thread})")
            self._gateway.send_followup(space, thread, BUSY_TEXT, "jinx_system")
            return

        locked_command = self._locked_commands.get(text)
        if locked_command:
            threading.Thread(
                target=locked_command, args=(space, thread), daemon=True
            ).start()
            return

        settings = self._session_manager.settings
        threading.Thread(
            target=self._handle_message,
            args=(space, thread, user, text, attachments, settings, quoted_message),
            daemon=True,
        ).start()

    def _handle_message(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        settings: ProviderSettings,
        quoted_message: dict[str, str] | None = None,
    ) -> None:
        try:
            files: list[dict[str, Any]] = []
            if attachments:
                print(
                    f"📎 [attachment-in] downloading {len(attachments)} file(s) "
                    f"(space={space}, thread={thread})"
                )
                files = self._attachment_service.download_with_meta(attachments)

            print(f"🤖 [openclaw-out] sending request (space={space}, thread={thread})")
            reply_text, provider_used, reply_files = ask_provider(
                text, user, files, settings, quoted_message
            )

            if reply_files:
                print(f"📎 [attachment-out] detected {len(reply_files)} file(s) to send (space={space})")
                for rf in reply_files:
                    print(f"   -> {rf}")
                # ส่งไฟล์ทั้งหมด ถ้ามีข้อความด้วยจะส่งข้อความก่อนแล้วตามด้วยไฟล์
                self._gateway.send_files(space, thread, reply_files, fallback_text=reply_text, )
                print(f"✅ [chat-out-files] delivered {len(reply_files)} file(s) (space={space}, thread={thread})")
            else:
                print(f"📤 [chat-out] delivering reply (space={space}, thread={thread})")
                self._gateway.send_followup(space, thread, reply_text, provider_used)

        except Exception as e:  # noqa: BLE001
            print(f"❌ [error] {e} (space={space}, thread={thread})")
            self._gateway.send_followup(space, thread, str(e), "jinx_system")
        finally:
            self._processing_lock.release()

    def _handle_abort(self, space: str, thread: str) -> None:
        print(f"🛑 [abort] triggering session abort (space={space}, thread={thread})")
        ok, reason = self._session_manager.abort_current(space, thread)
        if ok:
            self._gateway.send_followup(space, thread, ABORT_SUCCESS_TEXT, "jinx_system")
        else:
            self._gateway.send_followup(
                space, thread, ABORT_FAILURE_TEMPLATE.format(reason=reason), "jinx_system"
            )

    def _handle_new_session(self, space: str, thread: str) -> None:
        print(f"🆕 [new-session] triggering session reset (space={space}, thread={thread})")
        self._session_manager.abort_current(space, thread)
        self._session_manager.rotate()
        print(f"🆕 [new-session] session reset (space={space}, thread={thread})")
        self._gateway.send_followup(space, thread, NEW_SESSION_TEXT, "jinx_system")

    def _handle_models(self, space: str, thread: str) -> None:
        print(f"📚 [models] listing configured models (space={space}, thread={thread})")
        try:
            result = list_models_cli()
            if result.returncode != 0:
                detail = " ".join((result.stderr or result.stdout or "").split())[:500]
                reason = f"openclaw คืนค่ารหัส {result.returncode}"
                if detail:
                    reason = f"{reason}: {detail}"
                raise RuntimeError(reason)

            raw_output = result.stdout.lstrip("\ufeff").strip()
            if not raw_output:
                raise ValueError("openclaw ไม่ส่งข้อมูลกลับมา")

            payload = json.loads(raw_output)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("models"), list
            ):
                raise TypeError("รูปแบบข้อมูลจาก openclaw ไม่ถูกต้อง")

            models = payload["models"]
            summary = format_models_summary(models)
            print(
                f"✅ [models] found {len(models)} model(s) "
                f"(space={space}, thread={thread})"
            )
            self._gateway.send_followup(space, thread, summary, "jinx_system")
        except FileNotFoundError:
            reason = "ไม่พบคำสั่ง openclaw"
            print(f"❌ [models] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=reason),
                "jinx_system",
            )
        except subprocess.TimeoutExpired:
            reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            print(f"❌ [models] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=reason),
                "jinx_system",
            )
        except Exception as e:  # noqa: BLE001
            print(f"❌ [models] {e} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=e),
                "jinx_system",
            )
