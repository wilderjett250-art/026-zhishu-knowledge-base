import json
from datetime import UTC, datetime
from typing import Any

from pkas.scheduled_sync import run_scheduled_sync
from pkas.system import KnowledgeSystem


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
    knowledge_system = KnowledgeSystem.create()
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
