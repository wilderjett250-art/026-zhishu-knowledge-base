import argparse
import json
import shutil
from datetime import UTC, datetime
from typing import Any

from pkas.run_diagnostics import issue, root_scan_issues, write_run_receipt
from pkas.system import KnowledgeSystem


def run_sync(
    knowledge_system: KnowledgeSystem,
    *,
    connector_type: str | None = None,
    sync_mode: str | None = None,
    local_only: bool = False,
    process_outbox: bool = True,
    minimum_free_bytes: int = 0,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    selected = [
        root
        for root in knowledge_system.sync.list_roots()
        if root["enabled"]
        and (connector_type is None or root["connector_type"] == connector_type)
        and (sync_mode is None or root["sync_mode"] == sync_mode)
        and (not local_only or root["connector_type"] in {"local_files", "obsidian_vault"})
    ]
    results: list[dict[str, Any]] = []
    failed = 0
    warning = 0
    disabled = 0
    outbox_warning = 0
    low_disk = False
    for root in selected:
        if minimum_free_bytes and shutil.disk_usage(
            knowledge_system.settings.data_root
        ).free < minimum_free_bytes:
            low_disk = True
            break
        try:
            result = knowledge_system.sync.scan_root(root["id"])
            root_errors = (
                int(result.get("errors", 0))
                + int(result.get("unreadable", 0))
                + int(result.get("classification_failed", 0))
            )
            root_status = (
                "disabled"
                if result.get("status") == "disabled"
                else "warning" if root_errors else "completed"
            )
            if root_errors:
                warning += 1
            if root_status == "disabled":
                disabled += 1
            results.append(
                {
                    "root_id": root["id"],
                    "name": root["name"],
                    "status": root_status,
                    "result": result,
                }
            )
        except (OSError, ValueError) as exc:
            failed += 1
            results.append(
                {
                    "root_id": root["id"],
                    "name": root["name"],
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
    daily_closeout: dict[str, Any] = {
        "status": "disabled",
        "reason": "codex_is_the_interactive_agent",
    }
    outbox = (
        knowledge_system.outbox.process(limit=1000)
        if process_outbox
        else {"status": "skipped", "reason": "scheduled_local_scan_only"}
    )
    if process_outbox and outbox["status"] != "completed":
        outbox_warning = 1
    no_local_roots = local_only and not selected
    completed_at = datetime.now(UTC).isoformat()
    report = {
        "status": (
            "failed"
            if failed
            else (
                "warning"
                if warning or outbox_warning or no_local_roots or low_disk
                else "completed"
            )
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "selected_roots": len(selected),
        "failed_roots": failed,
        "warning_roots": warning,
        "disabled_roots": disabled,
        "no_local_roots": no_local_roots,
        "low_disk": low_disk,
        "daily_closeout_failures": 0,
        "index_outbox_warnings": outbox_warning,
        "results": results,
        "daily_closeout": daily_closeout,
        "index_outbox": outbox,
    }
    issues = []
    for row in results:
        if row["status"] == "failed":
            issues.append(issue("root_failed", root_id=row["root_id"]))
            continue
        result = row.get("result") or {}
        issues.extend(root_scan_issues(result, row["root_id"]))
    if outbox_warning:
        issues.append(issue("index_outbox"))
    if no_local_roots:
        issues.append(issue("no_registered_roots"))
    if low_disk:
        issues.append(issue("low_disk_space"))
    receipt = write_run_receipt(
        knowledge_system.settings.data_root,
        kind="sync", status=report["status"],
        counts={
            "selected_roots": len(selected), "failed_roots": failed,
            "warning_roots": warning, "disabled_roots": disabled,
            "index_outbox_warnings": outbox_warning,
            "no_local_roots": int(no_local_roots),
            "low_disk": int(low_disk),
        },
        issues=issues,
    )
    report["report_path"] = str(receipt)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="增量同步已授权的知识库资料源")
    parser.add_argument("--connector", choices=["local_files", "codex_sessions"])
    parser.add_argument("--sync-mode", choices=["catalog", "index"])
    args = parser.parse_args()
    result = run_sync(
        KnowledgeSystem.create(),
        connector_type=args.connector,
        sync_mode=args.sync_mode,
    )
    print(json.dumps(
        {"status": result["status"], "report_path": result["report_path"]},
        ensure_ascii=False,
    ))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
