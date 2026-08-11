from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.chat_gateway import ChatGateway
from helpers.chat_log_redaction import (
    MAX_DEPTH,
    REDACTED,
    UNSUPPORTED,
    redact_chat_log_value,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.jsonl_log import append_jsonl
from helpers.services import CardPresenter


class ChatLogRedactionTests(unittest.TestCase):
    def test_recursive_redaction_covers_incoming_and_outgoing_action_parameters(
        self,
    ) -> None:
        sentinel = "sentinel-history-handle"
        value = {
            "authorizationEventObject": {"userOAuthToken": sentinel},
            "commonEventObject": {
                "parameters": {
                    "historyActionHandle": sentinel,
                    "untrusted": sentinel,
                }
            },
            "cardsV2": [
                {
                    "action": {
                        "parameters": [
                            {"key": "historyActionHandle", "value": sentinel}
                        ]
                    }
                }
            ],
            "nested": {"access_token": sentinel, "safe": "visible"},
            "directHandles": {
                "confirmHandle": sentinel,
                "cancel_handle": sentinel,
                "opaqueActionHandle": sentinel,
            },
        }
        original = copy.deepcopy(value)

        redacted = redact_chat_log_value(value)
        serialized = json.dumps(redacted)

        self.assertNotIn(sentinel, serialized)
        self.assertEqual(value, original)
        self.assertEqual(redacted["authorizationEventObject"], REDACTED)
        self.assertEqual(redacted["commonEventObject"]["parameters"], REDACTED)
        self.assertEqual(redacted["nested"]["safe"], "visible")

    def test_parameter_keys_cannot_smuggle_a_handle_into_the_log(self) -> None:
        sentinel = "opaque-handle-used-as-a-key"
        redacted = redact_chat_log_value(
            {"commonEventObject": {"parameters": {sentinel: "anything"}}}
        )

        self.assertNotIn(sentinel, json.dumps(redacted))
        self.assertEqual(redacted["commonEventObject"]["parameters"], REDACTED)

    def test_unexpected_objects_are_not_stringified_or_mutated(self) -> None:
        class SecretObject:
            def __str__(self) -> str:
                raise AssertionError("redaction must not call __str__")

        secret_object = SecretObject()
        non_string_key = SecretObject()
        value = {
            "unexpected": secret_object,
            "binary": b"secret-binary-value",
            non_string_key: "secret-under-non-string-key",
        }

        redacted = redact_chat_log_value(value)

        self.assertIs(value["unexpected"], secret_object)
        self.assertEqual(redacted["unexpected"], UNSUPPORTED)
        self.assertEqual(redacted["binary"], UNSUPPORTED)
        self.assertEqual(redacted["[NON_STRING_KEY_2]"], UNSUPPORTED)
        self.assertNotIn("secret-binary-value", json.dumps(redacted))
        self.assertNotIn("secret-under-non-string-key", json.dumps(redacted))

    def test_recursive_cycle_stops_at_a_safe_depth(self) -> None:
        cyclic: dict[str, object] = {"refreshToken": "secret-token"}
        cyclic["self"] = cyclic

        redacted = redact_chat_log_value(cyclic)
        serialized = json.dumps(redacted)

        self.assertNotIn("secret-token", serialized)
        self.assertIn(MAX_DEPTH, serialized)

    def test_gateway_logs_only_redacted_copies_without_mutating_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = ChatGateway(
                credential_service=Mock(),
                card_presenter=CardPresenter(),
                chat_in_log=root / "chat-in.jsonl",
                chat_out_log=root / "chat-out.jsonl",
                file_policy=SendableFilePolicy([root]),
                drive_folder_id="drive-folder",
            )
            sentinel = "sentinel-confirm-handle"
            incoming = {
                "commonEventObject": {"parameters": {"historyActionHandle": sentinel}}
            }
            outgoing = {
                "action": {
                    "parameters": [{"key": "historyActionHandle", "value": sentinel}]
                }
            }
            original_incoming = copy.deepcopy(incoming)
            original_outgoing = copy.deepcopy(outgoing)

            gateway.record_incoming(incoming)
            gateway.record_outgoing(outgoing)

            self.assertEqual(incoming, original_incoming)
            self.assertEqual(outgoing, original_outgoing)
            self.assertEqual(
                incoming["commonEventObject"]["parameters"]["historyActionHandle"],
                sentinel,
            )
            self.assertNotIn(
                sentinel, (root / "chat-in.jsonl").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                sentinel, (root / "chat-out.jsonl").read_text(encoding="utf-8")
            )

    def test_gateway_reports_only_exception_type_when_logging_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = ChatGateway(
                credential_service=Mock(),
                card_presenter=CardPresenter(),
                chat_in_log=root / "chat-in.jsonl",
                chat_out_log=root / "chat-out.jsonl",
                file_policy=SendableFilePolicy([root]),
                drive_folder_id="drive-folder",
            )
            sentinel = "secret-from-logging-exception"

            with (
                patch(
                    "helpers.chat_gateway.append_jsonl",
                    side_effect=OSError(sentinel),
                ),
                patch("builtins.print") as print_message,
            ):
                sent = gateway.send_followup(
                    "spaces/one",
                    "spaces/one/threads/two",
                    "safe text",
                    "jinx_system",
                )

        self.assertFalse(sent)
        rendered = " ".join(str(argument) for argument in print_message.call_args.args)
        self.assertIn("OSError", rendered)
        self.assertNotIn(sentinel, rendered)


class JsonlLogPermissionTests(unittest.TestCase):
    def test_append_creates_a_new_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.jsonl"

            append_jsonl(path, {"safe": True})

            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_append_creates_and_repairs_private_log_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.jsonl"
            path.write_text("", encoding="utf-8")
            path.chmod(0o644)

            append_jsonl(path, {"safe": True})

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["body"], {"safe": True})

    def test_append_refuses_a_symlink_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.jsonl"
            target.write_text("unchanged", encoding="utf-8")
            link = root / "chat.jsonl"
            link.symlink_to(target)

            with self.assertRaises(OSError):
                append_jsonl(link, {"must_not": "write"})

            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_append_refuses_a_symlink_in_the_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_directory = root / "target"
            target_directory.mkdir()
            linked_directory = root / "linked"
            linked_directory.symlink_to(target_directory, target_is_directory=True)
            target = target_directory / "chat.jsonl"

            with self.assertRaises(OSError):
                append_jsonl(linked_directory / target.name, {"must_not": "write"})

            self.assertFalse(target.exists())

    def test_append_refuses_non_regular_paths_without_leaking_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = "secret-body-value"

            with self.assertRaises(OSError) as raised:
                append_jsonl(root, {"secret": sentinel})

            self.assertNotIn(sentinel, str(raised.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_append_refuses_a_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "chat.jsonl"
            os.mkfifo(fifo)

            with self.assertRaises(OSError):
                append_jsonl(fifo, {"must_not": "write"})


if __name__ == "__main__":
    unittest.main()
