"""Daily-sync eligibility and a small success marker, without opening SQLite."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

STATE_VERSION = 1
STATE_FILENAME = "nightly-sync.json"


def boot_elapsed_seconds() -> float:
    if not hasattr(ctypes, "windll"):
        raise OSError("Windows uptime is unavailable")
    kernel = ctypes.windll.kernel32
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    return int(kernel.GetTickCount64()) / 1000


def _load_state(data_root: Path) -> dict[str, Any] | None:
    path = data_root / "config" / STATE_FILENAME
    try:
        if path.stat().st_size > 4096:
            raise ValueError("Invalid nightly-sync state")
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(state, dict)
        or state.get("version") != STATE_VERSION
        or not isinstance(state.get("weflow_checked"), bool)
        or not isinstance(state.get("covered_until_ms"), int)
        or isinstance(state.get("covered_until_ms"), bool)
        or state["covered_until_ms"] <= 0
    ):
        raise ValueError("Invalid nightly-sync state")
    return state


def latest_daily_boundary(now: datetime, daily_at: str) -> datetime:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_at):
        raise ValueError("Invalid daily schedule")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An aware local time is required")
    hour, minute = map(int, daily_at.split(":"))
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def daily_sync_decision(
    data_root: Path,
    *,
    now: datetime | None = None,
    uptime_seconds: float | None = None,
    daily_at: str = "00:00",
    boot_delay_minutes: int = 60,
    weflow_required: bool = True,
) -> dict[str, Any]:
    """No writes, dependency initialization, chat reading or legacy-success guesses."""
    current = now or datetime.now().astimezone()
    boundary_ms = int(latest_daily_boundary(current, daily_at).timestamp() * 1000)
    if not 1 <= boot_delay_minutes <= 240:
        raise ValueError("Invalid boot delay")
    state = _load_state(data_root)
    current_ms = int(current.timestamp() * 1000)
    if state is not None and (
        state["covered_until_ms"] >= boundary_ms
        and state["covered_until_ms"] <= current_ms
        and (not weflow_required or state["weflow_checked"])
    ):
        return {"status": "not-due", "delay_seconds": 0}
    uptime = boot_elapsed_seconds() if uptime_seconds is None else uptime_seconds
    if not math.isfinite(uptime) or uptime < 0:
        raise ValueError("Invalid Windows uptime")
    remaining = math.ceil(boot_delay_minutes * 60 - uptime)
    if remaining > 0:
        return {"status": "boot-delay", "delay_seconds": remaining}
    return {"status": "due", "delay_seconds": 0}


def record_daily_success(
    data_root: Path,
    *,
    run_id: str,
    started_at_ms: int,
    completed_at_ms: int,
    weflow_checked: bool,
) -> Path:
    if started_at_ms <= 0 or completed_at_ms < started_at_ms:
        raise ValueError("Invalid completion time")
    state = {
        "version": STATE_VERSION,
        "run_id": uuid.UUID(run_id).hex,
        # A run that crosses midnight must not hide the next day's check.
        "covered_until_ms": started_at_ms,
        "last_success_at_ms": completed_at_ms,
        "weflow_checked": bool(weflow_checked),
    }
    path = data_root / "config" / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(state, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only daily-sync eligibility")
    parser.add_argument("--daily-at", default="00:00")
    parser.add_argument("--boot-delay-minutes", type=int, default=60)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("PKAS_DATA_ROOT") or "data")
    try:
        result = daily_sync_decision(
            root,
            daily_at=args.daily_at,
            boot_delay_minutes=args.boot_delay_minutes,
            weflow_required=not args.local_only,
        )
    except Exception:
        print(json.dumps({"status": "failed", "code": "sync_schedule_failed"}))
        sys.exit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
