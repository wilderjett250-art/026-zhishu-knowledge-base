from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from pkas.agent_worker import run_agent_jobs
from pkas.local_lock import WindowsFileLock
from pkas.sync_worker import run_sync
from pkas.system import KnowledgeSystem

CoreWorkerLock = WindowsFileLock


def run_core_cycle(
    system: KnowledgeSystem,
    *,
    include_sync: bool = False,
    process_agents: bool = True,
) -> dict[str, object]:
    system.database.initialize()
    started_at = datetime.now(UTC).isoformat()
    if include_sync:
        result = run_sync(system)
        return {
            "status": result["status"],
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "sync_report_path": result["report_path"],
            "failed_roots": result["failed_roots"],
            "warning_roots": result["warning_roots"],
            "index_outbox": result["index_outbox"],
            "daily_closeout": result["daily_closeout"],
        }
    outbox = system.outbox.process(limit=1000)
    agent = (
        run_agent_jobs(system, max_jobs=1)
        if process_agents and system.settings.deepseek_enabled
        else {"status": "skipped", "reason": "disabled_for_cycle"}
    )
    warning = outbox["status"] != "completed" or agent["status"] in {"warning", "failed"}
    return {
        "status": "warning" if warning else "completed",
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "index_outbox": outbox,
        "agent_jobs": agent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PKAS 单一后台同步、索引与 Agent 进程")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--include-sync", action="store_true")
    parser.add_argument("--outbox-only", action="store_true")
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    system = KnowledgeSystem.create()
    lock_path = system.settings.data_root / "runtime" / "pkas-core.lock"
    try:
        with CoreWorkerLock(lock_path):
            while True:
                if args.stop_file and args.stop_file.exists():
                    return
                report = run_core_cycle(
                    system,
                    include_sync=args.include_sync,
                    process_agents=not args.outbox_only,
                )
                print(json.dumps(report, ensure_ascii=False), flush=True)
                if args.once:
                    raise SystemExit(1 if report["status"] == "failed" else 0)
                for _ in range(max(30, args.interval_seconds)):
                    if args.stop_file and args.stop_file.exists():
                        return
                    time.sleep(1)
    except RuntimeError as exc:
        print(json.dumps({"status": "already_running", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped", "reason": "operator_interrupt"}))


if __name__ == "__main__":
    main()
