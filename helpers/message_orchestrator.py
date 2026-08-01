from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from helpers.chat_gateway import ChatGateway
from helpers.orchestrator_messages import (
    ABORT_FAILURE_TEMPLATE,
    ABORT_SUCCESS_TEXT,
    BUSY_TEXT,
    NEW_SESSION_TEXT,
)
from helpers.providers import ProviderSettings, ask_provider
from helpers.providers.openclaw_provider import NO_RESPONSE_TEXT
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
        self._commands: dict[str, Callable[[str, str], None]] = {
            "/abort": self._handle_abort,
            "/new": self._handle_new_session,
        }

    def dispatch(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        command = self._commands.get(text)
        if command:
            threading.Thread(
                target=command, args=(space, thread), daemon=True
            ).start()
            return

        print(f"✅ [chat-in] accepted request (space={space}, thread={thread})")

        if self._processing_lock.acquire(blocking=False):
            settings = self._session_manager.settings
            threading.Thread(
                target=self._handle_message,
                args=(space, thread, user, text, attachments, settings),
                daemon=True,
            ).start()
        else:
            print(f"🚫 [busy] rejecting concurrent request (space={space}, thread={thread})")
            self._gateway.send_followup(space, thread, BUSY_TEXT, "jinx_system")

    def _handle_message(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        settings: ProviderSettings,
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
            reply_text, provider_used = ask_provider(text, user, files, settings)

            if reply_text.strip() == NO_RESPONSE_TEXT:
                print(
                    f"⏭️ [openclaw-skip] no response, skipping reply "
                    f"(space={space}, thread={thread})"
                )
                return

            print(f"📤 [chat-out] delivering reply (space={space}, thread={thread})")
            self._gateway.send_followup(space, thread, reply_text, provider_used)
        except Exception as e:  # noqa: BLE001
            print(f"❌ [error] {e} (space={space}, thread={thread})")
            # router already formats the user-facing secretary message
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
