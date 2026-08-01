from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, body: dict[str, Any]) -> None:
    if not path.exists():
        path.touch()
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    line = json.dumps({"timeStamp": ts, "body": body}, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{line}\n")
