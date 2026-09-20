from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pkas.config import get_settings
from pkas.local_lock import WindowsFileLock
from pkas.system import KnowledgeSystem
from pkas.weflow_manual import (
    export_record_token,
    normalized_export_tokens,
    retained_export_tokens,
    update_manual_sync_state,
)

TERMINAL_STATUSES = {"success", "error", "skipped"}


class ManualSyncOwnerExited(RuntimeError):
    """Raised when the local PKAS backend that requested this sync has exited."""


def _owner_is_alive(owner_pid: int | None) -> bool:
    """Check the requesting backend without relying on a visible process window."""
    if owner_pid is None or owner_pid <= 0 or owner_pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information,
            False,
            owner_pid,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return ctypes.get_last_error() == 5  # Access denied still proves it exists.
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _require_owner_alive(owner_pid: int | None) -> None:
    if not _owner_is_alive(owner_pid):
        raise ManualSyncOwnerExited("知枢已退出，已停止本次微信同步。")


def _hidden_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _failure_code(exc: Exception) -> str:
    message = str(exc)
    if "当前由用户打开" in message:
        return "weflow_already_open"
    if "导出超时" in message:
        return "export_timeout"
    for category in (
        "invalid_options",
        "database",
        "permission",
        "filesystem",
        "ipc",
        "resource",
        "timeout",
        "unknown",
        "helper_task_missing",
        "helper_store_locked",
        "helper_runtime",
        "no_output",
    ):
        if f"[{category}]" in message:
            return f"weflow_export_{category}"
    if "Node.js" in message:
        return "node_unavailable"
    if "程序目录" in message or "导出记录" in message:
        return "weflow_path_invalid"
    if "没有可同步" in message:
        return "no_sync_scope"
    if "导出" in message:
        return "weflow_export_failed"
    return "manual_sync_failed"


def _run_quiet(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_hidden_flags(),
        check=False,
    )


def _node_paths(project_root: Path) -> tuple[Path, Path]:
    packaged_roots = (
        project_root / "runtime" / "node",
        project_root / "runtime" / "tauri-payload" / "node",
    )
    candidates = [
        (root / "node.exe", root / "node_modules" / "npm" / "bin" / "npm-cli.js")
        for root in packaged_roots
    ]
    candidates.append(
        (
            Path(r"C:\Program Files\nodejs\node.exe"),
            Path(r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js"),
        )
    )
    for node, npm_cli in candidates:
        if node.is_file() and npm_cli.is_file():
            return node, npm_cli
    raise OSError("Node.js 运行环境不可用；请修复安装包后重试。")


def _weflow_is_running(root: Path) -> bool:
    escaped = str(root / "node_modules" / "electron" / "dist" / "electron.exe").replace("'", "''")
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -eq 'electron.exe' -and $_.CommandLine -and "
        f"$_.CommandLine.IndexOf('{escaped}',[System.StringComparison]::OrdinalIgnoreCase) -ge 0"
        " }; if($p){exit 0}else{exit 1}"
    )
    result = subprocess.run(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_hidden_flags(),
        check=False,
    )
    return result.returncode == 0


def _task_command(
    node: Path,
    helper: Path,
    action: str,
    argument: str,
    *,
    root: Path,
    config_dir: Path,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["WEFLOW_ROOT"] = str(root)
    environment["WEFLOW_CONFIG_DIR"] = str(config_dir)
    result = _run_quiet(
        [str(node), str(helper), action, argument],
        cwd=root,
        env=environment,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "task was not found" in stderr:
            category = "helper_task_missing"
        elif any(marker in stderr for marker in ("ebusy", "eperm", "locked")):
            category = "helper_store_locked"
        else:
            category = "helper_runtime"
        raise RuntimeError(f"WeFlow 导出任务{action}失败 [{category}]。")
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("WeFlow 导出任务没有返回有效状态。") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("WeFlow 导出任务状态无效。")
    return payload


def _request_range(latest_timestamp: int) -> tuple[str, str]:
    now = datetime.now().astimezone()
    if latest_timestamp > 0:
        start = datetime.fromtimestamp(latest_timestamp).astimezone() - timedelta(days=1)
    else:
        start = now - timedelta(days=3650)
    return start.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M")


def _baseline_export_time(system: KnowledgeSystem, records_path: str, session_ids: set[str]) -> int:
    catalog = system.weflow.discover_exports(records_path=records_path, limit=1000)
    return max(
        (
            int(item.get("export_time") or 0)
            for item in catalog["items"]
            if str(item.get("session_id")) in session_ids
        ),
        default=0,
    )


def _new_export_candidates(
    catalog: dict[str, Any],
    *,
    session_ids: set[str],
    baseline_export_time: int,
    known_export_tokens: set[str],
) -> list[dict[str, Any]]:
    """Select records newer than the watermark, including unseen timestamp ties."""
    candidates: list[dict[str, Any]] = []
    for item in catalog.get("items") or []:
        if not isinstance(item, dict) or str(item.get("session_id")) not in session_ids:
            continue
        export_time = int(item.get("export_time") or 0)
        if export_time < baseline_export_time or int(item.get("byte_size") or 0) <= 0:
            continue
        token = export_record_token(item)
        if export_time > baseline_export_time or (
            bool(known_export_tokens) and token not in known_export_tokens
        ):
            candidates.append(item)
    return candidates


def _known_export_tokens(data_root: Path) -> set[str]:
    state_path = data_root / "runtime" / "weflow-manual-sync-state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    return normalized_export_tokens(
        payload.get("export_record_tokens") if isinstance(payload, dict) else None
    )


def _known_export_watermark(data_root: Path) -> int:
    """Return the last committed export watermark for retry-safe imports."""
    state_path = data_root / "runtime" / "weflow-manual-sync-state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    return max(0, int(payload.get("export_watermark") or 0))


def _committed_export_progress(
    result: dict[str, Any],
    *,
    previous_watermark: int,
    previous_tokens: set[str],
) -> tuple[int, list[str]]:
    """Advance the export watermark only after every selected session succeeds."""
    status = str(result.get("status") or "failed")
    failed_sessions = int(result.get("failed_sessions") or 0)
    if status != "completed" or failed_sessions > 0:
        return previous_watermark, retained_export_tokens(previous_tokens)
    return (
        max(previous_watermark, int(result.get("export_watermark") or 0)),
        retained_export_tokens(
            previous_tokens | set(result.get("export_record_tokens") or [])
        ),
    )


def _import_new_exports(
    system: KnowledgeSystem,
    *,
    records_path: str,
    session_ids: set[str],
    baseline_export_time: int,
    known_export_tokens: set[str],
) -> dict[str, Any]:
    catalog = system.weflow.discover_exports(records_path=records_path, limit=1000)
    candidates = _new_export_candidates(
        catalog,
        session_ids=session_ids,
        baseline_export_time=baseline_export_time,
        known_export_tokens=known_export_tokens,
    )
    if not candidates:
        return {
            "status": "completed",
            "imported_messages": 0,
            "duplicate_messages": 0,
            "failed_sessions": 0,
            "export_watermark": baseline_export_time,
            "export_record_tokens": retained_export_tokens(known_export_tokens),
        }
    selected = sorted({str(item["session_id"]) for item in candidates})
    inspection = system.weflow.inspect_export_selection(
        records_path=records_path,
        session_ids=selected,
    )
    result = system.customer_workflows.import_weflow_exports(
        records_path=records_path,
        session_ids=selected,
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )
    details = result.get("result") or {}
    return {
        "status": str(result.get("status") or "failed"),
        "imported_messages": int(details.get("imported") or 0),
        "duplicate_messages": int(details.get("duplicates") or 0),
        "failed_sessions": int(details.get("failed_sessions") or 0),
        "export_watermark": max(
            (int(item.get("export_time") or 0) for item in candidates),
            default=baseline_export_time,
        ),
        "export_record_tokens": retained_export_tokens(
            known_export_tokens | {export_record_token(item) for item in candidates}
        ),
    }


def _wait_for_new_export_records(
    system: KnowledgeSystem,
    *,
    records_path: str,
    session_ids: set[str],
    baseline_export_time: int,
    known_export_tokens: set[str],
    owner_pid: int | None = None,
    timeout_seconds: int = 30,
) -> bool:
    """Wait for an unseen export record while honoring the desktop lifecycle."""
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        _require_owner_alive(owner_pid)
        catalog = system.weflow.discover_exports(records_path=records_path, limit=1000)
        if _new_export_candidates(
            catalog,
            session_ids=session_ids,
            baseline_export_time=baseline_export_time,
            known_export_tokens=known_export_tokens,
        ):
            return True
        time.sleep(1)
    return False


def run(
    job_id: str,
    weflow_root: Path,
    records_path: Path,
    *,
    owner_pid: int | None = None,
) -> dict[str, Any]:
    _require_owner_alive(owner_pid)
    system = KnowledgeSystem.create()
    data_root = system.settings.data_root
    phase_timings: dict[str, float] = {}
    phase_started = time.monotonic()
    lock_path = data_root / "runtime" / "weflow-manual-sync.lock"
    with WindowsFileLock(lock_path):
        _require_owner_alive(owner_pid)
        update_manual_sync_state(
            data_root,
            job_id=job_id,
            status="preparing",
            stage="checking_watermark",
            summary_code="checking_watermark",
            phase_timings=phase_timings,
        )
        scope = system.weflow_manual.sync_scope()
        session_ids = set(scope["session_ids"])
        known_export_tokens = _known_export_tokens(data_root)
        if not session_ids:
            raise ValueError("知识库里没有可同步的微信会话。")
        if _weflow_is_running(weflow_root):
            raise RuntimeError("WeFlow 当前由用户打开；请退出 WeFlow 后再次点击同步。")

        node, npm_cli = _node_paths(system.settings.project_root)
        helper = system.settings.project_root / "scripts" / "configure_weflow_manual.mjs"
        committed_watermark = _known_export_watermark(data_root)
        baseline = committed_watermark or _baseline_export_time(
            system,
            str(records_path),
            session_ids,
        )
        start_text, end_text = _request_range(int(scope["latest_message_timestamp"] or 0))
        request_path = data_root / "runtime" / f"weflow-manual-request-{job_id}.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(
            json.dumps(
                {
                    "taskId": f"pkas-weflow-manual-{job_id}",
                    "sessionIds": sorted(session_ids),
                    "start": start_text,
                    "end": end_text,
                    "lastWatermarkMs": int(scope["latest_message_timestamp"] or 0) * 1000,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config_dir = records_path.parent
        task_id: str | None = None
        launcher: subprocess.Popen[bytes] | None = None
        try:
            task = _task_command(
                node,
                helper,
                "configure",
                str(request_path),
                root=weflow_root,
                config_dir=config_dir,
            )
            task_id = str(task["task_id"])
            _require_owner_alive(owner_pid)
            phase_timings["preparing"] = round(time.monotonic() - phase_started, 3)
            phase_started = time.monotonic()
            update_manual_sync_state(
                data_root,
                status="exporting",
                stage="weflow_export",
                summary_code="weflow_exporting",
                phase_timings=phase_timings,
            )

            environment = os.environ.copy()
            environment["WEFLOW_BACKGROUND_EXPORT"] = "1"
            launcher = subprocess.Popen(  # noqa: S603
                [str(node), str(npm_cli), "run", "electron:dev"],
                cwd=str(weflow_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_hidden_flags(),
            )
            deadline = time.monotonic() + int(system.settings.weflow_manual_timeout_seconds)
            task_status: dict[str, Any] = {}
            while time.monotonic() < deadline:
                time.sleep(2)
                _require_owner_alive(owner_pid)
                task_status = _task_command(
                    node,
                    helper,
                    "status",
                    task_id,
                    root=weflow_root,
                    config_dir=config_dir,
                )
                if str(task_status.get("run_status")) in TERMINAL_STATUSES:
                    break
            else:
                raise TimeoutError("WeFlow 导出超时。")

            if task_status.get("run_status") == "error":
                category = str(task_status.get("error_code") or "unknown")
                raise RuntimeError(f"WeFlow 导出失败 [{category}]。")
            if task_status.get("run_status") == "skipped":
                if task_status.get("skip_code") != "no_new_messages":
                    raise RuntimeError("WeFlow 未能完成本次导出。")
                result: dict[str, Any] = {
                    "status": "completed",
                    "summary_code": "no_new_messages",
                    "imported_messages": 0,
                    "duplicate_messages": 0,
                    "failed_sessions": 0,
                    "exported_sessions": 0,
                    "no_data_sessions": 0,
                    "export_failed_sessions": 0,
                    "export_watermark": baseline,
                    "export_record_tokens": retained_export_tokens(known_export_tokens),
                }
            else:
                phase_timings["exporting"] = round(time.monotonic() - phase_started, 3)
                phase_started = time.monotonic()
                update_manual_sync_state(
                    data_root,
                    status="importing",
                    stage="pkas_import",
                    summary_code="importing",
                    phase_timings=phase_timings,
                )
                if not _wait_for_new_export_records(
                    system,
                    records_path=str(records_path),
                    session_ids=session_ids,
                    baseline_export_time=baseline,
                    known_export_tokens=known_export_tokens,
                    owner_pid=owner_pid,
                ):
                    raise RuntimeError("WeFlow 导出成功但未产生新导出记录 [no_output]。")
                result = _import_new_exports(
                    system,
                    records_path=str(records_path),
                    session_ids=session_ids,
                    baseline_export_time=baseline,
                    known_export_tokens=known_export_tokens,
                )
                result["exported_sessions"] = int(
                    task_status.get("exported_session_count") or 0
                )
                result["no_data_sessions"] = int(
                    task_status.get("no_data_session_count") or 0
                )
                result["export_failed_sessions"] = int(
                    task_status.get("failed_session_count") or 0
                )
                result["summary_code"] = (
                    "import_completed"
                    if int(result["imported_messages"]) > 0
                    else "no_new_messages"
                )
                phase_timings["importing"] = round(time.monotonic() - phase_started, 3)
            if "exporting" not in phase_timings:
                phase_timings["exporting"] = round(time.monotonic() - phase_started, 3)
            result["phase_timings"] = phase_timings
            return result
        finally:
            if launcher is not None:
                subprocess.run(  # noqa: S603
                    ["taskkill.exe", "/PID", str(launcher.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_hidden_flags(),
                    check=False,
                )
                time.sleep(1)
            if task_id is not None:
                with suppress(OSError, RuntimeError):
                    _task_command(
                        node,
                        helper,
                        "cleanup",
                        task_id,
                        root=weflow_root,
                        config_dir=config_dir,
                    )
            request_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one user-triggered WeFlow sync")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--weflow-root", required=True)
    parser.add_argument("--records-path", required=True)
    args = parser.parse_args()
    data_root = get_settings().data_root
    started_monotonic = time.monotonic()
    previous_watermark = _known_export_watermark(data_root)
    previous_tokens = _known_export_tokens(data_root)
    try:
        result = run(
            args.job_id,
            Path(args.weflow_root),
            Path(args.records_path),
            owner_pid=args.owner_pid,
        )
        committed_watermark, committed_tokens = _committed_export_progress(
            result,
            previous_watermark=previous_watermark,
            previous_tokens=previous_tokens,
        )
        update_manual_sync_state(
            data_root,
            job_id=args.job_id,
            status=str(result.get("status") or "completed"),
            stage="completed",
            summary_code=result.get("summary_code"),
            completed_at=datetime.now(UTC).isoformat(),
            imported_messages=int(result.get("imported_messages") or 0),
            duplicate_messages=int(result.get("duplicate_messages") or 0),
            failed_sessions=int(result.get("failed_sessions") or 0),
            exported_sessions=int(result.get("exported_sessions") or 0),
            no_data_sessions=int(result.get("no_data_sessions") or 0),
            export_failed_sessions=int(
                result.get("export_failed_sessions") or 0
            ),
            export_watermark=committed_watermark,
            export_record_tokens=committed_tokens,
            phase_timings=result.get("phase_timings") or {},
            total_elapsed_seconds=round(time.monotonic() - started_monotonic, 3),
            export_progress_committed=(
                committed_watermark != previous_watermark
                or set(committed_tokens) != previous_tokens
            ),
        )
    except ManualSyncOwnerExited:
        update_manual_sync_state(
            data_root,
            job_id=args.job_id,
            status="stopped",
            stage="stopped",
            summary_code="owner_exited",
            completed_at=datetime.now(UTC).isoformat(),
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        update_manual_sync_state(
            data_root,
            job_id=args.job_id,
            status="failed",
            stage="failed",
            summary_code=_failure_code(exc),
            completed_at=datetime.now(UTC).isoformat(),
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        update_manual_sync_state(
            data_root,
            job_id=args.job_id,
            status="failed",
            stage="failed",
            summary_code="manual_sync_unexpected_error",
            completed_at=datetime.now(UTC).isoformat(),
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
