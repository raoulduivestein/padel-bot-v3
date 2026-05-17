from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import STATE_DIR


RUN_HISTORY_PATH = STATE_DIR / "run_history.jsonl"


def append_run_history(
    *,
    source: str,
    attempts: int,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "attempts": attempts,
        "ok": bool(result.get("ok")) if result is not None else False,
        "result": result,
    }
    if error is not None:
        entry["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")

    return entry


def read_run_history(*, limit: int = 50) -> list[dict[str, Any]]:
    if not RUN_HISTORY_PATH.exists():
        return []

    lines = RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append(
                {
                    "timestamp": None,
                    "source": "history",
                    "ok": False,
                    "error": {"type": "JSONDecodeError", "message": "Invalid history line"},
                }
            )
    entries.reverse()
    return entries
