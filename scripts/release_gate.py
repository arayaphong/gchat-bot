"""Validate release identity and reproducibility invariants."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _requirements_are_pinned(path: Path) -> bool:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        if "==" not in line:
            return False
    return True


def validate_release(source_dir: Path, expected_sha: str | None) -> list[str]:
    failures: list[str] = []
    try:
        ZoneInfo("Asia/Bangkok")
    except ZoneInfoNotFoundError:
        failures.append("timezone_unavailable")
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = source_dir / name
        if not path.is_file() or not _requirements_are_pinned(path):
            failures.append(f"unpinned_{name.replace('-', '_').replace('.', '_')}")
    if expected_sha is not None:
        normalized = expected_sha.lower()
        if _SHA_RE.fullmatch(normalized) is None:
            failures.append("invalid_expected_sha")
        else:
            try:
                actual = (source_dir / "RELEASE_SHA").read_text(
                    encoding="ascii"
                ).strip()
            except (OSError, UnicodeError):
                actual = ""
            if actual != normalized:
                failures.append("release_sha_mismatch")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    failures = validate_release(args.source_dir.resolve(), args.expected_sha)
    print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
