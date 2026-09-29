import argparse
import json
from datetime import UTC, datetime
from typing import Any

from pkas.run_diagnostics import issue, write_run_receipt
from pkas.scheduled_sync import run_scheduled_sync
from pkas.system import KnowledgeSystem
from pkas.weflow_daily import daily_import_is_enabled, run_daily_import


def _write_report(knowledge_system: KnowledgeSystem, report: dict[str, Any]) -> str:
    result = report.get("result") or {}
    full = result.get("full_sync") or {}
    weflow = result.get("weflow_import") or {}
    issues = []
    if report["status"] == "failed" and "error_type" in report:
        failure_code = {
            "ValueError": "worker_config", "OSError": "worker_io",
            "FileNotFoundError": "worker_io", "PermissionError": "worker_io",
        }.get(report["error_type"], "worker_failed")
        issues.append(issue(failure_code))
    if int(full.get("failed_roots") or 0) or int(full.get("warning_roots") or 0):
        issues.append(issue(
            "root_partial",
            int(full.get("failed_roots") or 0) + int(full.get("warning_roots") or 0),
        ))
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
            issues.append(issue("weflow_failed"))
    elif int(weflow.get("failed_sessions") or 0):
        issues.append(issue("weflow_partial", int(weflow["failed_sessions"])))
    if int(weflow.get("retention_errors") or 0):
        issues.append(issue("weflow_retention_warning", int(weflow["retention_errors"])))
    if result.get("weflow_export_status") in {"error", "timeout"}:
        issues.append(issue("weflow_export_failed"))
    output_path = write_run_receipt(
        knowledge_system.settings.data_root,
        kind="scheduled-sync", status=report["status"],
        counts={
            "full_sync_due": int(bool(result.get("full_sync_due"))),
            "selected_roots": int(full.get("selected_roots") or 0),
            "disabled_roots": int(full.get("disabled_roots") or 0),
            "weflow_enabled": int(weflow.get("status") != "disabled"),
            "weflow_export_skipped": int(result.get("weflow_export_status") == "skipped"),
            "weflow_candidate_sessions": int(weflow.get("candidate_sessions") or 0),
            "weflow_imported_messages": int(weflow.get("imported_messages") or 0),
            "weflow_import_low_disk": int(weflow.get("error_code") == "low_disk_space"),
            "weflow_import_failed": int(weflow.get("status") == "failed"),
            "failed_roots": int(full.get("failed_roots") or 0),
            "warning_roots": int(full.get("warning_roots") or 0),
            "low_disk": int(bool(
                full.get("low_disk") or weflow.get("error_code") == "low_disk_space"
            )),
            "no_local_roots": int(bool(full.get("no_local_roots"))),
            "weflow_failed_sessions": int(weflow.get("failed_sessions") or 0),
            "weflow_export_copies_pruned": int(weflow.get("pruned_files") or 0),
            "weflow_export_bytes_freed": int(weflow.get("freed_bytes") or 0),
            "weflow_retention_errors": int(weflow.get("retention_errors") or 0),
            "weflow_export_failed": int(
                result.get("weflow_export_status") in {"error", "timeout"}
            ),
        },
        issues=issues,
    )
    return str(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PKAS scheduled sync")
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
        "--local-only", action="store_true",
        help="refresh registered local roots without reading or starting WeFlow",
    )
    args = parser.parse_args()
    knowledge_system = KnowledgeSystem.create()
    if args.check_daily_authorization:
        enabled = daily_import_is_enabled(knowledge_system)
        print(
            json.dumps(
                {
                    "status": "authorized" if enabled else "disabled",
                    "daily_import_enabled": enabled,
                    "content_read": False,
                    "secret_fields_accessed": False,
                },
                ensure_ascii=False,
            )
        )
        if not enabled:
            raise SystemExit(2)
        return
    started_at = datetime.now(UTC).isoformat()
    try:
        result = run_scheduled_sync(
            knowledge_system,
            weflow_export_status=args.weflow_export_status,
            weflow_import_runner=(
                (lambda _: {"status": "disabled", "failed_sessions": 0})
                if args.local_only else run_daily_import
            ),
        )
        report = {
            "status": result.get("status", "completed"),
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
    except Exception as exc:
        # Scheduled tasks have no interactive terminal. Persist only the
        # exception class; never log source paths, messages, or chat content.
        report = {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_type": type(exc).__name__,
        }
    report["report_path"] = _write_report(knowledge_system, report)
    print(json.dumps(
        {"status": report["status"], "report_path": report["report_path"]},
        ensure_ascii=False,
    ))
    if report["status"] == "failed":
        raise SystemExit(1)
    if report["status"] == "warning":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
