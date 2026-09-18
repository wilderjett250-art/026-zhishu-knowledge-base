from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pkas.customer_repository import CustomerRepository
from pkas.customer_workflows import CustomerWorkflowService
from pkas.db import Database
from pkas.weflow import WeFlowService

STATE_VERSION = 1
RUNNING_STATUSES = {"queued", "preparing", "exporting", "importing"}
EXPORT_TOKEN_LIMIT = 1000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def export_record_token(item: dict[str, Any]) -> str:
    """Create a stable, non-content fingerprint for one export record."""
    payload = "\0".join(
        str(item.get(key) or "")
        for key in ("session_id", "export_time", "byte_size", "message_count")
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def normalized_export_tokens(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item
        for item in value
        if isinstance(item, str)
        and len(item) == 64
        and all(char in "0123456789abcdef" for char in item)
    }


def retained_export_tokens(tokens: set[str]) -> list[str]:
    return sorted(tokens)[-EXPORT_TOKEN_LIMIT:]


class WeFlowManualSyncService:
    """One-click, user-triggered WeFlow export and incremental import control plane."""

    def __init__(
        self,
        *,
        database: Database,
        customers: CustomerRepository,
        weflow: WeFlowService,
        customer_workflows: CustomerWorkflowService,
    ) -> None:
        self.database = database
        self.settings = database.settings
        self.customers = customers
        self.weflow = weflow
        self.customer_workflows = customer_workflows
        self.config_path = (
            self.settings.data_root / "config" / "weflow-manual-sync.json"
        )
        self.state_path = (
            self.settings.data_root / "runtime" / "weflow-manual-sync-state.json"
        )

    def status(self) -> dict[str, Any]:
        scope = self.sync_scope()
        state = _read_json(self.state_path) or {
            "version": STATE_VERSION,
            "status": "idle",
        }
        config = _read_json(self.config_path) or {}
        records_path = Path(
            str(config.get("records_path") or self.settings.weflow_export_records_path)
        ).expanduser()
        export_records = self._export_records_status(
            records_path=records_path,
            session_ids=set(scope["session_ids"]),
            export_watermark=int(state.get("export_watermark") or 0),
            export_tokens=normalized_export_tokens(state.get("export_record_tokens")),
        )
        return {
            "mode": "manual_only",
            "status": str(state.get("status") or "idle"),
            "job_id": state.get("job_id"),
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "stage": state.get("stage"),
            "summary_code": state.get("summary_code"),
            "imported_messages": int(state.get("imported_messages") or 0),
            "duplicate_messages": int(state.get("duplicate_messages") or 0),
            "failed_sessions": int(state.get("failed_sessions") or 0),
            "exported_sessions": int(state.get("exported_sessions") or 0),
            "no_data_sessions": int(state.get("no_data_sessions") or 0),
            "export_failed_sessions": int(
                state.get("export_failed_sessions") or 0
            ),
            "conversation_count": scope["conversation_count"],
            "latest_message_timestamp": scope["latest_message_timestamp"],
            "latest_message_at": scope["latest_message_at"],
            "last_import_at": scope["last_import_at"],
            "records_available": records_path.is_file(),
            "export_freshness": export_records["freshness"],
            "latest_export_at": export_records["latest_export_at"],
            "usable_export_count": export_records["usable_export_count"],
            "weflow_configured": self._configured_weflow_root(config) is not None,
            "scheduled": False,
            "autostart": False,
        }

    def _export_records_status(
        self,
        *,
        records_path: Path,
        session_ids: set[str],
        export_watermark: int,
        export_tokens: set[str],
    ) -> dict[str, Any]:
        """Return only safe export-record metadata; never reveal sessions or paths."""
        empty = {
            "freshness": "records_unavailable",
            "latest_export_at": None,
            "usable_export_count": 0,
        }
        if not records_path.is_file():
            return empty
        if not session_ids:
            return {**empty, "freshness": "no_sync_scope"}
        try:
            catalog = self.weflow.discover_exports(
                records_path=str(records_path),
                limit=1000,
            )
        except ValueError:
            return {**empty, "freshness": "records_invalid"}

        relevant = [
            item
            for item in catalog["items"]
            if str(item.get("session_id") or "") in session_ids
        ]
        if not relevant:
            return {**empty, "freshness": "no_usable_export"}
        latest_export_timestamp = max(
            (int(item.get("export_time") or 0) for item in relevant),
            default=0,
        )
        freshness = "not_compared"
        if export_watermark:
            same_watermark_record_is_new = bool(export_tokens) and any(
                int(item.get("export_time") or 0) >= export_watermark
                and export_record_token(item) not in export_tokens
                for item in relevant
            )
            freshness = (
                "new_export_available"
                if latest_export_timestamp > export_watermark or same_watermark_record_is_new
                else "current"
            )
        return {
            "freshness": freshness,
            "latest_export_at": (
                datetime.fromtimestamp(latest_export_timestamp, UTC).isoformat()
                if latest_export_timestamp
                else None
            ),
            "usable_export_count": len(relevant),
        }

    def sync_scope(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT platform_id, sync_watermark, last_synced_at
                   FROM customer_conversations
                   WHERE platform='wechat' AND platform_id<>''
                   ORDER BY platform_id"""
            ).fetchall()
        session_ids = [str(row["platform_id"]) for row in rows]
        latest_timestamp = max(
            (int(row["sync_watermark"] or 0) for row in rows),
            default=0,
        )
        import_times = [str(row["last_synced_at"]) for row in rows if row["last_synced_at"]]
        return {
            "session_ids": session_ids,
            "conversation_count": len(session_ids),
            "latest_message_timestamp": latest_timestamp or None,
            "latest_message_at": (
                datetime.fromtimestamp(latest_timestamp, UTC).isoformat()
                if latest_timestamp
                else None
            ),
            "last_import_at": max(import_times, default=None),
        }

    def start(
        self,
        *,
        weflow_root: str | None = None,
        records_path: str | None = None,
    ) -> dict[str, Any]:
        if os.name != "nt":
            raise ValueError("WeFlow 手动同步当前只支持 Windows。")
        current = _read_json(self.state_path) or {}
        if str(current.get("status")) in RUNNING_STATUSES:
            return self.status()

        scope = self.sync_scope()
        if not scope["session_ids"]:
            raise ValueError("知识库里还没有已确认的微信会话，无法确定同步范围。")

        config = _read_json(self.config_path) or {}
        resolved_root = self._resolve_weflow_root(weflow_root, config)
        resolved_records = Path(
            records_path
            or str(config.get("records_path") or "")
            or self.settings.weflow_export_records_path
        ).expanduser()
        self._validate_paths(resolved_root, resolved_records)
        _write_json(
            self.config_path,
            {
                "version": STATE_VERSION,
                "mode": "manual_only",
                "weflow_root": str(resolved_root),
                "records_path": str(resolved_records.resolve()),
            },
        )

        job_id = uuid.uuid4().hex
        state = {
            "version": STATE_VERSION,
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "summary_code": "queued",
            "started_at": _utc_now(),
            "completed_at": None,
            "imported_messages": 0,
            "duplicate_messages": 0,
            "failed_sessions": 0,
            "exported_sessions": 0,
            "no_data_sessions": 0,
            "export_failed_sessions": 0,
        }
        _write_json(self.state_path, state)

        command = [
            sys.executable,
            "-m",
            "pkas.weflow_manual_worker",
            "--job-id",
            job_id,
            "--owner-pid",
            str(os.getpid()),
            "--weflow-root",
            str(resolved_root),
            "--records-path",
            str(resolved_records.resolve()),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(self.settings.project_root),
            env=self._worker_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        state["worker_pid"] = process.pid
        _write_json(self.state_path, state)
        return self.status()

    def _configured_weflow_root(self, config: dict[str, Any]) -> Path | None:
        value = str(config.get("weflow_root") or "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_dir() else None

    def _resolve_weflow_root(
        self,
        supplied: str | None,
        config: dict[str, Any],
    ) -> Path:
        value = str(supplied or config.get("weflow_root") or "").strip()
        if not value:
            raise ValueError("尚未配置 WeFlow 程序目录。")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("WeFlow 程序目录必须是绝对路径。")
        return path.resolve()

    def _validate_paths(self, root: Path, records_path: Path) -> None:
        required = [
            root / "package.json",
            root / "node_modules" / "electron" / "dist" / "electron.exe",
            root / "node_modules" / "electron-store" / "index.js",
            self.settings.project_root / "scripts" / "configure_weflow_manual.mjs",
        ]
        missing = [path for path in required if not path.is_file()]
        if missing:
            raise ValueError("WeFlow 程序目录不完整，无法执行无窗口导出。")
        if not records_path.is_absolute() or not records_path.is_file():
            raise ValueError("WeFlow 导出记录文件不存在。")

    def _worker_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PKAS_PROJECT_ROOT"] = str(self.settings.project_root)
        environment["PKAS_DATA_ROOT"] = str(self.settings.data_root)
        return environment


def update_manual_sync_state(settings_data_root: Path, **changes: Any) -> dict[str, Any]:
    path = settings_data_root / "runtime" / "weflow-manual-sync-state.json"
    state = _read_json(path) or {"version": STATE_VERSION}
    state.update(changes)
    _write_json(path, state)
    return state
