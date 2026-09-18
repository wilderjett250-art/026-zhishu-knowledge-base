import argparse
import json
from datetime import UTC, datetime
from typing import Any

from pkas.scheduled_sync import run_scheduled_sync
from pkas.system import KnowledgeSystem
from pkas.weflow_daily import daily_import_is_enabled


def _write_report(knowledge_system: KnowledgeSystem, report: dict[str, Any]) -> str:
    output_dir = knowledge_system.settings.data_root / "runs" / "scheduled-sync"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"scheduled-sync-{stamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PKAS scheduled sync")
    parser.add_argument(
        "--check-daily-authorization",
        action="store_true",
        help="check explicit WeFlow daily-import authorization without running sync",
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
        result = run_scheduled_sync(knowledge_system)
        report = {
            "status": result.get("status", "completed"),
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
    except (OSError, ValueError) as exc:
        report = {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_type": type(exc).__name__,
        }
    report["report_path"] = _write_report(knowledge_system, report)
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
