from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pkas.local_lock import WindowsFileLock
from pkas.system import KnowledgeSystem
from pkas.weflow_manual import update_manual_sync_state

TERMINAL_STATUSES = {"success", "error", "skipped"}


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


def _node_paths() -> tuple[Path, Path]:
    node = Path(r"C:\Program Files\nodejs\node.exe")
    npm_cli = Path(r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js")
    if not node.is_file() or not npm_cli.is_file():
        raise OSError("Node.js 运行环境不可用。")
    return node, npm_cli


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


def _import_new_exports(
    system: KnowledgeSystem,
    *,
    records_path: str,
    session_ids: set[str],
    baseline_export_time: int,
) -> dict[str, int | str]:
    catalog = system.weflow.discover_exports(records_path=records_path, limit=1000)
    candidates = [
        item
        for item in catalog["items"]
        if str(item.get("session_id")) in session_ids
        and int(item.get("export_time") or 0) > baseline_export_time
        and int(item.get("byte_size") or 0) > 0
    ]
    if not candidates:
        return {
            "status": "completed",
            "imported_messages": 0,
            "duplicate_messages": 0,
            "failed_sessions": 0,
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
    }


def _wait_for_new_export_records(
    system: KnowledgeSystem,
    *,
    records_path: str,
    session_ids: set[str],
    baseline_export_time: int,
    timeout_seconds: int = 30,
) -> bool:
    """Wait for WeFlow's synchronous record writes to become observable."""
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if _baseline_export_time(system, records_path, session_ids) > baseline_export_time:
            return True
        time.sleep(1)
    return False


def run(job_id: str, weflow_root: Path, records_path: Path) -> dict[str, Any]:
    system = KnowledgeSystem.create()
    system.database.initialize()
    data_root = system.settings.data_root
    lock_path = data_root / "runtime" / "weflow-manual-sync.lock"
    with WindowsFileLock(lock_path):
        update_manual_sync_state(
            data_root,
            job_id=job_id,
            status="preparing",
            stage="checking_watermark",
            summary_code="checking_watermark",
        )
        scope = system.weflow_manual.sync_scope()
        session_ids = set(scope["session_ids"])
        if not session_ids:
            raise ValueError("知识库里没有可同步的微信会话。")
        if _weflow_is_running(weflow_root):
            raise RuntimeError("WeFlow 当前由用户打开；请退出 WeFlow 后再次点击同步。")

        node, npm_cli = _node_paths()
        helper = system.settings.project_root / "scripts" / "configure_weflow_manual.mjs"
        baseline = _baseline_export_time(system, str(records_path), session_ids)
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
            update_manual_sync_state(
                data_root,
                status="exporting",
                stage="weflow_export",
                summary_code="weflow_exporting",
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
                }
            else:
                update_manual_sync_state(
                    data_root,
                    status="importing",
                    stage="pkas_import",
                    summary_code="importing",
                )
                if not _wait_for_new_export_records(
                    system,
                    records_path=str(records_path),
                    session_ids=session_ids,
                    baseline_export_time=baseline,
                ):
                    raise RuntimeError("WeFlow 导出成功但未产生新导出记录 [no_output]。")
                result = _import_new_exports(
                    system,
                    records_path=str(records_path),
                    session_ids=session_ids,
                    baseline_export_time=baseline,
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
    parser.add_argument("--weflow-root", required=True)
    parser.add_argument("--records-path", required=True)
    args = parser.parse_args()
    system = KnowledgeSystem.create()
    data_root = system.settings.data_root
    try:
        result = run(args.job_id, Path(args.weflow_root), Path(args.records_path))
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


if __name__ == "__main__":
    main()
