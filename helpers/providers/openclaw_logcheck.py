from __future__ import annotations

import re
import subprocess
from datetime import datetime

JOURNAL_TIMEOUT_SECONDS = 10

# error patterns emitted by the gateway itself; successful runs log
# "isError=false" and are intentionally not matched here.
ERROR_PATTERN = re.compile(
    r"isError=true|\[model-fetch\] error|errorMessage|AbortError|timedOut=true",
    re.IGNORECASE,
)


def check_run_errors(run_id: str, since: datetime) -> list[str]:
    """Return journal lines for `run_id` that look like errors, since `since`.

    Returns an empty list when the run id is unknown or the journal cannot
    be read (e.g. journalctl missing or a different machine/user).
    """
    if not run_id:
        return []
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "openclaw-gateway",
                "--since",
                since.strftime("%Y-%m-%d %H:%M:%S"),
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=JOURNAL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[logcheck] journalctl unavailable: {e}")
        return []
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:500]
        print(
            f"[logcheck] journalctl failed (returncode={result.returncode}): "
            f"{detail}"
        )
        return []
    return [
        line
        for line in result.stdout.splitlines()
        if run_id in line and ERROR_PATTERN.search(line)
    ]
