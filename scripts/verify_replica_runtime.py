from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from pkas.config import Settings
from pkas.system import KnowledgeSystem

PROBE_MARKER = "PKAS_REPLICA_FTS_PROBE_7F2A91"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an isolated PKAS replica.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    data_root = args.data_root.resolve()
    report_path = data_root / "runs" / "replica-verification.json"
    report: dict[str, Any] = {
        "status": "failed",
        "protocol": "windows-isolated-replica-v1",
        "project_root": str(project_root),
        "data_root": str(data_root),
        "cloud_calls_allowed": False,
        "checks": {},
    }

    try:
        settings = Settings(
            project_root=project_root,
            data_root=data_root,
            embedding_enabled=False,
            rerank_enabled=False,
            deepseek_api_key=None,
            qdrant_url=args.qdrant_url,
            agent_daily_closeout_enabled=False,
        )
        settings.ensure_directories()
        system = KnowledgeSystem.create(settings)

        import_result = system.ingestion.import_text(
            text=(
                "# PKAS replica verification\n\n"
                f"The deterministic retrieval marker is {PROBE_MARKER}.\n"
            ),
            title="PKAS replica verification",
            original_uri="pkas://replica-verification/fts-probe-v1",
            source_type="replica-verification",
            domain="work",
            privacy="private",
        )
        response = system.retrieval.search(
            PROBE_MARKER,
            domain="work",
            limit=5,
            include_restricted=False,
            rerank_mode="never",
        )
        marker_found = any(
            PROBE_MARKER in str(item.get("snippet") or "") for item in response.results
        )
        with sqlite3.connect(settings.database_path) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            schema_row = connection.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()
            schema_version = int(schema_row[0]) if schema_row else 0

        secret_files = list(data_root.rglob("*.dpapi"))
        checks = {
            "project_root_resolved": settings.project_root.resolve() == project_root,
            "data_root_isolated": settings.data_root.resolve() == data_root,
            "database_exists": settings.database_path.is_file(),
            "sqlite_quick_check": quick_check == "ok",
            "schema_initialized": schema_version > 0,
            "synthetic_import_completed": import_result.get("status")
            in {"imported", "duplicate"},
            "fts_marker_found": marker_found,
            "web_build_exists": (project_root / "web" / "dist" / "index.html").is_file(),
            "no_dpapi_secret_copied": len(secret_files) == 0,
            "embedding_disabled": not settings.embedding_enabled,
            "rerank_disabled": not settings.rerank_enabled,
            "agent_disabled": not settings.agent_daily_closeout_enabled,
        }
        report.update(
            {
                "checks": checks,
                "schema_version": schema_version,
                "retrieval_mode": response.mode,
                "result_count": len(response.results),
                "status": "passed" if all(checks.values()) else "failed",
            }
        )
    except Exception as exc:
        report["error_code"] = type(exc).__name__

    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
