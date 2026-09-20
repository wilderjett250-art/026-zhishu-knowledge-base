from __future__ import annotations

import gc
import json
import tempfile
from pathlib import Path
from typing import Any

from pkas.config import get_settings
from pkas.system import KnowledgeSystem

PROBE_TASK = "PKAS_AGENT_CONNECTIVITY_PROBE_20260806"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    base_settings = get_settings()
    report_path = base_settings.data_root / "runs" / "deepseek-setup-verification.json"
    report: dict[str, Any] = {
        "status": "failed",
        "configured": base_settings.deepseek_enabled,
        "framework": "langgraph",
    }

    if not base_settings.deepseek_enabled:
        report["error_code"] = "deepseek_not_configured"
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=True))
        return 2

    temporary_root = base_settings.data_root / "runs" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="pkas-setup-", dir=temporary_root) as temp_dir:
            probe_settings = base_settings.model_copy(
                update={"data_root": Path(temp_dir) / "data"}
            )
            system = KnowledgeSystem.create(probe_settings)
            result = system.agent.run(
                task=PROBE_TASK,
                domain="work",
                include_restricted=False,
                persist_result=False,
                complexity="simple",
            )
            report.update(
                {
                    "status": result.get("status", "failed"),
                    "run_id": result.get("run_id"),
                    "framework": result.get("framework", "langgraph"),
                }
            )
            checkpoint = result.get("checkpoint")
            if isinstance(checkpoint, dict):
                report["checkpoint_resumable"] = bool(checkpoint.get("resumable"))
                report["next_nodes"] = checkpoint.get("next_nodes", [])
            if report["status"] != "completed":
                error = result.get("error")
                if isinstance(error, dict):
                    report["error_code"] = error.get("code", "agent_probe_failed")
            del system
            gc.collect()
    except Exception as exc:
        report["status"] = "failed"
        report["error_code"] = type(exc).__name__

    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
