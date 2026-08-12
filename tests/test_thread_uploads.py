from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers.session_keys import ChatSessionContext
from helpers.thread_uploads import (
    THREAD_UPLOAD_DIRECTORY_PREFIX,
    thread_upload_directory,
    thread_upload_directory_name,
)


class ThreadUploadDirectoryTests(unittest.TestCase):
    def test_name_is_stable_safe_and_scoped_by_space_and_thread(self) -> None:
        first = ChatSessionContext.for_thread(
            "spaces/One",
            "spaces/One/threads/shared",
        )
        same_thread_id_elsewhere = ChatSessionContext.for_thread(
            "spaces/Two",
            "spaces/Two/threads/shared",
        )

        first_name = thread_upload_directory_name(first.session_key)
        self.assertEqual(
            first_name,
            thread_upload_directory_name(first.session_key),
        )
        self.assertTrue(first_name.startswith(THREAD_UPLOAD_DIRECTORY_PREFIX))
        self.assertEqual(len(first_name), len(THREAD_UPLOAD_DIRECTORY_PREFIX) + 64)
        self.assertRegex(first_name, r"^thread-[0-9a-f]{64}$")
        self.assertNotEqual(
            first_name,
            thread_upload_directory_name(same_thread_id_elsewhere.session_key),
        )

    def test_directory_is_a_direct_child_even_for_long_encoded_ids(self) -> None:
        context = ChatSessionContext.for_thread(
            f"spaces/{'A' * 70}",
            f"spaces/{'A' * 70}/threads/{'B' * 60}",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = thread_upload_directory(root, context.session_key)

        self.assertEqual(output.parent, root.resolve(strict=False))
        self.assertLessEqual(len(output.name.encode("utf-8")), 255)

    def test_invalid_or_legacy_session_key_is_rejected(self) -> None:
        for session_key in (
            "not-a-session",
            "agent:main:gchat:one:main",
            "agent:main:gchat:only-one-component",
        ):
            with self.subTest(session_key=session_key), self.assertRaises(ValueError):
                thread_upload_directory_name(session_key)


if __name__ == "__main__":
    unittest.main()
