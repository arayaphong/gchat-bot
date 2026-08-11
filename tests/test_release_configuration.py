from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.release_gate import validate_release

PROJECT_DIR = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_runtime_and_dev_direct_requirements_are_exactly_pinned(self) -> None:
        self.assertEqual(validate_release(PROJECT_DIR, None), [])

    def test_release_gate_binds_archive_to_expected_sha_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "requirements.txt").write_text("Flask==3.1.3\n")
            (source / "requirements-dev.txt").write_text(
                "-r requirements.txt\nruff==0.16.0\n"
            )
            (source / "RELEASE_SHA").write_text("a" * 40)

            self.assertEqual(validate_release(source, "a" * 40), [])
            self.assertEqual(
                validate_release(source, "b" * 40),
                ["release_sha_mismatch"],
            )

    def test_deployment_uses_exact_sha_and_never_mutable_development_archive(
        self,
    ) -> None:
        script = (PROJECT_DIR / "deploy.sh").read_text(encoding="utf-8")

        self.assertIn("^[0-9a-f]{40}$", script)
        self.assertIn("archive/$release_sha.tar.gz", script)
        self.assertIn("refs/heads/development", script)
        self.assertIn('release_sha" != "$development_sha', script)
        self.assertNotIn("rsync", script)
        self.assertIn("preflight --remote", script)
        self.assertIn("release_sha", script)

    def test_temporary_chat_history_deploy_requires_explicit_test_mode(self) -> None:
        script = (PROJECT_DIR / "deploy.sh").read_text(encoding="utf-8")

        self.assertIn("--test-google-chat-history", script)
        self.assertIn(
            "https://github.com/arayaphong/gchat-bot/archive/refs/heads/"
            "google-chat-history.zip",
            script,
        )
        self.assertIn("refs/heads/google-chat-history", script)
        self.assertIn("temporary non-production deploy", script)

    def test_production_topology_starts_background_threads_after_one_worker_fork(
        self,
    ) -> None:
        config = (PROJECT_DIR / "gunicorn.conf.py").read_text(encoding="utf-8")

        self.assertIn("workers = 1", config)
        self.assertIn("preload_app = True", config)
        self.assertIn("def post_fork", config)
        self.assertIn("start_runtime_services", config)

    def test_ci_covers_minimum_production_and_forward_python_versions(self) -> None:
        workflow = (PROJECT_DIR / ".github/workflows/quality.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('python-version: ["3.10", "3.12", "3.14"]', workflow)

    def test_mutable_runtime_paths_can_live_outside_immutable_release(self) -> None:
        source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")

        for variable in (
            "JINX_SESSION_KEY_FILE",
            "JINX_CHAT_IN_LOG_FILE",
            "JINX_CHAT_OUT_LOG_FILE",
        ):
            self.assertIn(variable, source)
