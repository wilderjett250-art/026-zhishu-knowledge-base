import argparse
import json
from datetime import UTC, datetime
from typing import Any

from pkas.system import KnowledgeSystem
from pkas.weflow_daily import authorize_daily_import, disable_daily_import, run_daily_import


def _write_report(knowledge_system: KnowledgeSystem, report: dict[str, Any]) -> str:
    output_dir = knowledge_system.settings.data_root / "runs" / "weflow-daily"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"weflow-daily-{stamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import only newly exported daily WeFlow XLSX files"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--authorize", action="store_true")
    action.add_argument("--disable", action="store_true")
    parser.add_argument("--records-path")
    args = parser.parse_args()

    knowledge_system = KnowledgeSystem.create()
    started_at = datetime.now(UTC).isoformat()
    try:
        if args.authorize:
            result = authorize_daily_import(
                knowledge_system,
                records_path=args.records_path,
            )
        elif args.disable:
            result = disable_daily_import(knowledge_system)
        else:
            result = run_daily_import(knowledge_system)
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
