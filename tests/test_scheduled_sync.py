from typing import Any

from pkas.scheduled_sync import FULL_SYNC_INTERVAL_MS, run_scheduled_sync
from pkas.system import KnowledgeSystem


def _result(status: str = "completed", **extra: Any) -> dict[str, Any]:
    return {"status": status, **extra}


def test_scheduled_sync_runs_once_for_new_boot(
    knowledge_system: KnowledgeSystem,
) -> None:
    calls = {"full": 0, "weflow": 0}

    def full(system: KnowledgeSystem) -> dict[str, Any]:
        assert system is knowledge_system
        calls["full"] += 1
        return _result()

    def weflow(system: KnowledgeSystem) -> dict[str, Any]:
        assert system is knowledge_system
        calls["weflow"] += 1
        return _result(failed_sessions=0)

    first = run_scheduled_sync(
        knowledge_system,
        now_ms=1_000_000,
        boot_id=10,
        full_sync_runner=full,
        weflow_import_runner=weflow,
    )
    second = run_scheduled_sync(
        knowledge_system,
        now_ms=1_000_000 + 60_000,
        boot_id=10,
        full_sync_runner=full,
        weflow_import_runner=weflow,
    )

    assert first["status"] == "completed"
    assert first["reason"] == "new-boot"
    assert second["status"] == "deferred"
    assert second["reason"] == "not-due"
    assert calls == {"full": 1, "weflow": 2}


def test_scheduled_sync_runs_again_six_hours_after_success(
    knowledge_system: KnowledgeSystem,
) -> None:
    calls = {"full": 0}

    def full(_: KnowledgeSystem) -> dict[str, Any]:
        calls["full"] += 1
        return _result()

    def weflow(_: KnowledgeSystem) -> dict[str, Any]:
        return _result(failed_sessions=0)

    start = 10_000_000
    run_scheduled_sync(
        knowledge_system,
        now_ms=start,
        boot_id=20,
        full_sync_runner=full,
        weflow_import_runner=weflow,
    )
    before = run_scheduled_sync(
        knowledge_system,
        now_ms=start + FULL_SYNC_INTERVAL_MS - 1,
        boot_id=20,
        full_sync_runner=full,
        weflow_import_runner=weflow,
    )
    due = run_scheduled_sync(
        knowledge_system,
        now_ms=start + FULL_SYNC_INTERVAL_MS,
        boot_id=20,
        full_sync_runner=full,
        weflow_import_runner=weflow,
    )

    assert before["full_sync_due"] is False
    assert due["full_sync_due"] is True
    assert due["reason"] == "six-hours-elapsed"
    assert calls["full"] == 2


def test_warning_does_not_advance_success_watermark(
    knowledge_system: KnowledgeSystem,
) -> None:
    def full(_: KnowledgeSystem) -> dict[str, Any]:
        return _result("warning")

    result = run_scheduled_sync(
        knowledge_system,
        now_ms=5_000_000,
        boot_id=30,
        full_sync_runner=full,
        weflow_import_runner=lambda _: _result(failed_sessions=0),
    )

    assert result["status"] == "warning"
    assert result["last_success_at_ms"] is None
