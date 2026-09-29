from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pkas.scheduled_sync import (
    FULL_SYNC_INTERVAL_MS,
    run_registered_local_sync,
    run_scheduled_sync,
)
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


def test_nightly_weflow_import_is_completed_even_when_local_scan_is_not_due(
    knowledge_system: KnowledgeSystem,
) -> None:
    local_calls = 0

    def local(_: KnowledgeSystem) -> dict[str, Any]:
        nonlocal local_calls
        local_calls += 1
        return _result(selected_roots=1)

    run_scheduled_sync(
        knowledge_system, now_ms=1_000_000, boot_id=80,
        full_sync_runner=local,
        weflow_import_runner=lambda _: _result("disabled"),
    )
    imported = run_scheduled_sync(
        knowledge_system, now_ms=1_060_000, boot_id=80,
        full_sync_runner=local,
        weflow_import_runner=lambda _: _result(
            candidate_sessions=48, imported_messages=79, failed_sessions=0,
        ),
    )
    replay = run_scheduled_sync(
        knowledge_system, now_ms=1_120_000, boot_id=80,
        full_sync_runner=local,
        weflow_import_runner=lambda _: _result(
            candidate_sessions=0, imported_messages=0, failed_sessions=0,
        ),
    )

    assert imported["status"] == "completed"
    assert imported["full_sync_due"] is False
    assert imported["weflow_import"]["imported_messages"] == 79
    assert replay["status"] == "deferred"
    assert replay["last_success_at_ms"] == 1_000_000
    assert local_calls == 1


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


def test_scheduled_local_sync_discovers_new_files_without_codex_or_vectors(
    knowledge_system: KnowledgeSystem, source_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    local = knowledge_system.sync.register_root(
        name="local", root_path=str(source_root), connector_type="local_files",
        sync_mode="index",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    knowledge_system.sync.register_root(
        name="codex", root_path=str(codex_root), connector_type="codex_sessions",
        sync_mode="index",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("scheduled local sync must not call the vector outbox")

    monkeypatch.setattr(knowledge_system.outbox, "process", forbidden)
    first = run_registered_local_sync(knowledge_system)
    assert first["selected_roots"] == 1
    assert first["index_outbox"]["status"] == "skipped"

    (source_root / "misleading.md").write_text(
        "功能需求与验收标准：核对正文而非文件名。", encoding="utf-8"
    )
    second = run_registered_local_sync(knowledge_system)
    assert second["status"] == "completed"
    assert second["results"][0]["result"]["classification_suggested"] == 1
    assert knowledge_system.sync.search_catalog("misleading.md", root_id=local["id"])


def test_weflow_failure_does_not_block_authorized_local_sync(
    knowledge_system: KnowledgeSystem,
) -> None:
    calls = {"local": 0}

    def failing_import(_: KnowledgeSystem) -> dict[str, Any]:
        raise OSError("private export path must never enter a receipt")

    def local(_: KnowledgeSystem) -> dict[str, Any]:
        calls["local"] += 1
        return _result(selected_roots=1)

    result = run_scheduled_sync(
        knowledge_system, now_ms=7_000_000, boot_id=40,
        full_sync_runner=local, weflow_import_runner=failing_import,
    )
    assert result["status"] == "failed"
    assert result["weflow_import"] == {"status": "failed", "error_type": "OSError"}
    assert calls["local"] == 1
    assert result["last_success_at_ms"] == 7_000_000
    assert "private export path" not in str(result)


def test_export_timeout_is_not_reported_as_complete(
    knowledge_system: KnowledgeSystem,
) -> None:
    result = run_scheduled_sync(
        knowledge_system, now_ms=8_000_000, boot_id=50,
        full_sync_runner=lambda _: _result(selected_roots=1),
        weflow_import_runner=lambda _: _result("disabled", failed_sessions=0),
        weflow_export_status="timeout",
    )
    assert result["status"] == "failed"
    assert result["full_sync"]["status"] == "completed"
    assert result["weflow_export_status"] == "timeout"


def test_weflow_warning_without_failed_session_count_is_still_warning(
    knowledge_system: KnowledgeSystem,
) -> None:
    result = run_scheduled_sync(
        knowledge_system, now_ms=9_000_000, boot_id=60,
        full_sync_runner=lambda _: _result(selected_roots=1),
        weflow_import_runner=lambda _: _result("warning", failed_sessions=0),
    )
    assert result["status"] == "warning"
    assert result["last_success_at_ms"] == 9_000_000


def test_success_watermark_uses_finish_time_not_start_time(
    knowledge_system: KnowledgeSystem, monkeypatch,
) -> None:
    ticks = iter([10_000_000, 10_000_500])
    monkeypatch.setattr("pkas.scheduled_sync._now_ms", lambda: next(ticks))
    result = run_scheduled_sync(
        knowledge_system, boot_id=70,
        full_sync_runner=lambda _: _result(selected_roots=1),
        weflow_import_runner=lambda _: _result("disabled", failed_sessions=0),
    )
    assert result["last_success_at_ms"] == 10_000_500


def test_scheduled_local_sync_without_roots_requires_configuration(
    knowledge_system: KnowledgeSystem,
) -> None:
    result = run_registered_local_sync(knowledge_system)
    assert result["status"] == "warning"
    assert result["no_local_roots"] is True
    assert result["selected_roots"] == 0


def test_scheduled_local_sync_pauses_when_data_disk_is_low(
    knowledge_system: KnowledgeSystem, source_root: Path, monkeypatch,
) -> None:
    knowledge_system.sync.register_root(
        name="local", root_path=str(source_root), connector_type="local_files",
        sync_mode="catalog",
    )
    monkeypatch.setattr(
        "pkas.sync_worker.shutil.disk_usage", lambda _path: SimpleNamespace(free=1)
    )
    result = run_registered_local_sync(knowledge_system)
    assert result["status"] == "warning"
    assert result["low_disk"] is True
    assert result["results"] == []
