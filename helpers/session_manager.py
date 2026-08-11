from __future__ import annotations

import threading

from helpers.providers.openclaw_client import OpenClawClient
from helpers.session_keys import ChatSessionContext


class SessionManager:
    """Operate on explicit deterministic sessions without global rotation state."""

    def __init__(self, *, openclaw_client: OpenClawClient) -> None:
        self._openclaw_client = openclaw_client
        self._lock = threading.RLock()

    @staticmethod
    def _session_key(session_key: str) -> str:
        return ChatSessionContext.from_session_key(session_key).session_key

    @staticmethod
    def _model(model: str) -> str:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        return model.strip()

    def create_with_model(self, session_key: str, model: str) -> str:
        """Create one exact deterministic session with its initial model."""

        normalized_key = self._session_key(session_key)
        selected_model = self._model(model)
        with self._lock:
            return self._openclaw_client.create_session(
                normalized_key,
                selected_model,
            )

    def ensure_with_model(self, session_key: str, model: str) -> str:
        """Idempotently create a deterministic session without rewriting it.

        An existing session keeps its current history and model. If another
        process wins the create race, the newly visible exact key is accepted.
        """

        normalized_key = self._session_key(session_key)
        selected_model = self._model(model)
        with self._lock:
            if self._openclaw_client.has_session(normalized_key):
                return normalized_key
            try:
                return self._openclaw_client.create_session(
                    normalized_key,
                    selected_model,
                )
            except Exception:
                if self._openclaw_client.has_session(normalized_key):
                    return normalized_key
                raise

    def set_model(self, session_key: str, model: str) -> str:
        """Set a model on the same key, creating the exact key if necessary."""

        normalized_key = self._session_key(session_key)
        selected_model = self._model(model)
        with self._lock:
            if self._openclaw_client.has_session(normalized_key):
                return self._openclaw_client.patch_session_model(
                    normalized_key,
                    selected_model,
                )
            try:
                return self._openclaw_client.create_session(
                    normalized_key,
                    selected_model,
                )
            except Exception:
                if self._openclaw_client.has_session(normalized_key):
                    return self._openclaw_client.patch_session_model(
                        normalized_key,
                        selected_model,
                    )
                raise

    def reset(self, session_key: str, model: str) -> str:
        """Start fresh history on one exact deterministic session.

        OpenClaw cannot reset a key that has never existed. In that case the
        exact key is created with the already-resolved effective model instead.
        """

        normalized_key = self._session_key(session_key)
        selected_model = self._model(model)
        with self._lock:
            if self._openclaw_client.has_session(normalized_key):
                return self._openclaw_client.reset_session(normalized_key)
            try:
                return self._openclaw_client.create_session(
                    normalized_key,
                    selected_model,
                )
            except Exception:
                if self._openclaw_client.has_session(normalized_key):
                    return self._openclaw_client.reset_session(normalized_key)
                raise

    def abort(
        self,
        session_key: str,
        *,
        space: str = "",
        thread: str = "",
    ) -> tuple[bool, str]:
        normalized_key = self._session_key(session_key)
        try:
            result = self._openclaw_client.abort_session(normalized_key)
            if result.ok:
                return True, ""
            return False, result.reason
        except Exception as error:  # noqa: BLE001
            print(
                f"❌ [error] abort failed: {error} "
                f"(session={normalized_key}, space={space}, thread={thread})"
            )
            return False, str(error)


__all__ = ["SessionManager"]
