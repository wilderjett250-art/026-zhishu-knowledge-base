from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pkas.run_diagnostics import ACTION, issue, write_run_receipt

if TYPE_CHECKING:
    from pkas.system import KnowledgeSystem

EXPORT_FAILURE_CODES = (
    "weflow_export_failed",
    "weflow_export_timeout",
    "weflow_schedule_not_due",
    "weflow_process_exited",
    "weflow_config_unreadable",
    "weflow_task_mismatch",
    "weflow_export_directory",
    "weflow_export_low_disk",
    "weflow_export_quota",
    "weflow_export_sessions",
    "weflow_export_partial",
)


def _duration_argument(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected milliseconds between 0 and 86400000") from None
    if not 0 <= number <= 86_400_000:
        raise argparse.ArgumentTypeError("Expected milliseconds between 0 and 86400000")
    return number


def _timing_counts(report: dict[str, Any], result: dict[str, Any]) -> dict[str, int]:
    timings = result.get("timings_ms")
    timings = timings if isinstance(timings, dict) else {}
    candidates = {
        "weflow_export_wait_ms": report.get("export_wait_ms"),
        "weflow_import_ms": timings.get("weflow_import"),
        "local_refresh_ms": timings.get("local_refresh"),
        "worker_total_ms": report.get("worker_elapsed_ms"),
    }
    return {
        key: value for key, value in candidates.items()
        if type(value) is int and 0 <= value <= 86_400_000
    }


def _write_report(knowledge_system: KnowledgeSystem, report: dict[str, Any]) -> str:
    return _write_report_at(knowledge_system.settings.data_root, report)


def _write_report_at(data_root: Path, report: dict[str, Any]) -> str:
    result = report.get("result") or {}
    full = result.get("full_sync") or {}
    weflow = result.get("weflow_import") or {}
    issues = []
    if result.get("state_save_failed"):
        issues.append(issue("sync_state_failed"))
    if report["status"] == "failed" and "error_type" in report:
        failure_code = report.get("failure_code") or {
            "ValueError": "worker_config",
            "OSError": "worker_io",
            "FileNotFoundError": "worker_io",
            "PermissionError": "worker_io",
        }.get(report["error_type"], "worker_failed")
        issues.append(
            issue(
                failure_code if failure_code in ACTION else "worker_failed",
                error_type=report["error_type"],
            )
        )
    if result.get("weflow_export_status") in {"error", "timeout"}:
        export_code = report.get("export_failure_code") or (
            "weflow_export_timeout"
            if result["weflow_export_status"] == "timeout"
            else "weflow_export_failed"
        )
        issues.append(
            issue(export_code if export_code in EXPORT_FAILURE_CODES else "weflow_export_failed")
        )
    if int(full.get("failed_roots") or 0) or int(full.get("warning_roots") or 0):
        issues.append(
            issue(
                "root_partial",
                int(full.get("failed_roots") or 0) + int(full.get("warning_roots") or 0),
            )
        )
    if full.get("low_disk"):
        issues.append(issue("low_disk_space"))
    if full.get("no_local_roots"):
        issues.append(issue("no_registered_roots"))
    if full.get("status") == "failed" and not int(full.get("failed_roots") or 0):
        issues.append(issue("worker_failed"))
    if weflow.get("status") == "failed":
        if weflow.get("error_code") == "low_disk_space":
            if not full.get("low_disk"):
                issues.append(issue("low_disk_space"))
        else:
            failure_code = weflow.get("error_code")
            if failure_code == "export_catalog_truncated":
                failure_code = "weflow_catalog_failed"
            issues.append(
                issue(
                    failure_code if failure_code in ACTION else "weflow_failed",
                    error_type=weflow.get("error_type"),
                )
            )
    if int(weflow.get("failed_sessions") or 0):
        issues.append(issue("weflow_partial", int(weflow["failed_sessions"])))
    for code, count in (weflow.get("failure_counts") or {}).items():
        if code in ACTION and int(count) > 0:
            issues.append(issue(code, int(count)))
    if int(weflow.get("retention_errors") or 0):
        issues.append(issue("weflow_retention_warning", int(weflow["retention_errors"])))
    output_path = write_run_receipt(
        data_root,
        kind="scheduled-sync",
        status=report["status"],
        run_id=report.get("run_id"),
        counts={
            "full_sync_due": int(bool(result.get("full_sync_due"))),
            "selected_roots": int(full.get("selected_roots") or 0),
            "disabled_roots": int(full.get("disabled_roots") or 0),
            "weflow_enabled": int(bool(weflow) and weflow.get("status") != "disabled"),
            "weflow_export_skipped": int(result.get("weflow_export_status") == "skipped"),
            "weflow_candidate_sessions": int(weflow.get("candidate_sessions") or 0),
            "weflow_imported_messages": int(weflow.get("imported_messages") or 0),
            "weflow_import_low_disk": int(weflow.get("error_code") == "low_disk_space"),
            "weflow_import_failed": int(weflow.get("status") == "failed"),
            "failed_roots": int(full.get("failed_roots") or 0),
            "warning_roots": int(full.get("warning_roots") or 0),
            "low_disk": int(
                bool(full.get("low_disk") or weflow.get("error_code") == "low_disk_space")
            ),
            "no_local_roots": int(bool(full.get("no_local_roots"))),
            "weflow_failed_sessions": int(weflow.get("failed_sessions") or 0),
            "weflow_export_copies_pruned": int(weflow.get("pruned_files") or 0),
            "weflow_export_bytes_freed": int(weflow.get("freed_bytes") or 0),
            "weflow_retention_errors": int(weflow.get("retention_errors") or 0),
            "weflow_export_failed": int(result.get("weflow_export_status") in {"error", "timeout"}),
            **_timing_counts(report, result),
        },
        issues=issues,
    )
    return str(output_path)


def _authorization_enabled(data_root: Path) -> bool:
    """Read only the explicit authorization file, without initializing the DB."""
    path = data_root / "config" / "weflow-daily-import.json"
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    state = json.loads(content)
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("enabled"), bool)
    ):
        raise ValueError("Invalid daily-import authorization")
    return state["enabled"]


def main() -> None:
    worker_started_ns = time.perf_counter_ns()
    parser = argparse.ArgumentParser(description="Run the PKAS scheduled sync")
    parser.add_argument(
        "--weflow-export-wait-ms", type=_duration_argument,
        help="observed export wait duration, never a message or raw exception",
    )
    parser.add_argument(
        "--weflow-export-code",
        choices=EXPORT_FAILURE_CODES,
        help="allowlisted export failure reason, never raw WeFlow exception text",
    )
    parser.add_argument(
        "--check-daily-authorization",
        action="store_true",
        help="check explicit WeFlow daily-import authorization without running sync",
    )
    parser.add_argument(
        "--weflow-export-status",
        choices=["ready", "skipped", "error", "timeout"],
        default="ready",
        help="result of the separate WeFlow export wait, without message content",
    )
    parser.add_argument(
        "--record-daily-check",
        action="store_true",
        help="record daily success only after export, import and receipt succeed",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="refresh registered local roots without reading or starting WeFlow",
    )
    args = parser.parse_args()
    started_at_ms = int(time.time() * 1000)
    project_root = Path(os.environ.get("PKAS_PROJECT_ROOT") or Path(__file__).resolve().parents[2])
    data_root = Path(os.environ.get("PKAS_DATA_ROOT") or project_root / "data")
    try:
        run_id = uuid.UUID(os.environ.get("PKAS_SYNC_RUN_ID", "")).hex
    except ValueError:
        run_id = uuid.uuid4().hex
    failure_code = (
        "weflow_authorization_failed"
        if args.check_daily_authorization
        else ("worker_initialization")
    )
    report: dict[str, Any]
    try:
        if args.check_daily_authorization:
            enabled = _authorization_enabled(data_root)
            print(
                json.dumps(
                    {
                        "status": "authorized" if enabled else "disabled",
                        "daily_import_enabled": enabled,
                        "content_read": False,
                        "secret_fields_accessed": False,
                    }
                )
            )
            if not enabled:
                raise SystemExit(2)
            return
        # A broken dependency/configuration/database must still have a receipt.
        # Import the application only inside the guarded entry point.
        from pkas.scheduled_sync import run_scheduled_sync
        from pkas.system import KnowledgeSystem
        from pkas.weflow_daily import run_daily_import

        knowledge_system = KnowledgeSystem.create()
        data_root = knowledge_system.settings.data_root
        failure_code = "worker_failed"
        result = run_scheduled_sync(
            knowledge_system,
            weflow_export_status=args.weflow_export_status,
            weflow_import_runner=(
                (lambda _: {"status": "disabled", "failed_sessions": 0})
                if args.local_only
                else run_daily_import
            ),
        )
        report = {
            "status": result.get("status", "completed"),
            "result": result,
            "export_failure_code": args.weflow_export_code,
        }
    except Exception as exc:
        # Scheduled tasks have no interactive terminal. Persist only the
        # exception class; never log source paths, messages, or chat content.
        report = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "failure_code": failure_code,
        }
    report["run_id"] = run_id
    report["export_wait_ms"] = args.weflow_export_wait_ms
    report["worker_elapsed_ms"] = max(
        0, (time.perf_counter_ns() - worker_started_ns) // 1_000_000,
    )
    try:
        report["report_path"] = _write_report_at(data_root, report)
        if args.record_daily_check and report["status"] in {"completed", "deferred"}:
            result = report.get("result") or {}
            weflow = result.get("weflow_import") or {}
            weflow_checked = (
                weflow.get("status") == "completed"
                and not int(weflow.get("failed_sessions") or 0)
                and result.get("weflow_export_status") in {"ready", "skipped"}
            )
            if weflow_checked or weflow.get("status") == "disabled":
                try:
                    from pkas.sync_schedule import record_daily_success

                    record_daily_success(
                        data_root,
                        run_id=run_id,
                        started_at_ms=started_at_ms,
                        completed_at_ms=int(time.time() * 1000),
                        weflow_checked=weflow_checked,
                    )
                except Exception:
                    report["status"] = "failed"
                    report["result"]["state_save_failed"] = True
                    report["report_path"] = _write_report_at(data_root, report)
    except Exception:
        # Last-resort safe signal when the disk/permissions prevent a file log.
        # The PowerShell launcher also records this failure independently.
        print("[PKAS_SYNC] sync_receipt_failed", file=sys.stderr)
        print(
            json.dumps(
                {"status": "failed", "run_id": run_id, "failure_code": "sync_receipt_failed"}
            )
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {"status": report["status"], "report_path": report["report_path"]},
            ensure_ascii=False,
        )
    )
    if report["status"] == "failed":
        raise SystemExit(1)
    if report["status"] == "warning":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
