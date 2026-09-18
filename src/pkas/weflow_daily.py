import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.system import KnowledgeSystem

STATE_VERSION = 1


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _state_path(knowledge_system: KnowledgeSystem) -> Path:
    return knowledge_system.settings.data_root / "config" / "weflow-daily-import.json"


def _load_state(knowledge_system: KnowledgeSystem) -> dict[str, Any] | None:
    path = _state_path(knowledge_system)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("WeFlow daily-import authorization state is invalid")
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


def authorize_daily_import(
    knowledge_system: KnowledgeSystem,
    *,
    records_path: str | None = None,
    authorized_at_ms: int | None = None,
) -> dict[str, Any]:
    resolved = Path(
        records_path or knowledge_system.settings.weflow_export_records_path
    ).expanduser()
    if not resolved.is_absolute():
        raise ValueError("WeFlow export-record path must be absolute")
    if not resolved.is_file():
        raise ValueError("WeFlow export-record file does not exist")
    cutoff = max(0, int(authorized_at_ms or _now_ms()))
    state = {
        "version": STATE_VERSION,
        "enabled": True,
        "privacy": "restricted",
        "records_path": str(resolved.resolve()),
        "authorized_at_ms": cutoff,
        "last_completed_export_time_ms": cutoff,
        "last_run_at_ms": None,
        "authorization_scope": "new WeFlow XLSX exports created after authorization",
    }
    state_path = _save_state(knowledge_system, state)
    return {
        "status": "authorized",
        "enabled": True,
        "privacy": "restricted",
        "authorized_at_ms": cutoff,
        "state_path": str(state_path),
        "api_required": False,
        "key_accessed": False,
    }


def disable_daily_import(knowledge_system: KnowledgeSystem) -> dict[str, Any]:
    state = _load_state(knowledge_system)
    if state is None:
        return {"status": "disabled", "enabled": False}
    state["enabled"] = False
    state["disabled_at_ms"] = _now_ms()
    state_path = _save_state(knowledge_system, state)
    return {"status": "disabled", "enabled": False, "state_path": str(state_path)}


def daily_import_is_enabled(knowledge_system: KnowledgeSystem) -> bool:
    """Return whether an explicit WeFlow daily-import authorization exists.

    The scheduled Windows runner uses this as a fail-closed preflight so it
    never launches WeFlow merely because a task was registered. No message
    content or secret fields are read here.
    """
    state = _load_state(knowledge_system)
    return bool(state and state.get("enabled"))


def run_daily_import(knowledge_system: KnowledgeSystem) -> dict[str, Any]:
    state = _load_state(knowledge_system)
    if state is None or not state.get("enabled"):
        return {
            "status": "disabled",
            "candidate_sessions": 0,
            "imported_messages": 0,
            "failed_sessions": 0,
        }

    records_path = str(state.get("records_path") or "").strip()
    if not records_path:
        raise ValueError("WeFlow daily-import authorization has no records path")

    catalog = knowledge_system.weflow.discover_exports(
        records_path=records_path,
        limit=1000,
    )
    cutoff = max(
        int(state.get("authorized_at_ms") or 0),
        int(state.get("last_completed_export_time_ms") or 0),
    )
    candidates = [
        item
        for item in catalog["items"]
        if int(item.get("export_time") or 0) > cutoff
        and int(item.get("byte_size") or 0) > 0
    ]
    now = _now_ms()
    if not candidates:
        state["last_run_at_ms"] = now
        _save_state(knowledge_system, state)
        return {
            "status": "completed",
            "discovered_sessions": int(catalog["existing_sessions"]),
            "candidate_sessions": 0,
            "inspected_sessions": 0,
            "imported_messages": 0,
            "duplicate_messages": 0,
            "failed_sessions": 0,
            "api_required": False,
            "key_accessed": False,
        }

    session_ids = [str(item["session_id"]) for item in candidates]
    inspection = knowledge_system.weflow.inspect_export_selection(
        records_path=records_path,
        session_ids=session_ids,
    )
    result = knowledge_system.customer_workflows.import_weflow_exports(
        records_path=records_path,
        session_ids=session_ids,
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )
    import_result = result.get("result") or {}
    failed_sessions = int(import_result.get("failed_sessions") or 0)
    status = str(result.get("status") or "failed")
    if status == "completed" and failed_sessions == 0:
        state["last_completed_export_time_ms"] = max(
            int(item.get("export_time") or 0) for item in candidates
        )
    state["last_run_at_ms"] = now
    state["last_status"] = status
    _save_state(knowledge_system, state)
    return {
        "status": status,
        "discovered_sessions": int(catalog["existing_sessions"]),
        "candidate_sessions": len(candidates),
        "inspected_sessions": int(inspection["selected_sessions"]),
        "imported_messages": int(import_result.get("imported") or 0),
        "duplicate_messages": int(import_result.get("duplicates") or 0),
        "failed_sessions": failed_sessions,
        "api_required": False,
        "key_accessed": False,
    }
