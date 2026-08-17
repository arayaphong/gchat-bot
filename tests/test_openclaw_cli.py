"""Tests for openclaw binary resolution and CLI invocation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers.providers import openclaw_cli


class VersionKeyTests(unittest.TestCase):
    def _key_for(self, dirname: str) -> tuple[int, ...]:
        return openclaw_cli._version_key(
            Path(f"/x/.nvm/versions/node/{dirname}/bin/openclaw")
        )

    def test_plain_releases_sort_numerically(self) -> None:
        self.assertLess(self._key_for("v9.0.0"), self._key_for("v10.0.0"))

    def test_prerelease_without_dot_sorts_below_matching_release(self) -> None:
        self.assertLess(self._key_for("v18.0.0-rc1"), self._key_for("v18.0.0"))

    def test_prerelease_with_dotted_suffix_sorts_below_matching_release(self) -> None:
        self.assertLess(self._key_for("v20.0.0-rc.1"), self._key_for("v20.0.0"))


class ResolveBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        openclaw_cli._resolve_binary.cache_clear()
        self._original_override = os.environ.pop(openclaw_cli.OPENCLAW_CLI_ENV, None)

    def tearDown(self) -> None:
        openclaw_cli._resolve_binary.cache_clear()
        if self._original_override is not None:
            os.environ[openclaw_cli.OPENCLAW_CLI_ENV] = self._original_override
        else:
            os.environ.pop(openclaw_cli.OPENCLAW_CLI_ENV, None)

    def test_override_env_is_expanded_before_use(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/home/tester",
                openclaw_cli.OPENCLAW_CLI_ENV: "~/bin/openclaw",
            },
        ):
            self.assertEqual(
                openclaw_cli._resolve_binary(), "/home/tester/bin/openclaw"
            )

    def test_which_hit_is_preferred_over_nvm_glob(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/openclaw"):
            self.assertEqual(openclaw_cli._resolve_binary(), "/usr/local/bin/openclaw")

    def test_nvm_glob_picks_newest_version(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            for version in ("v18.0.0", "v20.0.0", "v18.20.4"):
                bin_dir = home_path / ".nvm" / "versions" / "node" / version / "bin"
                bin_dir.mkdir(parents=True)
                (bin_dir / "openclaw").write_text("#!/usr/bin/env node\n")
            with (
                patch("shutil.which", return_value=None),
                patch.object(Path, "home", return_value=home_path),
            ):
                resolved = openclaw_cli._resolve_binary()
            self.assertEqual(
                resolved,
                str(home_path / ".nvm/versions/node/v20.0.0/bin/openclaw"),
            )

    def test_missing_home_falls_back_to_bare_name(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch.object(
                Path,
                "home",
                side_effect=RuntimeError("Could not determine home directory."),
            ),
        ):
            self.assertEqual(openclaw_cli._resolve_binary(), "openclaw")

    def test_no_nvm_candidates_falls_back_to_bare_name(self) -> None:
        with (
            tempfile.TemporaryDirectory() as home,
            patch("shutil.which", return_value=None),
            patch.object(Path, "home", return_value=Path(home)),
        ):
            self.assertEqual(openclaw_cli._resolve_binary(), "openclaw")

    def test_result_is_cached_across_calls(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/openclaw") as which:
            openclaw_cli._resolve_binary()
            openclaw_cli._resolve_binary()
        which.assert_called_once()


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        openclaw_cli._resolve_binary.cache_clear()

    def tearDown(self) -> None:
        openclaw_cli._resolve_binary.cache_clear()

    def test_empty_path_does_not_get_a_trailing_separator(self) -> None:
        with (
            patch.object(
                openclaw_cli, "_resolve_binary", return_value="/opt/node/bin/openclaw"
            ),
            patch.dict(os.environ, {"PATH": ""}),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            openclaw_cli._run(["models", "list"])
        used_env = run.call_args.kwargs["env"]
        self.assertEqual(used_env["PATH"], "/opt/node/bin")

    def test_existing_path_is_prefixed_with_the_binary_dir(self) -> None:
        with (
            patch.object(
                openclaw_cli, "_resolve_binary", return_value="/opt/node/bin/openclaw"
            ),
            patch.dict(os.environ, {"PATH": "/usr/bin"}),
            patch("subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            openclaw_cli._run(["models", "list"])
        used_env = run.call_args.kwargs["env"]
        self.assertEqual(used_env["PATH"], f"/opt/node/bin{os.pathsep}/usr/bin")

    def test_is_a_directory_error_is_translated_to_file_not_found(self) -> None:
        with (
            patch.object(
                openclaw_cli, "_resolve_binary", return_value="/opt/node/bin"
            ),
            patch("subprocess.run", side_effect=IsADirectoryError("is a directory")),
            self.assertRaises(FileNotFoundError),
        ):
            openclaw_cli._run(["models", "list"])

    def test_permission_error_is_translated_to_file_not_found(self) -> None:
        with (
            patch.object(
                openclaw_cli, "_resolve_binary", return_value="/opt/node/bin/openclaw"
            ),
            patch("subprocess.run", side_effect=PermissionError("denied")),
            self.assertRaises(FileNotFoundError),
        ):
            openclaw_cli._run(["models", "list"])


class CommandArgumentsTests(unittest.TestCase):
    def test_session_listing_targets_the_complete_main_agent_store(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with patch.object(openclaw_cli, "_run", return_value=completed) as run:
            result = openclaw_cli.list_sessions()

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["sessions", "list", "--agent", "main", "--json", "--limit", "all"]
        )

    def test_abort_clears_queued_follow_up_and_lane_turns(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with patch.object(openclaw_cli, "_run", return_value=completed) as run:
            result = openclaw_cli.abort_session("agent:main:gchat:one:root")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.abort",
                "--json",
                "--params",
                json.dumps({"key": "agent:main:gchat:one:root"}),
            ]
        )

if __name__ == "__main__":
    unittest.main()
