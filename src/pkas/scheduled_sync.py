import ctypes
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.sync_worker import run_sync
from pkas.system import KnowledgeSystem
from pkas.weflow_daily import run_daily_import

STATE_VERSION = 1
FULL_SYNC_INTERVAL_MS = 6 * 60 * 60 * 1000
BOOT_ID_GRANULARITY_MS = 60 * 1000

SyncRunner = Callable[[KnowledgeSystem], dict[str, Any]]


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def current_boot_id(*, now_ms: int | None = None) -> int:
    """Return a stable, non-secret identifier for the current Windows boot."""
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    if not hasattr(ctypes, "windll"):
        return 0
    uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
    boot_ms = max(0, current_ms - uptime_ms)
    return boot_ms // BOOT_ID_GRANULARITY_MS


def _state_path(knowledge_system: KnowledgeSystem) -> Path:
    return knowledge_system.settings.data_root / "config" / "scheduled-sync.json"


def _load_state(knowledge_system: KnowledgeSystem) -> dict[str, Any]:
    path = _state_path(knowledge_system)
    if not path.is_file():
        return {
            "version": STATE_VERSION,
            "last_success_at_ms": None,
            "last_boot_id": None,
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("Scheduled-sync state is invalid")
    return payload


def _save_state(knowledge_system: KnowledgeSystem, state: dict[str, Any]) -> Path:
    path = _state_path(knowledge_system)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def run_scheduled_sync(
    knowledge_system: KnowledgeSystem,
    *,
    now_ms: int | None = None,
    boot_id: int | None = None,
    full_sync_runner: SyncRunner = run_sync,
    weflow_import_runner: SyncRunner = run_daily_import,
) -> dict[str, Any]:
    """Import completed WeFlow exports and gate full refreshes to six hours."""
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    current_boot = current_boot_id(now_ms=current_ms) if boot_id is None else int(boot_id)
    state = _load_state(knowledge_system)
    last_success = int(state.get("last_success_at_ms") or 0)
    last_boot = state.get("last_boot_id")
    is_new_boot = current_boot > 0 and current_boot != last_boot
    interval_elapsed = last_success <= 0 or current_ms - last_success >= FULL_SYNC_INTERVAL_MS
    full_sync_due = is_new_boot or interval_elapsed

    # This local import is intentionally checked on every lightweight poll. It
    # does not call an LLM and only sees exports newer than the persisted
    # authorization watermark.
    weflow_result = weflow_import_runner(knowledge_system)
    full_result = full_sync_runner(knowledge_system) if full_sync_due else None

    weflow_status = str(weflow_result.get("status") or "failed")
    full_status = str(full_result.get("status") or "deferred") if full_result else "deferred"
    completed = weflow_status in {"completed", "disabled"} and (
        full_result is None or full_status == "completed"
    )
    warning = full_status == "warning" or int(weflow_result.get("failed_sessions") or 0) > 0
    failed = weflow_status == "failed" or full_status == "failed"

    if failed:
        status = "failed"
    elif warning:
        status = "warning"
    elif full_result is None:
        status = "deferred"
    else:
        status = "completed"

    if completed and full_result is not None:
        state["last_success_at_ms"] = current_ms
        if current_boot > 0:
            state["last_boot_id"] = current_boot
    state["last_attempt_at_ms"] = current_ms
    state["last_status"] = status
    state_path = _save_state(knowledge_system, state)
    effective_success = int(state.get("last_success_at_ms") or 0)

    return {
        "status": status,
        "reason": (
            "new-boot"
            if full_sync_due and is_new_boot
            else ("six-hours-elapsed" if full_sync_due else "not-due")
        ),
        "full_sync_due": full_sync_due,
        "full_sync": full_result,
        "weflow_import": weflow_result,
        "last_success_at_ms": effective_success or None,
        "next_full_sync_at_ms": (
            effective_success + FULL_SYNC_INTERVAL_MS if effective_success else None
        ),
        "state_path": str(state_path),
        "llm_used_for_poll_only": False,
    }
