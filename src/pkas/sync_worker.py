import argparse
import json
from datetime import UTC, datetime
from typing import Any

from pkas.system import KnowledgeSystem


def run_sync(
    knowledge_system: KnowledgeSystem,
    *,
    connector_type: str | None = None,
    sync_mode: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    selected = [
        root
        for root in knowledge_system.sync.list_roots()
        if root["enabled"]
        and (connector_type is None or root["connector_type"] == connector_type)
        and (sync_mode is None or root["sync_mode"] == sync_mode)
    ]
    results: list[dict[str, Any]] = []
    failed = 0
    warning = 0
    outbox_warning = 0
    for root in selected:
        try:
            result = knowledge_system.sync.scan_root(root["id"])
            root_errors = int(result.get("errors", 0))
            root_status = (
                "disabled"
                if result.get("status") == "disabled"
                else "warning" if root_errors else "completed"
            )
            if root_errors:
                warning += 1
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
    outbox = knowledge_system.outbox.process(limit=1000)
    if outbox["status"] != "completed":
        outbox_warning = 1
    completed_at = datetime.now(UTC).isoformat()
    report = {
        "status": (
            "failed"
            if failed
            else (
                "warning"
                if warning or outbox_warning
                else "completed"
            )
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "selected_roots": len(selected),
        "failed_roots": failed,
        "warning_roots": warning,
        "daily_closeout_failures": 0,
        "index_outbox_warnings": outbox_warning,
        "results": results,
        "daily_closeout": daily_closeout,
        "index_outbox": outbox,
    }
    output_dir = knowledge_system.settings.data_root / "runs" / "sync"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"sync-{stamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(output_path)
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
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
