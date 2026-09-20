"""Run one already-confirmed local intake plan without a visible terminal.

This is a one-shot helper for the desktop launcher, not a resident service.
It records only aggregate progress so status polling never copies document text
or file names into a runtime log.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pkas.config import get_settings
from pkas.content_taxonomy import atomic_write
from pkas.intake import IntakeService
from pkas.runtime_manager import RuntimeManager
from pkas.system import KnowledgeSystem


def _status_path(plan_id: str) -> Path:
    settings = get_settings()
    return settings.data_root / "runtime" / f"intake-{plan_id}.status.json"


def _write_status(plan_id: str, service: IntakeService) -> dict:
    view = service.view(plan_id, offset=0)
    payload = {
        "plan_id": plan_id,
        "state": view.get("state"),
        "counts": view.get("counts", {}),
        "recovery_created": bool(view.get("recovery")),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path = _status_path(plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return payload


def main() -> int:
    if len(sys.argv) not in {2, 3} or len(sys.argv[1]) != 32:
        return 2
    plan_id = sys.argv[1]
    semantic = len(sys.argv) == 3 and sys.argv[2] == "--with-vector"
    if len(sys.argv) == 3 and not semantic:
        return 2
    settings = get_settings()
    runtime = RuntimeManager(settings) if semantic else None
    service: IntakeService | None = None
    try:
        if runtime is not None:
            runtime.ensure_qdrant_ready()
        system = KnowledgeSystem.create(settings)
        service = IntakeService(system)
        service.run(plan_id, confirmed=True, confirmed_vector=semantic)
        while service.thread and service.thread.is_alive():
            _write_status(plan_id, service)
            time.sleep(3)
        _write_status(plan_id, service)
        return 0
    finally:
        if service is not None:
            service.close()
        if runtime is not None and "qdrant" in runtime.children:
            runtime.control("qdrant", "stop")


if __name__ == "__main__":
    raise SystemExit(main())
